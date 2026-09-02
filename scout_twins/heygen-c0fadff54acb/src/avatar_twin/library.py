from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json
import re

from .audio import secure_asset_path
from .media import file_sha256
from .models import AvatarProfile, Background, BrandKit, ValidationError, VideoProject, VoiceProfile, utc_now


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def _safe_id(value: str, label: str) -> str:
    if not _ID.fullmatch(value):
        raise ValidationError(f"{label} must match {_ID.pattern}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(slots=True)
class VoiceCatalog:
    voices: list[VoiceProfile]

    @classmethod
    def load(cls, path: str | Path) -> "VoiceCatalog":
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ValidationError(f"voice catalog does not exist: {source}")
        value = json.loads(source.read_text(encoding="utf-8"))
        rows = value.get("voices") if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValidationError("voice catalog must contain a voices array")
        voices = [VoiceProfile.from_dict(dict(row or {})) for row in rows]
        ids = [voice.id for voice in voices]
        if len(set(ids)) != len(ids):
            raise ValidationError("voice catalog contains duplicate ids")
        for voice in voices:
            _safe_id(voice.id, "voice id")
            voice.validate()
        return cls(voices)

    def search(
        self,
        *,
        language: str = "",
        locale: str = "",
        accent: str = "",
        style: str = "",
        gender: str = "",
    ) -> list[VoiceProfile]:
        filters = {
            "language": language.lower(),
            "locale": locale.lower(),
            "accent": accent.lower(),
            "style": style.lower(),
            "gender": gender.lower(),
        }
        matches = []
        for voice in self.voices:
            if all(not wanted or str(getattr(voice, key)).lower() == wanted
                   for key, wanted in filters.items()):
                matches.append(voice)
        return matches

    def get(self, voice_id: str) -> VoiceProfile:
        for voice in self.voices:
            if voice.id == voice_id:
                return VoiceProfile.from_dict(asdict(voice))
        raise KeyError(voice_id)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "1.0", "voices": [asdict(voice) for voice in self.voices]}


class AvatarLibrary:
    """Durable metadata/index for consented reusable avatar identities and looks."""

    def __init__(self, root: str | Path, allowed_asset_root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.allowed_asset_root = Path(allowed_asset_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, profile: AvatarProfile, *, look_name: str = "default") -> dict[str, Any]:
        profile.validate()
        avatar_id = _safe_id(profile.id, "avatar id")
        look_id = _safe_id(look_name, "look name")
        image = secure_asset_path(profile.image_path, self.allowed_asset_root)
        digest = file_sha256(image)
        record = {
            "schema_version": "1.0",
            "avatar_id": avatar_id,
            "look_id": look_id,
            "profile": asdict(profile),
            "image_sha256": digest,
            "registered_at": utc_now(),
        }
        _write_json(self.root / avatar_id / f"{look_id}.json", record)
        return record

    def list(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.root.glob("*/*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        return records

    def get(self, avatar_id: str, look_id: str = "default") -> AvatarProfile:
        path = self.root / _safe_id(avatar_id, "avatar id") / f"{_safe_id(look_id, 'look id')}.json"
        if not path.is_file():
            raise KeyError(f"{avatar_id}/{look_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        profile = AvatarProfile.from_dict(record.get("profile"))
        image = secure_asset_path(profile.image_path, self.allowed_asset_root)
        if file_sha256(image) != record.get("image_sha256"):
            raise ValidationError("registered avatar image changed after consented enrollment")
        return profile


class TemplateStore:
    """Reusable scene/layout/voice/background/brand configurations."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, template_id: str, project: VideoProject, *, name: str = "") -> dict[str, Any]:
        project.validate()
        identifier = _safe_id(template_id, "template id")
        record = {
            "schema_version": "1.0",
            "id": identifier,
            "name": name or project.title,
            "aspect_ratio": project.aspect_ratio,
            "output_resolution": project.output_resolution,
            "voice": asdict(project.voice),
            "background": asdict(project.background),
            "brand": asdict(project.brand),
            "scene_styles": [{
                "visual": scene.visual,
                "layout": scene.layout,
                "transition": scene.transition,
                "expression": scene.expression,
                "gesture": scene.gesture,
                "camera": scene.camera,
            } for scene in project.scenes],
            "saved_at": utc_now(),
        }
        _write_json(self.root / f"{identifier}.json", record)
        return record

    def list(self) -> list[dict[str, Any]]:
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.root.glob("*.json"))]

    def apply(self, template_id: str, source: VideoProject) -> VideoProject:
        path = self.root / f"{_safe_id(template_id, 'template id')}.json"
        if not path.is_file():
            raise KeyError(template_id)
        template = json.loads(path.read_text(encoding="utf-8"))
        project = source.clone()
        project.template_id = template_id
        project.aspect_ratio = str(template["aspect_ratio"])
        project.output_resolution = str(template.get("output_resolution") or "720p")
        project.voice = VoiceProfile.from_dict(template.get("voice"), fallback_id=project.voice.id)
        project.background = Background.from_dict(template.get("background"))
        project.brand = BrandKit.from_dict(template.get("brand"))
        styles = template.get("scene_styles") or []
        for index, scene in enumerate(project.scenes):
            if not styles:
                break
            style = styles[min(index, len(styles) - 1)]
            for key in ("visual", "layout", "transition", "expression", "gesture"):
                if style.get(key):
                    setattr(scene, key, str(style[key]))
            scene.camera = dict(style.get("camera") or scene.camera)
        project.status = "awaiting_review" if project.scenes else "draft"
        project.touch()
        project.validate()
        return project
