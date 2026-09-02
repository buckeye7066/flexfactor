from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence
import hashlib
import json
import os
import shutil
import subprocess
import time

from .models import ValidationError


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_executable(value: str) -> str:
    if not value.strip():
        raise ValidationError("provider executable is empty")
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise ValidationError(f"provider executable does not exist: {resolved}")
        return str(resolved)
    discovered = shutil.which(value)
    if not discovered:
        raise ValidationError(f"provider executable is not on PATH: {value}")
    return discovered


def require_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValidationError(f"{label} directory does not exist: {path}")
    return path


def require_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValidationError(f"{label} file does not exist: {path}")
    return path


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    argv: list[str]
    cwd: str
    started_at: str
    duration_s: float
    returncode: int
    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


class CommandRunner:
    """Runs explicit argument vectors; never invokes a shell or records secrets."""

    def __init__(self, *, inherited_env: Mapping[str, str] | None = None) -> None:
        self.inherited_env = dict(inherited_env or os.environ)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        receipt_dir: str | Path,
        label: str,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> CommandReceipt:
        if not argv:
            raise ValidationError("provider command is empty")
        command = [str(item) for item in argv]
        if any("\x00" in item or "\n" in item or "\r" in item for item in command):
            raise ValidationError("provider command contains a forbidden control character")
        command[0] = resolve_executable(command[0])
        workdir = require_directory(cwd, "provider working")
        logs = Path(receipt_dir).resolve()
        logs.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label).strip("-")
        if not safe_label:
            safe_label = "provider"
        stdout_path = logs / f"{safe_label}.stdout.log"
        stderr_path = logs / f"{safe_label}.stderr.log"
        runtime_env = dict(self.inherited_env)
        runtime_env.update({str(key): str(value) for key, value in (env or {}).items()})
        started_at = _utc_now()
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(workdir),
                env=runtime_env,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "", encoding="utf-8")
            raise RuntimeError(f"provider {label!r} exceeded its {timeout_s:.0f}s timeout") from exc
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        receipt = CommandReceipt(
            argv=command,
            cwd=str(workdir),
            started_at=started_at,
            duration_s=round(time.monotonic() - start, 3),
            returncode=completed.returncode,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            stdout_sha256=_sha256(stdout_path),
            stderr_sha256=_sha256(stderr_path),
        )
        receipt_path = logs / f"{safe_label}.receipt.json"
        receipt_path.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-12:]
            detail = "\n".join(tail)
            raise RuntimeError(
                f"provider {label!r} exited with code {completed.returncode}"
                + (f":\n{detail}" if detail else "")
            )
        return receipt
