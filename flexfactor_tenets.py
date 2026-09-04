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
    except importlib_metadata.PackageNotFoundError:
        return None

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


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
) -> _BoundedProcessResult:
    """Run a child while bounding both output streams during production."""
    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("output limits must be positive")
    process = subprocess.Popen(
        tuple(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
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
    while process.poll() is None:
        if overflow_event.wait(timeout=0.02):
            _terminate_process(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process(process)
            break
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            continue

    if process.poll() is None:
        _terminate_process(process)
    for reader in readers:
        reader.join(timeout=3)
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
            "--top",
            str(top),
            "--format",
            "json",
        )
        temporary_output = tempfile.TemporaryDirectory(prefix="flexfactor-tenets-rank-")
        rank_output = Path(temporary_output.name) / "ranked-files.json"
        invocation_command = command + ("--output", str(rank_output))
        try:
            completed = _run_bounded_process(
                invocation_command,
                cwd=root,
                timeout_seconds=timeout,
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
                stdout = stdout_bytes.decode("utf-8", errors="strict")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                if completed.returncode != 0:
                    status = "degraded"
                    message = _bounded_text(
                        f"Tenets exited with status {completed.returncode}: {stderr or stdout}"
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


def _argv_task(argv: Sequence[str] | None) -> str:
    override = os.environ.get("FLEXFACTOR_TENETS_TASK", "").strip()
    if override:
        return override
    args = list(argv or ())
    mode = next((item for item in args if item in {"refactor", "scout", "audit", "prodready"}), "audit")
    task_flags = ("--session-prompt", "--guiding-prompt", "--goal")
    for flag in task_flags:
        for index, item in enumerate(args):
            if item == flag and index + 1 < len(args) and args[index + 1].strip():
                return args[index + 1].strip()
            prefix = flag + "="
            if item.startswith(prefix) and item.partition("=")[2].strip():
                return item.partition("=")[2].strip()
    return (
        f"{mode} this application for production readiness: prioritize broken user journeys, "
        "security and privacy defects, release blockers, failure handling, and missing tests"
    )


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
        if module_globals.get("_FLEXFACTOR_TENETS_INSTALLED"):
            return
        module_globals["_FLEXFACTOR_TENETS_INSTALLED"] = True
        prior_enum = module_globals.get("_enumerate_source_files")
        prior_manifest = module_globals.get("_repository_review_manifest")
        if not callable(prior_enum) and not callable(prior_manifest):
            return
        task = _argv_task(argv)
        canonicalize = module_globals.get("_canon_rel")

        def context_for(root: Path) -> TenetsContextResult | None:
            if not enabled():
                return None
            try:
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
    raise SystemExit(run_cli())
