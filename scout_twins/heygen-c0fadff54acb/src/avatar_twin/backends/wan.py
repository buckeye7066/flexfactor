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


def _size(value: object, default: tuple[int, int]) -> tuple[int, int]:
    if isinstance(value, str) and ("x" in value.lower() or "*" in value):
        left, right = value.lower().replace("*", "x").split("x", 1)
        width, height = int(left), int(right)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        width, height = int(value[0]), int(value[1])
    else:
        width, height = default
    if width < 256 or height < 256 or width * height > 4096 * 4096:
        raise ValidationError("Wan output size is outside supported bounds")
    return width, height


class _WanBase:
    def __init__(self, spec: ProviderSpec, runner: CommandRunner | None = None) -> None:
        self.spec = spec
        self.runner = runner or CommandRunner()

    def _paths(self) -> tuple[Path, Path, str]:
        repo = require_directory(self.spec.repo, "Wan2.2 repository")
        require_file(repo / "generate.py", "Wan2.2 generation entrypoint")
        checkpoint = require_directory(self.spec.checkpoint, "Wan2.2 checkpoint")
        return repo, checkpoint, resolve_executable(self.spec.python)

    def readiness(self) -> dict:
        try:
            repo, checkpoint, python = self._paths()
            return {
                "ready": True,
                "provider": self.name,
                "repo": str(repo),
                "checkpoint": str(checkpoint),
                "python": python,
            }
        except Exception as exc:
            return {"ready": False, "provider": self.name, "reason": str(exc)}


class WanAnimateBackend(_WanBase):
    name = "wan_animate"
    model = "Wan-AI/Wan2.2-Animate-14B"

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        if not job.driving_video:
            raise ValidationError("Wan Animate requires a driving/performance video")
        repo, checkpoint, python = self._paths()
        preprocess_script = require_file(
            repo / "wan/modules/animate/preprocess/preprocess_data.py",
            "Wan Animate preprocessing entrypoint",
        )
        process_checkpoint = require_directory(
            checkpoint / "process_checkpoint",
            "Wan Animate process checkpoint",
        )
        process_results = job.output_dir / "wan-process"
        process_results.mkdir(parents=True, exist_ok=True)
        width, height = _size(self.spec.options.get("resolution_area"), (1280, 720))
        preprocess_command = [
            python,
            str(preprocess_script),
            "--ckpt_path", str(process_checkpoint),
            "--video_path", str(job.driving_video),
            "--refer_path", str(job.avatar_image),
            "--save_path", str(process_results),
            "--resolution_area", str(width), str(height),
            "--retarget_flag",
            "--use_flux",
        ]
        preprocess_receipt = self.runner.run(
            preprocess_command,
            cwd=repo,
            receipt_dir=job.output_dir / "receipts",
            label="wan-animate-preprocess",
            timeout_s=min(self.spec.timeout_s, float(self.spec.options.get("preprocess_timeout_s", 1800))),
        )
        before = snapshot_mp4s(job.output_dir, repo)
        started_ns = start_timestamp_ns()
        generate_command = [
            python,
            str(repo / "generate.py"),
            "--task", "animate-14B",
            "--ckpt_dir", str(checkpoint),
            "--src_root_path", str(process_results),
            "--refert_num", str(int(self.spec.options.get("reference_frames", 1))),
        ]
        if bool(self.spec.options.get("offload_model", True)):
            generate_command.extend(["--offload_model", "True", "--convert_model_dtype"])
        generate_receipt = self.runner.run(
            generate_command,
            cwd=repo,
            receipt_dir=job.output_dir / "receipts",
            label="wan-animate-generate",
            timeout_s=self.spec.timeout_s,
        )
        generated = find_new_mp4([job.output_dir, repo], before, started_ns=started_ns)
        destination = stage_model_output(generated, job.output_dir / "model-video.mp4")
        return BackendArtifact(
            provider=self.name,
            model=self.model,
            video_path=destination,
            receipts=(preprocess_receipt.to_dict(), generate_receipt.to_dict()),
            metadata={
                "source": str(generated),
                "mode": "character_animation",
                "motion_source": str(job.driving_video),
                "resolution_area": [width, height],
            },
        )


class WanSpeechToVideoBackend(_WanBase):
    name = "wan_s2v"
    model = "Wan-AI/Wan2.2-S2V-14B"

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        repo, checkpoint, python = self._paths()
        width, height = _size(self.spec.options.get("size"), (1024, 704))
        before = snapshot_mp4s(job.output_dir, repo)
        started_ns = start_timestamp_ns()
        command = [
            python,
            str(repo / "generate.py"),
            "--task", "s2v-14B",
            "--size", f"{width}*{height}",
            "--ckpt_dir", str(checkpoint),
            "--prompt", job.prompt or "A natural presenter speaks directly to camera.",
            "--image", str(job.avatar_image),
            "--audio", str(job.audio),
        ]
        if bool(self.spec.options.get("offload_model", True)):
            command.extend(["--offload_model", "True", "--convert_model_dtype"])
        receipt = self.runner.run(
            command,
            cwd=repo,
            receipt_dir=job.output_dir / "receipts",
            label="wan-s2v",
            timeout_s=self.spec.timeout_s,
        )
        generated = find_new_mp4([job.output_dir, repo], before, started_ns=started_ns)
        destination = stage_model_output(generated, job.output_dir / "model-video.mp4")
        return BackendArtifact(
            provider=self.name,
            model=self.model,
            video_path=destination,
            receipts=(receipt.to_dict(),),
            metadata={"source": str(generated), "mode": "speech_to_video", "size": [width, height]},
        )
