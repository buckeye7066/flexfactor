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
from ..runtime import CommandRunner, require_directory, require_file, resolve_executable


class SadTalkerBackend:
    name = "sadtalker"
    model = "OpenTalker/SadTalker"

    def __init__(self, spec: ProviderSpec, runner: CommandRunner | None = None) -> None:
        self.spec = spec
        self.runner = runner or CommandRunner()

    def _paths(self) -> tuple[Path, str]:
        repo = require_directory(self.spec.repo, "SadTalker repository")
        require_file(repo / "inference.py", "SadTalker inference entrypoint")
        checkpoints = Path(self.spec.checkpoint or repo / "checkpoints").expanduser().resolve()
        require_directory(checkpoints, "SadTalker checkpoint")
        return repo, resolve_executable(self.spec.python)

    def readiness(self) -> dict:
        try:
            repo, python = self._paths()
            return {"ready": True, "provider": self.name, "repo": str(repo), "python": python}
        except Exception as exc:
            return {"ready": False, "provider": self.name, "reason": str(exc)}

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        repo, python = self._paths()
        result_dir = job.output_dir / "sadtalker"
        result_dir.mkdir(parents=True, exist_ok=True)
        before = snapshot_mp4s(result_dir)
        started_ns = start_timestamp_ns()
        command = [
            python,
            str(repo / "inference.py"),
            "--driven_audio", str(job.audio),
            "--source_image", str(job.avatar_image),
            "--result_dir", str(result_dir),
            "--still",
            "--preprocess", str(self.spec.options.get("preprocess") or "full"),
        ]
        enhancer = str(self.spec.options.get("enhancer") or "gfpgan").strip()
        if enhancer:
            command.extend(["--enhancer", enhancer])
        if bool(self.spec.options.get("use_cpu", False)):
            command.append("--cpu")
        receipt = self.runner.run(
            command,
            cwd=repo,
            receipt_dir=job.output_dir / "receipts",
            label="sadtalker",
            timeout_s=self.spec.timeout_s,
        )
        generated = find_new_mp4([result_dir], before, started_ns=started_ns)
        destination = stage_model_output(generated, job.output_dir / "model-video.mp4")
        return BackendArtifact(
            provider=self.name,
            model=self.model,
            video_path=destination,
            receipts=(receipt.to_dict(),),
            metadata={"source": str(generated), "mode": "audio_driven_single_image"},
        )
