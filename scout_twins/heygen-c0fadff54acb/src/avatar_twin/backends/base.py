from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
import shutil
import time

from ..models import ValidationError


@dataclass(frozen=True, slots=True)
class AvatarRenderRequest:
    job_id: str
    avatar_image: Path
    audio: Path
    output_dir: Path
    driving_video: Path | None = None
    prompt: str = ""
    expected_duration_s: float | None = None
    mode: str = "talking_avatar"
    options: dict[str, Any] = field(default_factory=dict)

    def validated(self) -> "AvatarRenderRequest":
        if not self.job_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in self.job_id):
            raise ValidationError("avatar render job_id contains unsafe characters")
        avatar = self.avatar_image.expanduser().resolve()
        audio = self.audio.expanduser().resolve()
        driving = self.driving_video.expanduser().resolve() if self.driving_video else None
        output = self.output_dir.expanduser().resolve()
        if not avatar.is_file():
            raise ValidationError(f"avatar image does not exist: {avatar}")
        if not audio.is_file():
            raise ValidationError(f"narration/master audio does not exist: {audio}")
        if driving and not driving.is_file():
            raise ValidationError(f"driving/performance video does not exist: {driving}")
        output.mkdir(parents=True, exist_ok=True)
        return AvatarRenderRequest(
            job_id=self.job_id,
            avatar_image=avatar,
            audio=audio,
            output_dir=output,
            driving_video=driving,
            prompt=self.prompt,
            expected_duration_s=self.expected_duration_s,
            mode=self.mode,
            options=dict(self.options),
        )


@dataclass(frozen=True, slots=True)
class BackendArtifact:
    provider: str
    model: str
    video_path: Path
    receipts: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["video_path"] = str(self.video_path)
        return value


class AvatarBackend(Protocol):
    name: str

    def readiness(self) -> dict[str, Any]: ...

    def render(self, request: AvatarRenderRequest) -> BackendArtifact: ...


def snapshot_mp4s(*roots: Path) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.mp4"):
            try:
                stat = path.stat()
            except OSError:
                continue
            found[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return found


def find_new_mp4(
    roots: list[Path],
    before: dict[str, tuple[int, int]],
    *,
    started_ns: int,
) -> Path:
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.mp4"):
            try:
                stat = path.stat()
            except OSError:
                continue
            previous = before.get(str(path.resolve()))
            if stat.st_size > 0 and (previous != (stat.st_mtime_ns, stat.st_size) or stat.st_mtime_ns >= started_ns):
                candidates.append(path.resolve())
    if not candidates:
        raise RuntimeError("model command completed but produced no new MP4 artifact")
    candidates.sort(
        key=lambda path: (
            "concat" not in path.stem.lower(),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        ),
        reverse=True,
    )
    return candidates[0]


def stage_model_output(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copyfile(source, destination)
    if not destination.is_file() or destination.stat().st_size < 1:
        raise RuntimeError("model output could not be staged")
    return destination


def start_timestamp_ns() -> int:
    return time.time_ns()
