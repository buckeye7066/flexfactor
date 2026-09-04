#!/usr/bin/env python3
"""Safe Tenets code-context integration for FlexFactor.

Tenets is a local-first external tool that ranks the files most relevant to a
coding objective. FlexFactor uses the ranking only to PRIORITIZE its existing
complete source sweep; it never lets Tenets remove a file, mark a file clean, or
bypass a release gate.

The adapter is deliberately fail-open with respect to Tenets availability:
absence, timeout, malformed output, or a tool failure is recorded in a bounded
JSON evidence manifest while FlexFactor retains its original file order. Invalid
caller input still fails closed so the wrong repository is never analyzed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from importlib import metadata as importlib_metadata
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat as stat_module
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from typing import Any, BinaryIO, Iterable, Mapping, MutableMapping, Sequence

TENETS_VERSION = "0.13.3"
DEFAULT_TOP = 50
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TOP = 200
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_RANKER_MEMORY_BYTES = 1024 * 1024 * 1024
MAX_RANKER_PROCESSES = 64
LINUX_CGROUP_ROOT_ENV = "FLEXFACTOR_TENETS_CGROUP_ROOT"
# Python sequences cannot contain more than sys.maxsize elements. Using
# that interpreter bound neutralizes the caller's review quota without
# imposing a smaller, arbitrary repository cutoff before prioritization.
_UNBOUNDED_ENUMERATION_LIMIT = sys.maxsize

_DISABLE_VALUES = {"0", "false", "no", "off"}
_CAP_PARAMETER_NAMES = (
    "max_files",
    "max_files_per_run",
    "file_limit",
    "limit",
)
_CAP_GLOBAL_NAMES = (
    "MAX_FILES_PER_RUN",
    "_MAX_FILES_PER_RUN",
)
_INSTALL_LOCK = threading.Lock()
_ENUMERATION_LOCK = threading.RLock()
_RESULT_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: dict[tuple[str, str, int, float, str], "TenetsContextResult"] = {}


@dataclass(frozen=True)
class RankedFile:
    path: str
    score: float | None = None


@dataclass(frozen=True)
class TenetsContextResult:
    schema_version: int
    tool: str
    expected_version: str
    adapter_version: int
    status: str
    project_root: str
    task: str
    files: tuple[RankedFile, ...]
    message: str
    duration_seconds: float
    output_path: str
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["files"] = [asdict(item) for item in self.files]
        data["command"] = list(self.command)
        return data


@dataclass(frozen=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow_stream: str | None = None
    read_error: str | None = None


def enabled() -> bool:
    """Whether automatic Tenets prioritization is enabled for launcher runs."""
    return os.environ.get("FLEXFACTOR_TENETS", "1").strip().lower() not in _DISABLE_VALUES


def _bounded_text(value: str, *, limit: int = 2_000) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _coerce_score(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return score


def _candidate_items(payload: Any) -> Iterable[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("files", "results", "ranked_files", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return ()


def _safe_relative_path(project_root: Path, candidate: Any) -> str | None:
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    raw = Path(candidate.strip())
    combined = raw if raw.is_absolute() else project_root / raw
    try:
        resolved = combined.resolve(strict=True)
        relative = resolved.relative_to(project_root)
    except (OSError, ValueError):
        return None
    normalized = relative.as_posix()
    if normalized in ("", ".") or not resolved.is_file():
        return None
    return normalized


def _parse_ranked_files(payload: Any, project_root: Path, top: int) -> tuple[RankedFile, ...]:
    ranked: list[RankedFile] = []
    seen: set[str] = set()
    for item in _candidate_items(payload):
        path_value: Any = item
        score: float | None = None
        if isinstance(item, Mapping):
            path_value = item.get("path", item.get("file", item.get("name")))
            score = _coerce_score(item.get("score", item.get("relevance_score")))
        safe_path = _safe_relative_path(project_root, path_value)
        if safe_path is None or safe_path in seen:
            continue
        seen.add(safe_path)
        ranked.append(RankedFile(path=safe_path, score=score))
        if len(ranked) >= top:
            break
    return tuple(ranked)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _state_root() -> Path:
    override = os.environ.get("FLEXFACTOR_STATE_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".flexfactor"


def _default_output_path(project_root: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", project_root.name).strip("-._") or "project"
    identity = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:12]
    candidate = (
        _state_root() / "context" / f"{slug}-{identity}" / "tenets-context.json"
    ).resolve(strict=False)
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return candidate
    raise ValueError(
        "FlexFactor state/evidence must be outside the audited repository"
    )


def _resolve_output_path(project_root: Path, output: str | os.PathLike[str] | None) -> Path:
    if output is None:
        return _default_output_path(project_root)
    candidate = Path(output).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(project_root)
    except ValueError:
        return resolved
    raise ValueError("Tenets evidence output must be outside the audited repository")


def _trusted_tenets_directories(
    *, interpreter: str | os.PathLike[str] | None = None, platform_name: str | None = None
) -> tuple[Path, ...]:
    interpreter_path = Path(interpreter or sys.executable).expanduser().resolve(strict=False)
    windows = (platform_name or os.name) == "nt"
    candidates = [interpreter_path.parent]
    prefix_scripts = Path(sys.prefix).expanduser().resolve(strict=False) / ("Scripts" if windows else "bin")
    if prefix_scripts not in candidates:
        candidates.append(prefix_scripts)
    return tuple(candidates)


def _is_trusted_tenets_executable(
    executable: str | os.PathLike[str],
    *,
    interpreter: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
) -> bool:
    windows = (platform_name or os.name) == "nt"
    expected_names = {"tenets.exe", "tenets"} if windows else {"tenets"}
    candidate = Path(executable).expanduser().resolve(strict=False)
    if candidate.name.lower() not in {name.lower() for name in expected_names}:
        return False
    if candidate.parent not in _trusted_tenets_directories(
        interpreter=interpreter, platform_name=platform_name
    ):
        return False
    return candidate.is_file() and (windows or os.access(candidate, os.X_OK))


def _find_tenets_executable(
    *,
    interpreter: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    path_value: str | None = None,
) -> str | None:
    """Resolve Tenets only from the active Python installation, never PATH."""
    del path_value  # API compatibility only. Ambient PATH is intentionally ignored.
    windows = (platform_name or os.name) == "nt"
    executable_names = ("tenets.exe", "tenets") if windows else ("tenets",)
    for directory in _trusted_tenets_directories(
        interpreter=interpreter, platform_name=platform_name
    ):
        for name in executable_names:
            candidate = directory / name
            if _is_trusted_tenets_executable(
                candidate, interpreter=interpreter, platform_name=platform_name
            ):
                return str(candidate.resolve(strict=False))
    return None


def _trusted_tenets_metadata_directories() -> tuple[Path, ...]:
    """Return only active-interpreter package roots, excluding cwd/user paths."""
    directories: list[Path] = []
    for name in ("purelib", "platlib"):
        raw = sysconfig.get_path(name)
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve(strict=False)
        if candidate not in directories:
            directories.append(candidate)
    return tuple(directories)


def _tenets_distribution_version(
    executable: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return Tenets' version only when metadata owns the selected script.

    With an executable, this binds the console script to one distribution in
    the active interpreter's installation roots.  Ambient ``sys.path`` (and a
    target checkout containing forged ``*.dist-info`` metadata) is never a
    version oracle for the executable that will run.
    """
    try:
        if executable is None:
            return importlib_metadata.version("tenets")
        candidate = Path(executable).expanduser().resolve(strict=True)
        if not _is_trusted_tenets_executable(candidate):
            return None
        matches: list[str] = []
        roots = [str(path) for path in _trusted_tenets_metadata_directories()]
        for distribution in importlib_metadata.distributions(path=roots):
            name = str(distribution.metadata.get("Name") or "")
            canonical_name = re.sub(r"[-_.]+", "-", name).casefold()
            if canonical_name != "tenets":
                continue
            has_entry_point = any(
                entry.group == "console_scripts" and entry.name == "tenets"
                for entry in distribution.entry_points
            )
            owns_executable = any(
                Path(distribution.locate_file(record)).resolve(strict=False)
                == candidate
                for record in (distribution.files or ())
            )
            if has_entry_point and owns_executable:
                matches.append(str(distribution.version))
        return matches[0] if len(matches) == 1 else None
    except Exception:
        # Distribution metadata is optional input.  A partially installed
        # package/backend can raise filesystem, decoding, archive, parser, or
        # other ordinary metadata errors here; none may turn an unavailable
        # advisory ranker into a traceback that aborts the production audit.
        # BaseException subclasses still propagate, so interrupts are honored.
        return None


