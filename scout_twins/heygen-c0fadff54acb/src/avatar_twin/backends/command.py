from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AvatarRenderRequest, BackendArtifact
from ..configuration import ProviderSpec
from ..models import ValidationError
from ..runtime import CommandRunner, resolve_executable


class CommandAvatarBackend:
    """Adapter for an operator-owned CLI. It is explicit argv templating, never shell execution."""

    name = "command"

    def __init__(self, spec: ProviderSpec, runner: CommandRunner | None = None) -> None:
        self.spec = spec
        self.runner = runner or CommandRunner()

    def _command(self, request: AvatarRenderRequest) -> tuple[list[str], Path]:
        executable = str(self.spec.options.get("executable") or "").strip()
        raw_args = self.spec.options.get("args")
        raw_output = str(self.spec.options.get("output") or "{output_dir}/model-video.mp4")
        if not executable or not isinstance(raw_args, list):
            raise ValidationError("command provider requires options.executable and options.args[]")
        values = {
            "job_id": request.job_id,
            "avatar_image": str(request.avatar_image),
            "audio": str(request.audio),
            "driving_video": str(request.driving_video or ""),
            "output_dir": str(request.output_dir),
            "prompt": request.prompt,
        }
        try:
            args = [str(item).format_map(values) for item in raw_args]
            output = Path(raw_output.format_map(values)).expanduser().resolve()
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"invalid command provider placeholder: {exc}") from exc
        if not output.is_relative_to(request.output_dir.resolve()):
            raise ValidationError("command provider output must remain inside the job output directory")
        return [resolve_executable(executable), *args], output

    def readiness(self) -> dict[str, Any]:
        try:
            executable = resolve_executable(str(self.spec.options.get("executable") or ""))
            return {
                "ready": True,
                "provider": self.name,
                "executable": executable,
                "test_only": self.spec.test_only,
            }
        except Exception as exc:
            return {"ready": False, "provider": self.name, "reason": str(exc)}

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        command, output = self._command(job)
        receipt = self.runner.run(
            command,
            cwd=job.output_dir,
            receipt_dir=job.output_dir / "receipts",
            label=f"command-{self.spec.name}",
            timeout_s=self.spec.timeout_s,
        )
        if not output.is_file():
            raise RuntimeError(f"command provider did not create its declared output: {output}")
        return BackendArtifact(
            provider=self.spec.name,
            model=str(self.spec.options.get("model") or "operator-owned-command"),
            video_path=output,
            receipts=(receipt.to_dict(),),
            metadata={"mode": "external_command", "test_only": self.spec.test_only},
        )
