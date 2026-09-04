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
import signal
import subprocess
import sys
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
_RESULT_CACHE: dict[tuple[str, str, int, float], "TenetsContextResult"] = {}


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
        resolved = combined.resolve(strict=False)
        relative = resolved.relative_to(project_root)
    except (OSError, ValueError):
        return None
    normalized = relative.as_posix()
    if normalized in ("", "."):
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
    return _state_root() / "context" / f"{slug}-{identity}" / "tenets-context.json"


def _resolve_output_path(project_root: Path, output: str | os.PathLike[str] | None) -> Path:
    if output is None:
        return _default_output_path(project_root).resolve(strict=False)
    candidate = Path(output).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


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


def _tenets_distribution_version() -> str | None:
    try:
        return importlib_metadata.version("tenets")
    except Exception:
        # Distribution metadata is optional input.  A partially installed
        # package/backend can raise filesystem, decoding, archive, parser, or
        # other ordinary metadata errors here; none may turn an unavailable
        # advisory ranker into a traceback that aborts the production audit.
        # BaseException subclasses still propagate, so interrupts are honored.
        return None


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
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
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
                signal.pidfd_send_signal(pidfd, chosen_signal)
            finally:
                os.close(pidfd)
        time.sleep(0.01)
    _reap_linux_children()
    return not _linux_direct_child_pids()


def _linux_supervise_command(command: Sequence[str]) -> int:
    """Run one command behind a subreaper that owns orphaned descendants.

    This function executes only in the isolated supervisor subprocess started
    below. Keeping the subreaper out of the long-lived FlexFactor process
    avoids adopting or signalling unrelated concurrent tools.
    """
    if not command:
        return 125
    try:
        _enable_linux_child_subreaper()
        # Prove procfs inventory is available before any optional ranker code
        # executes. Without it the supervisor cannot safely enumerate adoptees.
        _linux_direct_child_pids()
        # A pidfd makes cleanup immune to PID reuse between inventory and
        # signalling. Refuse to launch if the running kernel lacks this piece
        # of the containment boundary.
        self_pidfd = os.pidfd_open(os.getpid())
        os.close(self_pidfd)
    except OSError as exc:
        print(f"flexfactor Tenets containment unavailable: {exc}", file=sys.stderr)
        return 125

    received_signal: list[int | None] = [None]

    def _request_stop(signum, _frame) -> None:
        received_signal[0] = int(signum)

    for handled in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(handled, _request_stop)
    try:
        child = subprocess.Popen(tuple(command), shell=False)
    except OSError as exc:
        print(f"flexfactor could not start Tenets: {exc}", file=sys.stderr)
        return 125

    stop_started: float | None = None
    while True:
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

    try:
        cleaned = _terminate_linux_supervisor_children()
    except OSError as exc:
        print(f"flexfactor could not verify Tenets descendant cleanup: {exc}",
              file=sys.stderr)
        return 125
    if not cleaned:
        print("flexfactor could not terminate every Tenets descendant",
              file=sys.stderr)
        return 125
    if received_signal[0] is not None:
        return 128 + received_signal[0]
    if returncode < 0:
        return 128 + abs(returncode)
    return min(returncode, 255)


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
    process: subprocess.Popen[bytes], *, windows_job: int | None = None
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
    windows_job: int | None = None
    if os.name == "nt":
        try:
            windows_job = _contain_and_resume_windows_process(process)
        except Exception as exc:
            # Do not run an optional helper without the containment required
            # to clean up descendants after the direct ranker exits.
            _terminate_process_tree(process)
            _close_process_pipes(process)
            raise OSError(f"could not contain Tenets in a Windows Job Object: {exc}") from exc
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

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    tree_closed = False
    while process.poll() is None:
        if overflow_event.wait(timeout=0.02):
            _terminate_process_tree(process, windows_job=windows_job)
            tree_closed = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_tree(process, windows_job=windows_job)
            tree_closed = True
            break
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            continue

    # Always close the containment boundary.  The direct process can exit zero
    # while a descendant keeps inherited output pipes (or other work) alive.
    # The retained POSIX PGID / Windows Job Object still identifies that tree.
    if not tree_closed:
        _terminate_process_tree(process, windows_job=windows_job)
    reader_deadline = time.monotonic() + 3
    for reader in readers:
        reader.join(timeout=max(0.0, reader_deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        with state_lock:
            state.setdefault("read_error", "output reader did not terminate")

    return _BoundedProcessResult(
        returncode=int(process.returncode if process.returncode is not None else -1),
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
        timed_out=timed_out,
        overflow_stream=state.get("overflow_stream"),
        read_error=state.get("read_error"),
    )


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
    installed_version = _tenets_distribution_version()
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
        try:
            temporary_output = tempfile.TemporaryDirectory(prefix="flexfactor-tenets-rank-")
            rank_output = Path(temporary_output.name) / "ranked-files.json"
            invocation_command = command + ("--output", str(rank_output))
            isolation_root = Path(temporary_output.name)
            completed = _run_bounded_process(
                invocation_command,
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
                    if rank_output.is_file():
                        with rank_output.open("rb") as handle:
                            payload_bytes = handle.read(MAX_STDOUT_BYTES + 1)
                        if len(payload_bytes) > MAX_STDOUT_BYTES:
                            raise ValueError(
                                f"Tenets JSON output exceeded the {MAX_STDOUT_BYTES}-byte safety limit"
                            )
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
                temporary_output.cleanup()

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
            message=_bounded_text(f"{result.message} Evidence write failed: {exc}"),
            output_path="",
        )
    return result


def cached_tenets_context(
    project: str | os.PathLike[str],
    task: str,
    *,
    top: int = DEFAULT_TOP,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TenetsContextResult:
    root = Path(project).expanduser().resolve(strict=False)
    timeout = _validated_timeout(timeout_seconds)
    key = (str(root), str(task).strip(), top, timeout)
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


def _program_candidates(programs: Sequence[str]) -> list[tuple[int, str, str, str]]:
    candidates: list[tuple[int, str, str, str]] = []
    for index, program in enumerate(programs):
        candidate = Path(program).expanduser()
        try:
            candidate_full = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            candidate_full = os.path.normcase(str(candidate.absolute()))
        candidates.append(
            (index, program, candidate_full, _program_identity(program))
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
    if exact:
        return exact[0]
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
    if session_prompts:
        if project is None or not programs:
            return session_prompts[0]
        routed_task = _routed_session_task(session_prompts[0], programs, project)
        return routed_task or _default_task(mode)

    guiding_prompts = _argv_values(args, "--guiding-prompt")
    if guiding_prompts:
        if project is not None and programs:
            # Exact resolved paths are authoritative.  A basename/slug is only
            # a fallback when it identifies exactly one program; two checkouts
            # named "app" must never receive each other's repair objective.
            selected = _matching_program_index(programs, project)
            if selected is not None and selected < len(guiding_prompts):
                return guiding_prompts[selected]
        if len(guiding_prompts) == 1:
            return guiding_prompts[0]

    goals = _argv_values(args, "--goal")
    if goals:
        return goals[0]
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


if __name__ == "__main__":
    if sys.argv[1:3] == ["--_linux-subprocess-supervisor", "--"]:
        raise SystemExit(_linux_supervise_command(sys.argv[3:]))
    raise SystemExit(run_cli())