def _windows_job_limit_policy() -> tuple[int, int, int]:
    """Return Job Object flags, process cap, and aggregate memory cap."""
    job_object_limit_active_process = 0x00000008
    job_object_limit_job_memory = 0x00000200
    job_object_limit_kill_on_job_close = 0x00002000
    return (
        job_object_limit_active_process
        | job_object_limit_job_memory
        | job_object_limit_kill_on_job_close,
        MAX_RANKER_PROCESSES,
        MAX_RANKER_MEMORY_BYTES,
    )


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int:
    """Put ``process`` in a kill-on-close Windows Job Object.

    A console process group is not a containment boundary after its leader
    exits: ``taskkill /T`` can no longer discover an orphaned descendant from
    that exited PID.  A Job Object retains the descendant set independently of
    the leader, so closing/terminating it remains effective for that case.
    """
    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )
        )

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _ExtendedLimitInformation()
        limit_flags, process_limit, memory_limit = _windows_job_limit_policy()
        info.BasicLimitInformation.LimitFlags = limit_flags
        info.BasicLimitInformation.ActiveProcessLimit = process_limit
        info.JobMemoryLimit = memory_limit
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = wintypes.HANDLE(int(getattr(process, "_handle")))
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        value = handle if isinstance(handle, int) else handle.value
        if not value:
            raise OSError("Windows Job Object returned an invalid handle")
        return int(value)
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _resume_windows_process(process_id: int) -> None:
    """Resume the primary thread of a process created with CREATE_SUSPENDED."""
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot_value = snapshot if isinstance(snapshot, int) else snapshot.value
    if not snapshot or snapshot_value == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        available = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while available:
            if int(entry.th32OwnerProcessID) == int(process_id):
                thread = kernel32.OpenThread(
                    0x0002, False, entry.th32ThreadID  # THREAD_SUSPEND_RESUME
                )
                if thread:
                    try:
                        previous = kernel32.ResumeThread(thread)
                        if previous != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        kernel32.CloseHandle(thread)
            available = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    if resumed < 1:
        raise OSError("could not resume the suspended Tenets process")


def _contain_and_resume_windows_process(process: subprocess.Popen[bytes]) -> int:
    """Atomically establish descendant containment before Tenets can execute."""
    job = _create_windows_kill_job(process)
    try:
        _resume_windows_process(process.pid)
    except Exception:
        _terminate_windows_kill_job(job)
        raise
    return job


def _terminate_windows_kill_job(handle: int) -> None:
    """Terminate every member of a Windows Job Object and release it."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = wintypes.HANDLE(handle)
    try:
        kernel32.TerminateJobObject(job, 1)
    finally:
        kernel32.CloseHandle(job)


def _posix_process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _enable_linux_child_subreaper() -> None:
    """Make this process the reparenting boundary for orphan descendants."""
    if not sys.platform.startswith("linux"):
        raise OSError("Linux child-subreaper containment is unavailable")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    prctl.restype = ctypes.c_int
    if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _enable_linux_parent_death_signal(expected_parent_pid: int) -> None:
    """Ask the kernel to stop the supervisor if its FlexFactor parent dies."""
    if not sys.platform.startswith("linux"):
        raise OSError("Linux parent-death signalling is unavailable")
    if expected_parent_pid <= 1 or os.getppid() != expected_parent_pid:
        raise OSError("the FlexFactor parent changed before containment")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    prctl.restype = ctypes.c_int
    if prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # The parent can exit between getppid() and prctl().  Linux does not send a
    # retroactive signal in that race, so prove the same live parent remains.
    if os.getppid() != expected_parent_pid:
        raise OSError("the FlexFactor parent died during containment")


def _apply_linux_ranker_file_limit() -> None:
    """Bound individual file writes in addition to aggregate cgroup limits."""
    if not sys.platform.startswith("linux"):
        raise OSError("Linux ranker file limits are unavailable")
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        effective = (
            MAX_STDOUT_BYTES
            if hard == resource.RLIM_INFINITY
            else min(MAX_STDOUT_BYTES, int(hard))
        )
        if effective < 1:
            raise OSError("an inherited file-size limit is unusable")
        resource.setrlimit(resource.RLIMIT_FSIZE, (effective, effective))
    except (AttributeError, ImportError, OSError, ValueError) as exc:
        raise OSError(f"could not enforce Linux ranker file limits: {exc}") from exc


def _linux_cgroup_root() -> Path:
    """Return a verified owner-delegated cgroup-v2 directory.

    RLIMIT_AS is per process and RLIMIT_NPROC is per real user (and ignored by
    root), so neither is a process-tree boundary.  Linux ranking is optional:
    without a delegated cgroup that exposes aggregate memory/PID controls and
    atomic tree kill, the safe behavior is to retain canonical file order.
    """
    configured = os.environ.get(LINUX_CGROUP_ROOT_ENV, "").strip()
    if not configured:
        raise OSError(
            f"{LINUX_CGROUP_ROOT_ENV} is required for Linux Tenets containment"
        )
    candidate = Path(configured).expanduser().resolve(strict=True)
    system_root = Path("/sys/fs/cgroup").resolve(strict=True)
    try:
        candidate.relative_to(system_root)
    except ValueError as exc:
        raise OSError("the Tenets cgroup root is outside cgroup v2") from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise OSError("the Tenets cgroup root is not a real directory")
    controllers = candidate / "cgroup.controllers"
    subtree = candidate / "cgroup.subtree_control"
    try:
        available = set(controllers.read_text(encoding="ascii").split())
        enabled = set(subtree.read_text(encoding="ascii").replace("+", "").split())
    except OSError as exc:
        raise OSError("the Tenets cgroup delegation cannot be inspected") from exc
    if not {"memory", "pids"}.issubset(available | enabled):
        raise OSError("the Tenets cgroup lacks memory and pids controllers")
    return candidate


def _write_cgroup_control(path: Path, value: str) -> None:
    try:
        path.write_text(value + "\n", encoding="ascii")
        observed = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise OSError(f"could not set cgroup control {path.name}") from exc
    if observed != value:
        raise OSError(f"cgroup control {path.name} did not retain its limit")


def _prepare_linux_ranker_cgroup() -> Path:
    """Create one empty, aggregate-limited cgroup for one ranker invocation."""
    parent = _linux_cgroup_root()
    for _attempt in range(8):
        boundary = parent / (
            f"rank-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(6)}"
        )
        try:
            boundary.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise OSError("could not create the Tenets cgroup boundary") from exc
    else:
        raise OSError("could not allocate a unique Tenets cgroup boundary")
    try:
        required = (
            "cgroup.procs", "cgroup.events", "cgroup.kill",
            "memory.max", "memory.oom.group", "pids.max",
        )
        if any(not (boundary / name).is_file() for name in required):
            raise OSError("the delegated cgroup lacks required controls")
        _write_cgroup_control(boundary / "memory.max", str(MAX_RANKER_MEMORY_BYTES))
        _write_cgroup_control(boundary / "memory.oom.group", "1")
        _write_cgroup_control(boundary / "pids.max", str(MAX_RANKER_PROCESSES))
        swap_limit = boundary / "memory.swap.max"
        if swap_limit.is_file():
            _write_cgroup_control(swap_limit, "0")
        return boundary
    except Exception:
        try:
            boundary.rmdir()
        except OSError:
            pass
        raise


def _join_linux_ranker_cgroup(boundary: Path) -> None:
    """Move the trusted supervisor into the prepared boundary before Tenets."""
    try:
        (boundary / "cgroup.procs").write_text(
            f"{os.getpid()}\n", encoding="ascii"
        )
        members = set((boundary / "cgroup.procs").read_text(
            encoding="ascii").split())
    except OSError as exc:
        raise OSError("the Tenets cgroup membership cannot be verified") from exc
    if str(os.getpid()) not in members:
        raise OSError("the Tenets supervisor did not enter its cgroup")


def _kill_linux_ranker_cgroup(boundary: Path) -> None:
    """Atomically terminate every task still in the job-scoped boundary."""
    try:
        (boundary / "cgroup.kill").write_text("1\n", encoding="ascii")
    except OSError as exc:
        raise OSError("could not atomically kill the Tenets cgroup") from exc


def _linux_ranker_cgroup_populated(boundary: Path) -> bool:
    """Return the kernel's membership state for one invocation boundary."""
    try:
        rows = {}
        for line in (boundary / "cgroup.events").read_text(
                encoding="ascii").splitlines():
            key, separator, value = line.partition(" ")
            if separator:
                rows[key] = value.strip()
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise OSError("the Tenets cgroup membership cannot be inspected") from exc
    if rows.get("populated") not in {"0", "1"}:
        raise OSError("the Tenets cgroup reported an invalid membership state")
    return rows["populated"] == "1"


