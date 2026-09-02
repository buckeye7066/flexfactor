from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import os

from .models import ValidationError


SUPPORTED_PROVIDER_TYPES = {
    "wan_animate",
    "wan_s2v",
    "sadtalker",
    "musetalk",
    "liveportrait",
    "remote_worker",
    "command",
}


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    type: str
    enabled: bool = True
    repo: str = ""
    python: str = "python"
    checkpoint: str = ""
    timeout_s: float = 3600.0
    test_only: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, value: dict[str, Any]) -> "ProviderSpec":
        provider_type = str(value.get("type") or name).strip().lower()
        if provider_type not in SUPPORTED_PROVIDER_TYPES:
            raise ValidationError(
                f"provider {name!r} has unsupported type {provider_type!r}; "
                f"expected one of {sorted(SUPPORTED_PROVIDER_TYPES)}"
            )
        timeout_s = float(value.get("timeout_s", 3600.0))
        if not 1.0 <= timeout_s <= 86400.0:
            raise ValidationError(f"provider {name!r} timeout_s must be between 1 and 86400")
        reserved = {
            "type", "enabled", "repo", "python", "checkpoint", "timeout_s", "test_only",
        }
        options = dict(value.get("options") or {})
        options.update({key: item for key, item in value.items() if key not in reserved | {"options"}})
        return cls(
            name=name,
            type=provider_type,
            enabled=bool(value.get("enabled", True)),
            repo=str(value.get("repo") or ""),
            python=str(value.get("python") or "python"),
            checkpoint=str(value.get("checkpoint") or ""),
            timeout_s=timeout_s,
            test_only=bool(value.get("test_only", False)),
            options=options,
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    providers: dict[str, ProviderSpec]
    default_talking_provider: str = ""
    default_performance_provider: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_download_bytes: int = 4 * 1024 * 1024 * 1024
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, source_path: Path | None = None) -> "RuntimeConfig":
        raw_providers = value.get("providers") or {}
        if not isinstance(raw_providers, dict):
            raise ValidationError("runtime providers must be a JSON object")
        providers = {
            str(name): ProviderSpec.from_dict(str(name), dict(spec or {}))
            for name, spec in raw_providers.items()
        }
        max_download = int(value.get("max_download_bytes", 4 * 1024 * 1024 * 1024))
        if not 1024 <= max_download <= 64 * 1024 * 1024 * 1024:
            raise ValidationError("max_download_bytes is outside the supported safety bounds")
        return cls(
            providers=providers,
            default_talking_provider=str(value.get("default_talking_provider") or ""),
            default_performance_provider=str(value.get("default_performance_provider") or ""),
            ffmpeg=str(value.get("ffmpeg") or "ffmpeg"),
            ffprobe=str(value.get("ffprobe") or "ffprobe"),
            max_download_bytes=max_download,
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RuntimeConfig":
        selected = str(path or os.environ.get("AVATAR_TWIN_CONFIG") or "").strip()
        if not selected:
            raise ValidationError(
                "no avatar runtime is configured; copy runtime.example.json, set real model paths "
                "or a remote worker URL, then pass --runtime-config or AVATAR_TWIN_CONFIG"
            )
        config_path = Path(selected).expanduser().resolve()
        if not config_path.is_file():
            raise ValidationError(f"runtime config does not exist: {config_path}")
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read runtime config {config_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("runtime config root must be a JSON object")
        return cls.from_dict(value, source_path=config_path)

    def provider(self, name: str, *, allow_test: bool = False) -> ProviderSpec:
        spec = self.providers.get(name)
        if spec is None:
            raise ValidationError(f"runtime provider {name!r} is not configured")
        if not spec.enabled:
            raise ValidationError(f"runtime provider {name!r} is disabled")
        if spec.test_only and not allow_test:
            raise ValidationError(f"runtime provider {name!r} is test-only and cannot render production jobs")
        return spec

    def choose(self, *, performance: bool, requested: str = "", allow_test: bool = False) -> ProviderSpec:
        name = requested or (
            self.default_performance_provider if performance else self.default_talking_provider
        )
        if not name:
            mode = "performance" if performance else "talking-avatar"
            raise ValidationError(f"no default {mode} provider is configured")
        return self.provider(name, allow_test=allow_test)

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.providers),
            "source_path": str(self.source_path) if self.source_path else None,
            "default_talking_provider": self.default_talking_provider,
            "default_performance_provider": self.default_performance_provider,
            "providers": {
                name: {
                    "type": spec.type,
                    "enabled": spec.enabled,
                    "test_only": spec.test_only,
                    "repo_configured": bool(spec.repo),
                    "checkpoint_configured": bool(spec.checkpoint),
                }
                for name, spec in sorted(self.providers.items())
            },
        }
