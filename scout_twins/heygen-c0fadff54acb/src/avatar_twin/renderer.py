from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .configuration import RuntimeConfig
from .models import ValidationError, VideoProject
from .pipeline import AvatarVideoPipeline


@dataclass(slots=True)
class RenderEngine:
    """Production facade for the API and persistent render queue.

    The earlier procedural canvas and timing-tone path was removed. A completed
    render now requires a configured model backend and a probed audio-video MP4.
    """

    allowed_asset_root: Path
    runtime_config: RuntimeConfig | str | Path | None = None
    provider_name: str = ""
    allow_test_backends: bool = False
    # Retained constructor fields let older integrations receive a capability
    # error instead of an unexpected TypeError during migration.
    fps: int = 24
    max_draw_dimension: int = 0
    create_mp4: bool = True

    def _config(self) -> RuntimeConfig:
        if isinstance(self.runtime_config, RuntimeConfig):
            return self.runtime_config
        return RuntimeConfig.load(self.runtime_config)

    def readiness(self) -> dict[str, Any]:
        try:
            config = self._config()
            from .backends import build_backend
            statuses: dict[str, Any] = {}
            for name, candidate in sorted(config.providers.items()):
                if not candidate.enabled or (candidate.test_only and not self.allow_test_backends):
                    statuses[name] = {
                        "ready": False,
                        "provider": name,
                        "reason": "disabled" if not candidate.enabled else "test-only provider hidden in production",
                    }
                    continue
                statuses[name] = build_backend(
                    candidate,
                    max_download_bytes=config.max_download_bytes,
                ).readiness()
            selected_name = self.provider_name or config.default_talking_provider or config.default_performance_provider
            selected_status = statuses.get(selected_name, {})
            defaults = {config.default_talking_provider, config.default_performance_provider} - {""}
            defaults_ready = any(statuses.get(name, {}).get("ready") for name in defaults)
            return {
                "ready": bool(self.create_mp4 and (selected_status.get("ready") or defaults_ready)),
                "runtime": config.status(),
                "selected_provider": selected_name,
                "provider": selected_status,
                "providers": statuses,
            }
        except Exception as exc:
            return {"ready": False, "reason": str(exc)}

    def render(self, source: VideoProject, output_dir: str | Path) -> dict[str, Any]:
        if not self.create_mp4:
            raise ValidationError(
                "preview-only rendering was removed because it cannot reproduce the scouted avatar-video outcome"
            )
        return AvatarVideoPipeline(
            config=self._config(),
            allowed_asset_root=self.allowed_asset_root,
            allow_test_backends=self.allow_test_backends,
        ).render(
            source,
            output_dir,
            provider_name=self.provider_name or str(source.metadata.get("runtime_provider") or ""),
        )