def _cleanup_linux_ranker_cgroup(boundary: Path) -> None:
    """Remove an empty invocation boundary; never hide a populated one."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            if not _linux_ranker_cgroup_populated(boundary):
                boundary.rmdir()
                return
        except FileNotFoundError:
            return
        if time.monotonic() >= deadline:
            raise OSError("the Tenets cgroup is still populated")
        time.sleep(0.01)


def _finalize_linux_ranker_cgroup(boundary: Path) -> None:
    """Prove a supervisor's cgroup empty, killing it atomically if necessary.

    A reaped supervisor is not itself cleanup evidence: it may have returned
    125 because procfs inventory or pidfd signalling failed while a
    session-escaped descendant remained alive. The parent therefore retains
    the kernel cgroup handle and independently verifies membership after every
    exit path.
    """
    try:
        populated = _linux_ranker_cgroup_populated(boundary)
    except FileNotFoundError:
        return
    except OSError:
        # If membership cannot be proven empty, fail toward containment.  A
        # successful cgroup.kill is safe on an empty boundary and covers every
        # task atomically if the supervisor left descendants behind.
        _kill_linux_ranker_cgroup(boundary)
    else:
        if populated:
            _kill_linux_ranker_cgroup(boundary)
    _cleanup_linux_ranker_cgroup(boundary)


def _linux_direct_child_pids() -> tuple[int, ...]:
    """Return direct children of the single-threaded supervisor from procfs.

    Some hardened kernels omit ``task/<tid>/children``. Scanning status files
    also lets the supervisor translate a host-mounted procfs PID to the PID in
    its own namespace before signalling it.
    """
    def _status_ids(path: Path) -> tuple[int, tuple[int, ...]]:
        parent_pid: int | None = None
        namespace_pids: tuple[int, ...] = ()
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith("PPid:"):
                parent_pid = int(line.split(":", 1)[1].strip())
            elif line.startswith("NSpid:"):
                namespace_pids = tuple(
                    int(value) for value in line.split(":", 1)[1].split()
                )
        if parent_pid is None:
            raise ValueError("missing PPid")
        return parent_pid, namespace_pids

    try:
        _self_parent, self_namespace_pids = _status_ids(Path("/proc/self/status"))
        procfs_self_pid = (
            self_namespace_pids[0]
            if self_namespace_pids
            else int(os.readlink("/proc/self"))
        )
        namespace_depth = max(0, len(self_namespace_pids) - 1)
        children: list[int] = []
        for candidate in Path("/proc").iterdir():
            if not candidate.name.isdigit():
                continue
            try:
                parent_pid, candidate_namespace_pids = _status_ids(
                    candidate / "status"
                )
            except (OSError, ValueError):
                # Processes can exit between directory enumeration and read.
                continue
            if parent_pid != procfs_self_pid:
                continue
            if candidate_namespace_pids:
                if len(candidate_namespace_pids) <= namespace_depth:
                    raise OSError("procfs child is outside the supervisor namespace")
                children.append(candidate_namespace_pids[namespace_depth])
            else:
                # Without NSpid, this is safe only when procfs and the process
                # agree on the current namespace's PID numbering.
                if procfs_self_pid != os.getpid():
                    raise OSError("procfs PID namespace mapping is unavailable")
                children.append(int(candidate.name))
        return tuple(children)
    except (OSError, ValueError) as exc:
        raise OSError("Linux procfs child inventory is unavailable") from exc


def _reap_linux_children() -> None:
    while True:
        try:
            child_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if child_pid <= 0:
            return


def _terminate_linux_supervisor_children(timeout_seconds: float = 2.0) -> bool:
    """Kill every adopted child, including helpers that created a new session."""
    started = time.monotonic()
    deadline = started + timeout_seconds
    escalation = started + min(0.5, timeout_seconds / 2)
    while time.monotonic() < deadline:
        _reap_linux_children()
        children = _linux_direct_child_pids()
        if not children:
            return True
        chosen_signal = (
            signal.SIGTERM if time.monotonic() < escalation else signal.SIGKILL
        )
        for child_pid in children:
            try:
                pidfd = os.pidfd_open(child_pid)
            except ProcessLookupError:
                continue
            try:
                try:
                    signal.pidfd_send_signal(pidfd, chosen_signal)
                except ProcessLookupError:
                    # The pidfd prevents reuse; ESRCH here means this exact
                    # child finished between inventory and signalling.
                    pass
            finally:
                os.close(pidfd)
        time.sleep(0.01)
    _reap_linux_children()
    return not _linux_direct_child_pids()


def _linux_supervise_command(
    command: Sequence[str],
    *,
    cgroup_boundary: Path,
    expected_parent_pid: int,
    max_runtime_seconds: float,
) -> int:
    """Run one command behind a subreaper that owns orphaned descendants.

    This function executes only in the isolated supervisor subprocess started
    below. Keeping the subreaper out of the long-lived FlexFactor process
    avoids adopting or signalling unrelated concurrent tools.
    """
    if (not command or expected_parent_pid <= 1
            or not math.isfinite(max_runtime_seconds)
            or max_runtime_seconds <= 0):
        return 125
    received_signal: list[int | None] = [None]

    def _request_stop(signum, _frame) -> None:
        received_signal[0] = int(signum)

    for handled in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(handled, _request_stop)
    try:
        _enable_linux_parent_death_signal(expected_parent_pid)
        _join_linux_ranker_cgroup(cgroup_boundary)
        _enable_linux_child_subreaper()
        # Prove procfs inventory is available before any optional ranker code
        # executes. Without it the supervisor cannot safely enumerate adoptees.
        _linux_direct_child_pids()
        # A pidfd makes cleanup immune to PID reuse between inventory and
        # signalling. Refuse to launch if the running kernel lacks this piece
        # of the containment boundary.
        self_pidfd = os.pidfd_open(os.getpid())
        os.close(self_pidfd)
        # Aggregate memory/process ceilings come from the cgroup. RLIMIT_FSIZE
        # remains useful for each file a descendant may open in isolation.
        _apply_linux_ranker_file_limit()
    except OSError as exc:
        print(f"flexfactor Tenets containment unavailable: {exc}", file=sys.stderr)
        return 125
    if received_signal[0] is not None:
        return 128 + received_signal[0]
    try:
        child = subprocess.Popen(tuple(command), shell=False)
    except OSError as exc:
        print(f"flexfactor could not start Tenets: {exc}", file=sys.stderr)
        return 125

    stop_started: float | None = None
    supervisor_deadline = time.monotonic() + max_runtime_seconds
    deadline_expired = False
    while True:
        if time.monotonic() >= supervisor_deadline and received_signal[0] is None:
            received_signal[0] = signal.SIGTERM
            deadline_expired = True
        requested = received_signal[0]
        if requested is not None:
            if stop_started is None:
                stop_started = time.monotonic()
                try:
                    child.send_signal(requested)
                except ProcessLookupError:
                    pass
            elif time.monotonic() - stop_started >= 0.5:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
        try:
            returncode = child.wait(timeout=0.02)
            break
        except subprocess.TimeoutExpired:
            continue

    result = 125
    try:
        cleaned = _terminate_linux_supervisor_children()
    except OSError as exc:
        print(f"flexfactor could not verify Tenets descendant cleanup: {exc}",
              file=sys.stderr)
        cleaned = False
    if not cleaned:
        print("flexfactor could not terminate every Tenets descendant",
              file=sys.stderr)
    elif deadline_expired:
        result = 124
    elif received_signal[0] is not None:
        result = 128 + received_signal[0]
    elif returncode < 0:
        result = 128 + abs(returncode)
    else:
        result = min(returncode, 255)
    return result


def _read_bounded_pipe(
    pipe: BinaryIO,
    *,
    limit: int,
    stream_name: str,
    chunks: list[bytes],
    overflow_event: threading.Event,
    state: MutableMapping[str, str],
    state_lock: threading.Lock,
) -> None:
    total = 0
    try:
        while True:
            read1 = getattr(pipe, "read1", None)
            if callable(read1):
                chunk = read1(64 * 1024)
            else:
                try:
                    chunk = os.read(pipe.fileno(), 64 * 1024)
                except (AttributeError, OSError, ValueError):
                    chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            remaining = max(0, limit - total)
            if remaining:
                retained = chunk[:remaining]
                chunks.append(retained)
                total += len(retained)
            if len(chunk) > remaining:
                with state_lock:
                    state.setdefault("overflow_stream", stream_name)
                overflow_event.set()
                return
    except Exception as exc:
        with state_lock:
            state.setdefault("read_error", f"{stream_name}: {exc}")
        overflow_event.set()
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _terminate_process_tree(
    process: subprocess.Popen[bytes], *, windows_job: int | None = None,
    linux_cgroup: Path | None = None,
) -> None:
    """Stop the ranker and every helper it created.

    ``terminate()`` only addresses the direct child. A helper that inherited
    stdout/stderr can otherwise survive the timeout and keep both reader
    threads blocked. On Linux, the retained process-group leader is a trusted
    subreaper supervisor, so a helper that calls ``setsid()`` is adopted and
    terminated before that leader exits. Windows uses a kill-on-close Job
    Object. Other POSIX systems fail closed before optional Tenets is launched.
    """
    if os.name == "nt":
        if windows_job is not None:
            _terminate_windows_kill_job(windows_job)
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return
        if process.poll() is not None:
            return
        # Windows has no killpg equivalent.  taskkill's /T flag walks the
        # descendant tree and /F prevents a console-less process from ignoring
        # the request.  Use an absolute system path so neither the audited
        # repository nor ambient PATH selects the executable.
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        taskkill = (
            Path(buffer.value) / "taskkill.exe"
            if 0 < length < len(buffer)
            else Path(r"C:\Windows\System32\taskkill.exe")
        )
        try:
            subprocess.run(
                (str(taskkill), "/PID", str(process.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    elif sys.platform.startswith("linux"):
        # The child is FlexFactor's trusted subreaper supervisor. While it is
        # unreaped its PID cannot be recycled, so signal that exact child and
        # let it terminate adopted/session-escaped descendants via pidfds.
        # Once it has exited it has already verified that no descendants
        # remain; never signal its numeric process group after that boundary.
        if process.poll() is not None:
            return
        try:
            process.send_signal(signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if process.poll() is None:
            if linux_cgroup is None:
                raise OSError(
                    "cannot escalate Linux Tenets cleanup without its cgroup"
                )
            # Killing only the subreaper would orphan a setsid() helper. The
            # cgroup kill control is the one atomic handle that covers the
            # supervisor, direct ranker, and every descendant together.
            _kill_linux_ranker_cgroup(linux_cgroup)
    else:
        # The group can outlive its leader.  Address the retained PGID even
        # when Popen.poll() already reports that the direct process exited.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        # A Linux supervisor may first need to stop an escaped direct ranker,
        # adopt its helpers, and then terminate those adoptees. Keep the
        # retained group alive long enough for that two-stage cleanup.
        deadline = time.monotonic() + (3 if sys.platform.startswith("linux") else 1)
        while _posix_process_group_alive(process.pid) and time.monotonic() < deadline:
            process.poll()  # Reap an exited leader; descendants retain the PGID.
            time.sleep(0.02)
        if _posix_process_group_alive(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            try:
                process.kill()
            except OSError:
                pass


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass


def _run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
    env: Mapping[str, str] | None = None,
) -> _BoundedProcessResult:
    """Run a child while bounding both output streams during production."""
    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("output limits must be positive")
    process_group: dict[str, Any]
    process_command = tuple(str(part) for part in command)
    linux_cgroup: Path | None = None
    if os.name == "nt":
        process_group = {
            # CREATE_SUSPENDED closes the launch/assignment race: no ranker or
            # helper instruction runs until the kill-on-close Job Object owns
            # the process.  The primary thread is resumed immediately after.
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004
            ),
        }
    elif sys.platform.startswith("linux"):
        linux_cgroup = _prepare_linux_ranker_cgroup()
        process_group = {"start_new_session": True}
        # The target shares this new session, but cannot escape cleanup by
        # creating another one: after it exits, the dedicated Linux subreaper
        # adopts every orphan and kills all adoptees before closing its pipes.
        process_command = (
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).resolve(strict=True)),
            "--_linux-subprocess-supervisor",
            "--cgroup",
            str(linux_cgroup),
            "--parent-pid",
            str(os.getpid()),
            "--max-runtime",
            str(timeout_seconds + 5.0),
            "--",
            *process_command,
        )
    else:
        # POSIX process groups are escapable via setsid(). Tenets is advisory,
        # so preserve the primary audit path instead of launching it without a
        # containment primitive that survives direct-parent exit.
        raise OSError(
            "strong Tenets descendant containment is unavailable on this platform"
        )
    process: subprocess.Popen[bytes] | None = None
    windows_job: int | None = None
    tree_closed = False
    started_readers: list[threading.Thread] = []
    try:
        process = subprocess.Popen(
            process_command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=dict(env) if env is not None else None,
            **process_group,
        )
        if os.name == "nt":
            try:
                windows_job = _contain_and_resume_windows_process(process)
            except Exception as exc:
                # Do not run an optional helper without the containment required
                # to clean up descendants after the direct ranker exits.
                _terminate_process_tree(process)
                tree_closed = True
                raise OSError(
                    f"could not contain Tenets in a Windows Job Object: {exc}"
                ) from exc
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        overflow_event = threading.Event()
        state: dict[str, str] = {}
        state_lock = threading.Lock()
        readers = (
            threading.Thread(
                target=_read_bounded_pipe,
                kwargs={
                    "pipe": process.stdout,
                    "limit": stdout_limit,
                    "stream_name": "stdout",
                    "chunks": stdout_chunks,
                    "overflow_event": overflow_event,
                    "state": state,
                    "state_lock": state_lock,
                },
                name="tenets-stdout-reader",
                daemon=True,
            ),
            threading.Thread(
                target=_read_bounded_pipe,
                kwargs={
                    "pipe": process.stderr,
                    "limit": stderr_limit,
                    "stream_name": "stderr",
                    "chunks": stderr_chunks,
                    "overflow_event": overflow_event,
                    "state": state,
                    "state_lock": state_lock,
                },
                name="tenets-stderr-reader",
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
            started_readers.append(reader)

        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        while process.poll() is None:
            if overflow_event.wait(timeout=0.02):
                _terminate_process_tree(
                    process, windows_job=windows_job,
                    linux_cgroup=linux_cgroup,
                )
                tree_closed = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_tree(
                    process, windows_job=windows_job,
                    linux_cgroup=linux_cgroup,
                )
                tree_closed = True
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue

        # A reaped Linux supervisor's numeric PID/PGID is now recyclable and
        # must not be signalled. Its retained cgroup is independently verified
        # in finally below, because exit 125 can mean supervisor cleanup failed.
        # Windows Job Objects remain the authoritative descendant handle after
        # their leader exits and must always be closed.
        if not tree_closed:
            if sys.platform.startswith("linux") and process.poll() is not None:
                tree_closed = True
            else:
                _terminate_process_tree(
                    process, windows_job=windows_job,
                    linux_cgroup=linux_cgroup,
                )
                tree_closed = True
        reader_deadline = time.monotonic() + 3
        for reader in started_readers:
            reader.join(timeout=max(0.0, reader_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in started_readers):
            with state_lock:
                state.setdefault("read_error", "output reader did not terminate")

        return _BoundedProcessResult(
            returncode=int(
                process.returncode if process.returncode is not None else -1
            ),
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            timed_out=timed_out,
            overflow_stream=state.get("overflow_stream"),
            read_error=state.get("read_error"),
        )
    finally:
        # Couple the optional ranker's lifetime to FlexFactor even when the
        # caller is interrupted or an unexpected exception escapes polling.
        if process is not None and not tree_closed:
            try:
                if not (
                    sys.platform.startswith("linux")
                    and process.poll() is not None
                ):
                    _terminate_process_tree(
                        process, windows_job=windows_job,
                        linux_cgroup=linux_cgroup,
                    )
            except Exception:
                try:
                    if process.poll() is None:
                        if linux_cgroup is not None:
                            _kill_linux_ranker_cgroup(linux_cgroup)
                        else:
                            process.kill()
                except Exception:
                    pass
            tree_closed = True
        if process is not None:
            _close_process_pipes(process)
        for reader in started_readers:
            if reader.is_alive():
                reader.join(timeout=1)
        if linux_cgroup is not None:
            # Supervisor exit is not proof of descendant cleanup: it reports
            # status 125 when its procfs/pidfd cleanup cannot be verified.  The
            # parent owns an independent kernel boundary and must prove it
            # empty (or atomically kill it) before releasing that handle.
            _finalize_linux_ranker_cgroup(linux_cgroup)


def _isolated_tenets_environment(project_root: Path, isolation_root: Path) -> dict[str, str]:
    """Return an environment that cannot resolve helpers or state from the target.

    Tenets is advisory, but it may invoke tools such as Git and load user-level
    configuration.  The audited checkout is untrusted input, so neither its
    root nor one of its descendants may participate in executable lookup and
    Tenets receives private, disposable home/cache/config directories.
    """
    # Start from a small OS/runtime allowlist rather than forwarding provider
    # keys, GitHub tokens, proxies, or unrelated application secrets to an
    # optional third-party process.
    inherited = os.environ
    environment = {
        name: inherited[name]
        for name in (
            "LANG", "LC_ALL", "LC_CTYPE", "TZ",
            "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        )
        if name in inherited
    }
    # The pinned console script is invoked by an absolute path and needs no
    # executable lookup.  An empty PATH is materially safer than attempting to
    # classify inherited entries: a lexical target path can be a junction or
    # symlink whose resolved destination sits outside the target, and Git can
    # execute repository-local helpers such as core.fsmonitor.  With no helper
    # lookup, Tenets cannot execute either case.
    environment["PATH"] = ""

    # Do not let a target-controlled module shadow the pinned Tenets package or
    # any of its dependencies.  Python has already resolved the absolute
    # console-script interpreter before these variables matter.
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    # Tenets rank 0.13.3 does not require Git.  Make GitPython resolve a private
    # nonexistent executable and suppress every ambient configuration tier.
    # This prevents executable local config (notably core.fsmonitor) from
    # running outside FlexFactor's command broker if Tenets probes the checkout.
    environment["GIT_PYTHON_GIT_EXECUTABLE"] = str(isolation_root / "git-disabled")
    environment["GIT_PYTHON_REFRESH"] = "quiet"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_TERMINAL_PROMPT"] = "0"
    for name, relative in (
        ("HOME", "home"),
        ("USERPROFILE", "home"),
        ("APPDATA", "config"),
        ("LOCALAPPDATA", "cache"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("TMP", "tmp"),
        ("TEMP", "tmp"),
        ("TMPDIR", "tmp"),
    ):
        directory = isolation_root / relative
        directory.mkdir(parents=True, exist_ok=True)
        environment[name] = str(directory)
    return environment


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    return timeout


def generate_tenets_context(
    project: str | os.PathLike[str],
    task: str,
    *,
    output: str | os.PathLike[str] | None = None,
    top: int = DEFAULT_TOP,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    executable: str | None = None,
) -> TenetsContextResult:
    """Run ``tenets rank`` and persist a safe, bounded context manifest.

    Operational tool failures return ``unavailable`` or ``degraded`` evidence
    and do not raise. Invalid input raises ``ValueError``.
    """
    root = Path(project).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise ValueError(f"project must be an existing directory: {root}")
    task_text = str(task or "").strip()
    if not task_text:
        raise ValueError("task must not be empty")
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= MAX_TOP:
        raise ValueError(f"top must be an integer between 1 and {MAX_TOP}")
    timeout = _validated_timeout(timeout_seconds)

    output_path = _resolve_output_path(root, output)
    started = time.monotonic()
    resolved_executable = executable or _find_tenets_executable()
    explicit_untrusted = bool(executable and not _is_trusted_tenets_executable(executable))
    if explicit_untrusted:
        resolved_executable = None
    installed_version = _tenets_distribution_version(resolved_executable)
    if resolved_executable and installed_version != TENETS_VERSION:
        resolved_executable = None
    command: tuple[str, ...] = ()
    status = "unavailable"
    if installed_version and installed_version != TENETS_VERSION:
        message = (
            f"Tenets version mismatch: expected {TENETS_VERSION}, found {installed_version}; "
            "ranking disabled so an untested tool cannot influence audit order."
        )
    elif explicit_untrusted:
        message = "Tenets executable is outside the active Python installation; ranking disabled."
    else:
        message = (
            f"Tenets {TENETS_VERSION} is not installed; install FlexFactor with "
            "the context extra (or the all extra) to enable local file ranking."
        )
    files: tuple[RankedFile, ...] = ()

    if resolved_executable:
        command = (
            str(resolved_executable),
            "rank",
            task_text,
            str(root),
            "--no-git",
            "--top",
            str(top),
            "--format",
            "json",
        )
        temporary_output = None
        cleanup_error: Exception | None = None
        try:
            temporary_output = tempfile.TemporaryDirectory(prefix="flexfactor-tenets-rank-")
            isolation_root = Path(temporary_output.name)
            completed = _run_bounded_process(
                command,
                # Never make an untrusted checkout the current directory of a
                # process outside FlexFactor's execution broker.  In
                # particular, Windows searches the current directory while
                # resolving helpers such as git.exe.
                cwd=isolation_root,
                timeout_seconds=timeout,
                env=_isolated_tenets_environment(root, isolation_root),
            )
            stdout_bytes = completed.stdout
            stderr_bytes = completed.stderr
            if completed.timed_out:
                status = "degraded"
                message = f"Tenets exceeded the {timeout:g}-second timeout."
            elif completed.overflow_stream:
                limit = (
                    MAX_STDOUT_BYTES
                    if completed.overflow_stream == "stdout"
                    else MAX_STDERR_BYTES
                )
                status = "degraded"
                message = (
                    f"Tenets {completed.overflow_stream} exceeded the "
                    f"{limit}-byte safety limit."
                )
            elif completed.read_error:
                status = "degraded"
                message = _bounded_text(
                    f"Tenets output could not be read safely: {completed.read_error}"
                )
            else:
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                if completed.returncode != 0:
                    stdout_diagnostic = stdout_bytes.decode("utf-8", errors="replace")
                    status = "degraded"
                    message = _bounded_text(
                        f"Tenets exited with status {completed.returncode}: {stderr or stdout_diagnostic}"
                    )
                else:
                    payload_bytes = stdout_bytes
                    payload_text = payload_bytes.decode("utf-8", errors="strict")
                    payload = json.loads(payload_text)
                    files = _parse_ranked_files(payload, root, top)
                    if files:
                        status = "ok"
                        message = f"Tenets ranked {len(files)} safe in-repository file(s)."
                    else:
                        status = "degraded"
                        message = "Tenets returned no safe in-repository file paths."
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            status = "degraded"
            message = _bounded_text(f"Tenets returned unusable output: {exc}")
        except OSError as exc:
            status = "degraded"
            message = _bounded_text(f"Tenets could not start: {exc}")
        finally:
            if temporary_output is not None:
                try:
                    temporary_output.cleanup()
                except Exception as exc:
                    cleanup_error = exc
        if cleanup_error is not None:
            status = "degraded"
            message = _bounded_text(
                f"{message} Temporary isolation cleanup failed: {cleanup_error}"
            )

    duration = round(max(0.0, time.monotonic() - started), 6)
    result = TenetsContextResult(
        schema_version=1,
        tool="tenets",
        expected_version=TENETS_VERSION,
        adapter_version=2,
        status=status,
        project_root=str(root),
        task=task_text,
        files=files,
        message=message,
        duration_seconds=duration,
        output_path=str(output_path),
        command=command,
    )
    try:
        _atomic_write_json(output_path, result.to_dict())
    except OSError as exc:
        # Optional context ranking must not abort an audit because evidence storage failed.
        result = replace(
            result,
            status="degraded",
            message=_bounded_text(f"{result.message} Evidence write failed: {exc}"),
            output_path="",
        )
    return result


_CACHE_SKIP_DIRECTORIES = frozenset({
    ".git", ".next", ".venv", "venv", "node_modules", "dist", "build",
    "coverage", "__pycache__", ".pytest_cache", ".ruff_cache",
})


def _entry_is_traversal_boundary(entry: os.DirEntry[str], stat_result: os.stat_result) -> bool:
    """Treat every link/reparse directory as data, never as a walk edge."""
    if entry.is_symlink():
        return True
    attributes = int(getattr(stat_result, "st_file_attributes", 0) or 0)
    if attributes & int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _repository_state_fingerprint(project_root: Path) -> str:
    """Fingerprint reviewable tree metadata so post-mutation rankings expire.

    File size, nanosecond mtime/ctime, mode, and symlink destination make this
    inexpensive compared with ranking while still changing for content edits,
    creates, deletes, renames, and permission changes. Artifact/dependency
    directories excluded by the canonical sweep are excluded here as well.
    """
    digest = hashlib.sha256()
    pending = [project_root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            relative = directory.relative_to(project_root).as_posix()
            digest.update(f"!scan:{relative}:{type(exc).__name__}\n".encode())
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(project_root).as_posix()
            try:
                stat = entry.stat(follow_symlinks=False)
                digest.update(
                    (
                        f"{relative}\0{stat.st_mode}\0{stat.st_size}\0"
                        f"{stat.st_mtime_ns}\0{stat.st_ctime_ns}\n"
                    ).encode("utf-8", errors="surrogateescape")
                )
                if _entry_is_traversal_boundary(entry, stat):
                    try:
                        target = os.readlink(path)
                    except OSError:
                        target = "[reparse-boundary]"
                    digest.update(target.encode(
                        "utf-8", errors="surrogateescape"))
                elif entry.is_dir(follow_symlinks=False) \
                        and entry.name not in _CACHE_SKIP_DIRECTORIES:
                    pending.append(path)
            except OSError as exc:
                digest.update(
                    f"!stat:{relative}:{type(exc).__name__}\n".encode(
                        "utf-8", errors="surrogateescape"
                    )
                )
    return digest.hexdigest()


def cached_tenets_context(
    project: str | os.PathLike[str],
    task: str,
    *,
    top: int = DEFAULT_TOP,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TenetsContextResult:
    root = Path(project).expanduser().resolve(strict=False)
    timeout = _validated_timeout(timeout_seconds)
    fingerprint = _repository_state_fingerprint(root)
    key = (str(root), str(task).strip(), top, timeout, fingerprint)
    with _RESULT_CACHE_LOCK:
        hit = _RESULT_CACHE.get(key)
    if hit is not None:
        return hit
    result = generate_tenets_context(root, task, top=top, timeout_seconds=timeout)
    with _RESULT_CACHE_LOCK:
        return _RESULT_CACHE.setdefault(key, result)


def _argv_values(args: Sequence[str], flag: str) -> list[str]:
    """Return every non-empty value supplied for one CLI flag, in order."""
    values: list[str] = []
    prefix = flag + "="
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            value = str(args[index + 1]).strip()
            if value:
                values.append(value)
        elif isinstance(item, str) and item.startswith(prefix):
            value = item.partition("=")[2].strip()
            if value:
                values.append(value)
    return values


def _program_identity(value: str | os.PathLike[str]) -> str:
    """Portable identity used only to associate a prompt with its target root."""
    text = str(value or "").strip()
    if not text:
        return ""
    name = Path(text).name or text
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _resolved_cli_program_dir(program: str) -> Path | None:
    """Resolve every CLI-supported program form through FlexFactor's resolver."""
    try:
        import flexfactor

        resolver = getattr(flexfactor, "resolve_project_dir", None)
        if not callable(resolver):
            return None
        cleaned = str(program or "").strip().strip('"')
        if not cleaned:
            return None
        hint = cleaned.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        hint = re.sub(r"\.lnk$", "", hint, flags=re.IGNORECASE)
        resolved = resolver(cleaned, hint)
        if not resolved:
            return None
        candidate = Path(resolved).expanduser().resolve(strict=True)
        return candidate if candidate.is_dir() else None
    except (ImportError, OSError, TypeError, ValueError):
        return None


