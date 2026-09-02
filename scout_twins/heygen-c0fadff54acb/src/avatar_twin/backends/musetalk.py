from __future__ import annotations

from pathlib import Path
import json

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


class MuseTalkBackend:
    name = "musetalk"
    model = "TMElyralab/MuseTalk-1.5"

    def __init__(self, spec: ProviderSpec, runner: CommandRunner | None = None) -> None:
        self.spec = spec
        self.runner = runner or CommandRunner()

    def _paths(self) -> tuple[Path, str, Path, Path]:
        repo = require_directory(self.spec.repo, "MuseTalk repository")
        require_file(repo / "scripts/inference.py", "MuseTalk inference entrypoint")
        python = resolve_executable(self.spec.python)
        model_root = Path(self.spec.checkpoint or repo / "models").expanduser().resolve()
        unet = require_file(
            self.spec.options.get("unet_model_path") or model_root / "musetalkV15/unet.pth",
            "MuseTalk 1.5 UNet",
        )
        config = require_file(
            self.spec.options.get("unet_config") or model_root / "musetalkV15/musetalk.json",
            "MuseTalk 1.5 UNet config",
        )
        return repo, python, unet, config

    def readiness(self) -> dict:
        try:
            repo, python, unet, config = self._paths()
            return {
                "ready": True,
                "provider": self.name,
                "repo": str(repo),
                "python": python,
                "unet": str(unet),
                "config": str(config),
            }
        except Exception as exc:
            return {"ready": False, "provider": self.name, "reason": str(exc)}

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        repo, python, unet, unet_config = self._paths()
        source = job.driving_video or job.avatar_image
        result_dir = job.output_dir / "musetalk"
        result_dir.mkdir(parents=True, exist_ok=True)
        inference_config = job.output_dir / "musetalk-job.yaml"
        # JSON string literals are valid YAML scalars and prevent path injection.
        inference_config.write_text(
            "job_0:\n"
            f"  video_path: {json.dumps(str(source))}\n"
            f"  audio_path: {json.dumps(str(job.audio))}\n",
            encoding="utf-8",
        )
        before = snapshot_mp4s(result_dir)
        started_ns = start_timestamp_ns()
        ffmpeg_path = str(self.spec.options.get("ffmpeg_path") or Path(resolve_executable("ffmpeg")).parent)
        command = [
            python,
            "-m", "scripts.inference",
            "--inference_config", str(inference_config),
            "--result_dir", str(result_dir),
            "--unet_model_path", str(unet),
            "--unet_config", str(unet_config),
            "--version", "v15",
            "--ffmpeg_path", ffmpeg_path,
        ]
        if bool(self.spec.options.get("use_float16", False)):
            command.append("--use_float16")
        if "bbox_shift" in self.spec.options:
            command.extend(["--bbox_shift", str(int(self.spec.options["bbox_shift"]))])
        receipt = self.runner.run(
            command,
            cwd=repo,
            receipt_dir=job.output_dir / "receipts",
            label="musetalk",
            timeout_s=self.spec.timeout_s,
        )
        generated = find_new_mp4([result_dir], before, started_ns=started_ns)
        destination = stage_model_output(generated, job.output_dir / "model-video.mp4")
        return BackendArtifact(
            provider=self.name,
            model=self.model,
            video_path=destination,
            receipts=(receipt.to_dict(),),
            metadata={"source": str(generated), "mode": "lip_sync", "input_video": str(source)},
        )
