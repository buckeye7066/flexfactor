#!/usr/bin/env python3
"""Safe Tenets code-context integration for FlexFactor.

Tenets is a local-first external tool that ranks the files most relevant to a
coding objective.  FlexFactor uses the ranking only to PRIORITIZE its existing
complete source sweep; it never lets Tenets remove a file, mark a file clean, or
bypass a release gate.

The adapter is deliberately fail-open with respect to Tenets availability:
absence, timeout, malformed output, or a tool failure is recorded in a bounded
JSON evidence manifest while FlexFactor retains its original file order. Invalid
caller input still fails closed so the wrong repository is never analyzed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

TENETS_VERSION = "0.13.3"
DEFAULT_TOP = 50
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TOP = 200
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024

_DISABLE_VALUES = {"0", "false", "no", "off"}
_INSTALL_LOCK = threading.Lock()
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
    if score != score or score in (float("inf"), float("-inf")):
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
    # resolve(strict=False) already follows every existing symlink component.
    # A broken leaf is permitted only if its resolved location remains in-root.
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
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number")

    output_path = _resolve_output_path(root, output)
    started = time.monotonic()
    resolved_executable = executable or shutil.which("tenets")
    command: tuple[str, ...] = ()
    status = "unavailable"
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
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                timeout=float(timeout_seconds),
                check=False,
            )
            stdout_bytes = completed.stdout or b""
            stderr_bytes = completed.stderr or b""
            if len(stdout_bytes) > MAX_STDOUT_BYTES:
                raise ValueError(f"Tenets output exceeded {MAX_STDOUT_BYTES} bytes")
            stdout = stdout_bytes.decode("utf-8", errors="strict")
            stderr = stderr_bytes[:MAX_STDERR_BYTES].decode("utf-8", errors="replace")
            if completed.returncode != 0:
                status = "degraded"
                message = _bounded_text(
                    f"Tenets exited with status {completed.returncode}: {stderr or stdout}"
                )
            else:
                payload = json.loads(stdout)
                files = _parse_ranked_files(payload, root, top)
                if files:
                    status = "ok"
                    message = f"Tenets ranked {len(files)} safe in-repository file(s)."
                else:
                    status = "degraded"
                    message = "Tenets returned no safe in-repository file paths."
        except subprocess.TimeoutExpired:
            status = "degraded"
            message = f"Tenets exceeded the {float(timeout_seconds):g}-second timeout."
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            status = "degraded"
            message = _bounded_text(f"Tenets returned unusable output: {exc}")
        except OSError as exc:
            status = "degraded"
            message = _bounded_text(f"Tenets could not start: {exc}")

    duration = round(max(0.0, time.monotonic() - started), 6)
    result = TenetsContextResult(
        schema_version=1,
        tool="tenets",
        expected_version=TENETS_VERSION,
        adapter_version=1,
        status=status,
        project_root=str(root),
        task=task_text,
        files=files,
        message=message,
        duration_seconds=duration,
        output_path=str(output_path),
        command=command,
    )
    _atomic_write_json(output_path, result.to_dict())
    return result


def cached_tenets_context(
    project: str | os.PathLike[str],
    task: str,
    *,
    top: int = DEFAULT_TOP,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TenetsContextResult:
    root = Path(project).expanduser().resolve(strict=False)
    key = (str(root), str(task).strip(), top, float(timeout_seconds))
    with _RESULT_CACHE_LOCK:
        hit = _RESULT_CACHE.get(key)
    if hit is not None:
        return hit
    result = generate_tenets_context(root, task, top=top, timeout_seconds=timeout_seconds)
    with _RESULT_CACHE_LOCK:
        return _RESULT_CACHE.setdefault(key, result)


def _argv_task(argv: Sequence[str] | None) -> str:
    override = os.environ.get("FLEXFACTOR_TENETS_TASK", "").strip()
    if override:
        return override
    args = list(argv or ())
    mode = next((item for item in args if item in {"refactor", "scout", "audit", "prodready"}), "audit")
    for index, item in enumerate(args):
        if item == "--goal" and index + 1 < len(args) and args[index + 1].strip():
            return args[index + 1].strip()
        if item.startswith("--goal=") and item.partition("=")[2].strip():
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


def install(module_globals: MutableMapping[str, Any], *, argv: Sequence[str] | None = None) -> None:
    """Idempotently prioritize FlexFactor's complete source sweep with Tenets.

    This hook only changes order. The original enumerator still determines the
    complete file set, skip rules, clean-file memory, containment, and errors.
    """
    with _INSTALL_LOCK:
        if module_globals.get("_FLEXFACTOR_TENETS_INSTALLED"):
            return
        module_globals["_FLEXFACTOR_TENETS_INSTALLED"] = True
        prior = module_globals.get("_enumerate_source_files")
        if not callable(prior):
            return
        task = _argv_task(argv)
        canonicalize = module_globals.get("_canon_rel")

        def enumerate_source_files(*args: Any, **kwargs: Any) -> Any:
            source_files = prior(*args, **kwargs)
            if not enabled():
                return source_files
            root = _infer_project_root(args, kwargs)
            if root is None:
                return source_files
            try:
                result = cached_tenets_context(root, task)
            except Exception as exc:  # the optional ranker must never break the audit
                module_globals["_TENETS_CONTEXT_LAST_ERROR"] = _bounded_text(str(exc))
                return source_files
            module_globals["_TENETS_CONTEXT_LAST"] = result.to_dict()
            if result.status != "ok":
                return source_files
            return _prioritize_paths(source_files, result.files, canonicalize)

        enumerate_source_files._tenets_wrapped = True  # type: ignore[attr-defined]
        module_globals["_enumerate_source_files"] = enumerate_source_files


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