def _program_candidates(programs: Sequence[str]) -> list[tuple[int, str, str, str]]:
    candidates: list[tuple[int, str, str, str]] = []
    for index, program in enumerate(programs):
        resolved = _resolved_cli_program_dir(program)
        cleaned = str(program or "").strip().strip('"')
        if resolved is not None:
            candidate_full = os.path.normcase(str(resolved))
            identity = _program_identity(program)
        else:
            # A URL, shortcut, or path-like alias has an authoritative resolver.
            # If that resolver rejects it, its coincidental trailing basename
            # must not fall through to another checkout's owner prompt.  Keep
            # the historical normalized-name fallback only for a genuinely
            # fuzzy display name such as "Family Stewardship".
            structured_alias = (
                cleaned.casefold().startswith(("http://", "https://"))
                or cleaned.casefold().endswith(".lnk")
                or "/" in cleaned
                or "\\" in cleaned
            )
            candidate_full = ""
            identity = "" if structured_alias else _program_identity(program)
        candidates.append(
            (index, program, candidate_full, identity)
        )
    return candidates


def _matching_program_index(
    programs: Sequence[str], project: str | os.PathLike[str]
) -> int | None:
    root = Path(project).expanduser().resolve(strict=False)
    root_full = os.path.normcase(str(root))
    candidates = _program_candidates(programs)
    exact = [index for index, _program, full, _identity in candidates
             if full == root_full]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    root_identity = _program_identity(root.name)
    identity_matches = [
        index for index, _program, _full, identity in candidates
        if identity and identity == root_identity
    ]
    return identity_matches[0] if len(identity_matches) == 1 else None


