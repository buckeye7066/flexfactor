from __future__ import annotations

from pathlib import Path

from .base import (
    AvatarRenderRequest,
    BackendArtifact,
    find_new_mp4,
    snapshot_mp4s,
    stage_model_output,
    start_timestamp_ns,
)
from ..configuration import ProviderSpec
from ..models import ValidationError
from ..runtime import CommandRunner, require_directory, require_file, resolve_executable


class LivePortraitBackend:
    name = "liveportrait"
    model = "KlingAIResearch/LivePortrait"

    def __init__(self, spec: ProviderSpec, runner: CommandRunner | None = None) -> None:
        self.spec = spec
        self.runner = runner or CommandRunner()

    def _paths(self) -> tuple[Path, str]:
        repo = require_directory(self.spec.repo, "LivePortrait repository")
        require_file(repo / "inference.py", "LivePortrait inference entrypoint")
        weights = Path(self.spec.checkpoint or repo / "pretrained_weights").expanduser().resolve()
        require_directory(weights, "LivePortrait pretrained weights")
        return repo, resolve_executable(self.spec.python)

    def readiness(self) -> dict:
        try:
            repo, python = self._paths()
            return {"ready": True, "provider": self.name, "repo": str(repo), "python": python}
        except Exception as exc:
            return {"ready": False, "provider": self.name, "reason": str(exc)}

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        if not job.driving_video:
            raise ValidationError("LivePortrait requires a driving/performance video")
        repo, python = self._paths()
        animations = repo / "animations"
        animations.mkdir(parents=True, exist_ok=True)
        before = snapshot_mp4s(animations)
        started_ns = start_timestamp_ns()
        command = [
            python,
            str(repo / "inference.py"),
            "-s", str(job.avatar_image),
            "-d", str(job.driving_video),
        ]
        if bool(self.spec.options.get("crop_driving_video", True)):
            command.append("--flag_crop_driving_video")
        if bool(self.spec.options.get("torch_compile", False)):
            command.append("--flag_do_torch_compile")
        receipt = self.runner.run(
            command,
            cwd=repo,
            receipt_dir=job.output_dir / "receipts",
            label="liveportrait",
            timeout_s=self.spec.timeout_s,
        )
        generated = find_new_mp4([animations], before, started_ns=started_ns)
        destination = stage_model_output(generated, job.output_dir / "model-video.mp4")
        return BackendArtifact(
            provider=self.name,
            model=self.model,
            video_path=destination,
            receipts=(receipt.to_dict(),),
            metadata={"source": str(generated), "mode": "portrait_motion_transfer"},
        )