def _routed_session_task(
    prompt: str,
    programs: Sequence[str],
    project: str | os.PathLike[str],
) -> str | None:
    """Return only the session instruction routed to this exact target."""
    selected = _matching_program_index(programs, project)
    if selected is None:
        return None
    candidates = _program_candidates(programs)
    targets = [(program, full) for _index, program, full, _identity in candidates]
    try:
        import flexfactor_steering

        routed = flexfactor_steering.route_session_prompt(prompt, targets)
    except (ImportError, OSError, ValueError):
        return None
    _index, selected_program, selected_full, _identity = candidates[selected]
    for route in routed.get("routes", ()):
        if not isinstance(route, Mapping):
            continue
        if str(route.get("program") or "").casefold() != selected_program.casefold():
            continue
        if os.path.normcase(str(route.get("project_dir") or "")) != selected_full:
            continue
        instruction = str(route.get("instruction") or "").strip()
        return instruction or None
    return None


def _default_task(mode: str) -> str:
    return (
        f"{mode} this application for production readiness: prioritize broken user journeys, "
        "security and privacy defects, release blockers, failure handling, and missing tests"
    )


def _durable_guidance_task(project: str | os.PathLike[str]) -> str | None:
    """Load exact-project standing owner guidance before using generic scope."""
    try:
        import flexfactor_steering

        guidance = flexfactor_steering.get_guidance_for_project(
            str(Path(project).expanduser().resolve(strict=True))
        )
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if not isinstance(guidance, Mapping):
        return None
    prompt = str(guidance.get("prompt") or "").strip()
    return prompt or None


def _argv_task(
    argv: Sequence[str] | None,
    *,
    project: str | os.PathLike[str] | None = None,
) -> str:
    override = os.environ.get("FLEXFACTOR_TENETS_TASK", "").strip()
    if override:
        return override
    args = list(argv or ())
    mode = next(
        (item for item in args if item in {"refactor", "scout", "audit", "prodready"}),
        "audit",
    )
    programs = _argv_values(args, "--program")

    session_prompts = _argv_values(args, "--session-prompt")
    if not session_prompts:
        environment_session = os.environ.get("FLEXFACTOR_SESSION_PROMPT", "").strip()
        if environment_session:
            session_prompts = [environment_session]
    if session_prompts:
        if project is None or not programs:
            return session_prompts[0]
        routed_task = _routed_session_task(session_prompts[0], programs, project)
        return routed_task or _default_task(mode)

    guiding_prompts = _argv_values(args, "--guiding-prompt")
    if not guiding_prompts:
        environment_guidance = os.environ.get("FLEXFACTOR_GUIDING_PROMPT", "").strip()
        if environment_guidance:
            guiding_prompts = [environment_guidance]
    if guiding_prompts:
        if project is not None and programs:
            # Exact resolved paths are authoritative.  A basename/slug is only
            # a fallback when it identifies exactly one program; two checkouts
            # named "app" must never receive each other's repair objective.
            selected = _matching_program_index(programs, project)
            if selected is not None and selected < len(guiding_prompts):
                return guiding_prompts[selected]
        elif len(guiding_prompts) == 1:
            return guiding_prompts[0]

    goals = _argv_values(args, "--goal")
    if goals:
        return goals[0]
    if project is not None:
        durable = _durable_guidance_task(project)
        if durable:
            return durable
    return _default_task(mode)

def _infer_project_root(args: Sequence[Any], kwargs: Mapping[str, Any]) -> Path | None:
    for key in ("project_dir", "project", "root", "program"):
        value = kwargs.get(key)
        if isinstance(value, (str, os.PathLike)):
            candidate = Path(value).expanduser().resolve(strict=False)
            if candidate.is_dir():
                return candidate
    for value in args:
        if isinstance(value, (str, os.PathLike)):
            candidate = Path(value).expanduser().resolve(strict=False)
            if candidate.is_dir():
                return candidate
    return None


def _prioritize_paths(
    source_files: Any,
    ranked_files: Sequence[RankedFile],
    canonicalize: Any = None,
) -> Any:
    if not isinstance(source_files, (list, tuple)) or not source_files:
        return source_files
    if not all(isinstance(item, str) for item in source_files):
        return source_files

    def canon(value: str) -> str:
        if callable(canonicalize):
            try:
                return str(canonicalize(value))
            except Exception:
                pass
        return value.replace("\\", "/").removeprefix("./")

    priority = {canon(item.path): index for index, item in enumerate(ranked_files)}
    fallback = len(priority) + len(source_files) + 1
    ordered = [
        value
        for _, value in sorted(
            enumerate(source_files),
            key=lambda pair: (priority.get(canon(pair[1]), fallback), pair[0]),
        )
    ]
    return tuple(ordered) if isinstance(source_files, tuple) else ordered


def _positive_cap(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _limit_paths(source_files: Any, cap: int | None) -> Any:
    if cap is None or not isinstance(source_files, (list, tuple)):
        return source_files
    limited = source_files[:cap]
    return tuple(limited) if isinstance(source_files, tuple) else list(limited)


def _call_with_lifted_cap(
    prior: Any,
    module_globals: MutableMapping[str, Any],
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> tuple[Any, int | None]:
    """Call the canonical enumerator before its cap, then report that cap."""
    try:
        signature = inspect.signature(prior)
        bound = signature.bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        signature = None
        bound = None

    if bound is not None and signature is not None:
        for name in _CAP_PARAMETER_NAMES:
            parameter = signature.parameters.get(name)
            if parameter is None:
                continue
            supplied = bound.arguments.get(name, parameter.default)
            cap = _positive_cap(supplied)
            if cap is None:
                continue
            if cap >= _UNBOUNDED_ENUMERATION_LIMIT:
                return prior(*args, **kwargs), cap
            bound.arguments[name] = _UNBOUNDED_ENUMERATION_LIMIT
            return prior(*bound.args, **bound.kwargs), cap

    for name in _CAP_GLOBAL_NAMES:
        cap = _positive_cap(module_globals.get(name))
        if cap is None:
            continue
        if cap >= _UNBOUNDED_ENUMERATION_LIMIT:
            return prior(*args, **kwargs), cap
        with _ENUMERATION_LOCK:
            original = module_globals[name]
            module_globals[name] = _UNBOUNDED_ENUMERATION_LIMIT
            try:
                return prior(*args, **kwargs), cap
            finally:
                module_globals[name] = original

    return prior(*args, **kwargs), None


def install(module_globals: MutableMapping[str, Any], *, argv: Sequence[str] | None = None) -> None:
    """Idempotently prioritize canonical audit candidates without changing membership."""
    with _INSTALL_LOCK:
        # A long-lived embedder may call run_cli more than once.  Refresh the
        # effective arguments even after the wrapper is installed so each run's
        # explicit task, rather than the importing host's sys.argv, is used.
        module_globals["_FLEXFACTOR_TENETS_ARGV"] = tuple(argv or ())
        if module_globals.get("_FLEXFACTOR_TENETS_INSTALLED"):
            return
        module_globals["_FLEXFACTOR_TENETS_INSTALLED"] = True
        prior_enum = module_globals.get("_enumerate_source_files")
        prior_manifest = module_globals.get("_repository_review_manifest")
        if not callable(prior_enum) and not callable(prior_manifest):
            return
        canonicalize = module_globals.get("_canon_rel")

        def context_for(root: Path) -> TenetsContextResult | None:
            if not enabled():
                return None
            try:
                task = _argv_task(
                    module_globals.get("_FLEXFACTOR_TENETS_ARGV"), project=root
                )
                result = cached_tenets_context(root, task)
            except Exception as exc:
                module_globals["_TENETS_CONTEXT_LAST_ERROR"] = _bounded_text(str(exc))
                return None
            module_globals["_TENETS_CONTEXT_LAST"] = result.to_dict()
            return result if result.status == "ok" else None

        if callable(prior_enum):
            def enumerate_source_files(*args: Any, **kwargs: Any) -> Any:
                root = _infer_project_root(args, kwargs)
                if root is None:
                    return prior_enum(*args, **kwargs)
                result = context_for(root)
                if result is None:
                    return prior_enum(*args, **kwargs)
                try:
                    source_files, cap = _call_with_lifted_cap(
                        prior_enum, module_globals, args, kwargs
                    )
                except Exception as exc:
                    message = _bounded_text(
                        f"uncapped enumeration failed; original order preserved: {exc}"
                    )
                    degraded = result.to_dict()
                    degraded["status"] = "degraded"
                    degraded["message"] = message
                    module_globals["_TENETS_CONTEXT_LAST"] = degraded
                    module_globals["_TENETS_CONTEXT_LAST_ERROR"] = message
                    return prior_enum(*args, **kwargs)
                prioritized = _prioritize_paths(source_files, result.files, canonicalize)
                return _limit_paths(prioritized, cap)

            enumerate_source_files._tenets_wrapped = True  # type: ignore[attr-defined]
            module_globals["_enumerate_source_files"] = enumerate_source_files

        if callable(prior_manifest):
            def repository_review_manifest(*args: Any, **kwargs: Any) -> Any:
                manifest = prior_manifest(*args, **kwargs)
                if not isinstance(manifest, Mapping):
                    return manifest
                root = _infer_project_root(args, kwargs)
                if root is None:
                    return manifest
                result = context_for(root)
                if result is None:
                    return manifest
                reviewable = manifest.get("reviewable_files")
                prioritized = _prioritize_paths(reviewable, result.files, canonicalize)
                if prioritized is reviewable:
                    return manifest
                updated = dict(manifest)
                updated["reviewable_files"] = prioritized
                return updated

            repository_review_manifest._tenets_wrapped = True  # type: ignore[attr-defined]
            module_globals["_repository_review_manifest"] = repository_review_manifest

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flexfactor-context",
        description="Generate a local Tenets file-ranking manifest for a FlexFactor task.",
    )
    parser.add_argument("project", help="Existing project directory to analyze")
    parser.add_argument("task", help="Concrete audit, repair, or review objective")
    parser.add_argument(
        "--output",
        help="JSON manifest path (default: ~/.flexfactor/context/<project>/tenets-context.json)",
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"Maximum files, 1-{MAX_TOP}")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Tenets subprocess timeout in seconds",
    )
    parser.add_argument("--strict", action="store_true", help="Return non-zero unless Tenets status is ok")
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_tenets_context(
            args.project,
            args.task,
            output=args.output,
            top=args.top,
            timeout_seconds=args.timeout,
        )
    except ValueError as exc:
        print(f"flexfactor-context: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if result.status == "ok" or not args.strict else 1


def _run_linux_supervisor_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cgroup", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--max-runtime", required=True, type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(list(argv))
        boundary = Path(args.cgroup).resolve(strict=True)
        system_root = Path("/sys/fs/cgroup").resolve(strict=True)
        boundary.relative_to(system_root)
        command = list(args.command)
        if command[:1] == ["--"]:
            command = command[1:]
    except (OSError, ValueError, SystemExit):
        return 125
    return _linux_supervise_command(
        command,
        cgroup_boundary=boundary,
        expected_parent_pid=args.parent_pid,
        max_runtime_seconds=args.max_runtime,
    )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_linux-subprocess-supervisor"]:
        raise SystemExit(_run_linux_supervisor_cli(sys.argv[2:]))
    raise SystemExit(run_cli())
