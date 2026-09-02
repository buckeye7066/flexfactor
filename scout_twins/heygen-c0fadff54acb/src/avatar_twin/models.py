from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import re
import uuid


ASPECT_DIMENSIONS = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
_RESOLUTION_SHORT_EDGE = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "4k": 2160,
}
PROJECT_STATES = (
    "draft",
    "planned",
    "awaiting_review",
    "approved",
    "rendering",
    "completed",
    "failed",
)
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class ValidationError(ValueError):
    """A user-controlled project failed a fail-closed validation rule."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{sha256(payload).hexdigest()[:20]}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clean_color(value: str, field_name: str) -> str:
    if not _HEX_COLOR.fullmatch(value):
        raise ValidationError(f"{field_name} must be a six-digit hex color")
    return value.lower()


@dataclass(slots=True)
class ConsentRecord:
    subject_name: str = ""
    granted: bool = False
    recorded_at: str = ""
    evidence_reference: str = ""
    permitted_uses: list[str] = field(default_factory=lambda: ["avatar_video"])

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ConsentRecord":
        value = value or {}
        return cls(
            subject_name=str(value.get("subject_name") or ""),
            granted=bool(value.get("granted", False)),
            recorded_at=str(value.get("recorded_at") or ""),
            evidence_reference=str(value.get("evidence_reference") or ""),
            permitted_uses=[str(x) for x in value.get("permitted_uses") or ["avatar_video"]],
        )

    def validate(self, required_use: str = "avatar_video") -> None:
        if not self.granted:
            raise ValidationError("real-person avatars require affirmative consent")
        if not self.subject_name.strip() or not self.recorded_at.strip():
            raise ValidationError("consent requires subject_name and recorded_at")
        if required_use not in self.permitted_uses:
            raise ValidationError(f"consent does not permit {required_use} use")


@dataclass(slots=True)
class AvatarProfile:
    id: str = "avatar_illustrated"
    name: str = "Studio Presenter"
    kind: str = "illustrated"
    image_path: str = ""
    voice_id: str = "browser-default"
    style: str = "full_body"
    consent: ConsentRecord = field(default_factory=ConsentRecord)
    traits: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "AvatarProfile":
        value = value or {}
        return cls(
            id=str(value.get("id") or "avatar_illustrated"),
            name=str(value.get("name") or "Studio Presenter"),
            kind=str(value.get("kind") or "illustrated"),
            image_path=str(value.get("image_path") or ""),
            voice_id=str(value.get("voice_id") or "browser-default"),
            style=str(value.get("style") or "full_body"),
            consent=ConsentRecord.from_dict(value.get("consent")),
            traits=dict(value.get("traits") or {}),
        )

    def validate(self) -> None:
        if self.kind not in {"illustrated", "photo", "digital_twin", "stylized", "animal"}:
            raise ValidationError(f"unsupported avatar kind: {self.kind}")
        if self.style not in {"portrait", "full_body", "talking_head"}:
            raise ValidationError(f"unsupported avatar style: {self.style}")
        if self.kind in {"photo", "digital_twin"}:
            if not self.image_path:
                raise ValidationError(f"{self.kind} avatar requires image_path")
            self.consent.validate()


@dataclass(slots=True)
class VoiceProfile:
    id: str = "default"
    name: str = "Default voice"
    provider: str = "auto"
    language: str = "en"
    locale: str = "en-US"
    accent: str = "neutral"
    style: str = "conversational"
    gender: str = "unspecified"
    sample_path: str = ""
    rate: float = 1.0
    pitch_semitones: float = 0.0
    emotion: str = "neutral"
    consent: ConsentRecord = field(default_factory=ConsentRecord)
    traits: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None, *, fallback_id: str = "default") -> "VoiceProfile":
        value = value or {}
        language = str(value.get("language") or "en")
        return cls(
            id=str(value.get("id") or fallback_id),
            name=str(value.get("name") or value.get("id") or "Default voice"),
            provider=str(value.get("provider") or "auto"),
            language=language,
            locale=str(value.get("locale") or language),
            accent=str(value.get("accent") or "neutral"),
            style=str(value.get("style") or "conversational"),
            gender=str(value.get("gender") or "unspecified"),
            sample_path=str(value.get("sample_path") or ""),
            rate=float(value.get("rate", 1.0)),
            pitch_semitones=float(value.get("pitch_semitones", 0.0)),
            emotion=str(value.get("emotion") or "neutral"),
            consent=ConsentRecord.from_dict(value.get("consent")),
            traits=dict(value.get("traits") or {}),
        )

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", self.language):
            raise ValidationError("voice language must be a BCP-47-like language tag")
        if not 0.5 <= self.rate <= 2.0:
            raise ValidationError("voice rate must be between 0.5 and 2.0")
        if not -12.0 <= self.pitch_semitones <= 12.0:
            raise ValidationError("voice pitch_semitones must be between -12 and 12")
        if self.sample_path:
            self.consent.validate("voice_clone")


@dataclass(slots=True)
class Background:
    kind: str = "brand_color"
    color: str = "#10131a"
    path: str = ""
    prompt: str = ""
    fit: str = "cover"
    blur: float = 0.0
    key_color: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "Background":
        value = value or {}
        return cls(
            kind=str(value.get("kind") or "brand_color"),
            color=str(value.get("color") or "#10131a"),
            path=str(value.get("path") or ""),
            prompt=str(value.get("prompt") or ""),
            fit=str(value.get("fit") or "cover"),
            blur=float(value.get("blur", 0.0)),
            key_color=str(value.get("key_color") or ""),
        )

    def validate(self) -> None:
        if self.kind not in {"brand_color", "color", "image", "video", "generated", "transparent"}:
            raise ValidationError(f"unsupported background kind: {self.kind}")
        if self.kind in {"brand_color", "color"}:
            self.color = _clean_color(self.color, "background color")
        if self.kind in {"image", "video"} and not self.path:
            raise ValidationError(f"{self.kind} background requires path")
        if self.kind == "generated" and not self.prompt.strip():
            raise ValidationError("generated background requires prompt")
        if self.fit not in {"cover", "contain", "stretch"}:
            raise ValidationError(f"unsupported background fit: {self.fit}")
        if not 0.0 <= self.blur <= 100.0:
            raise ValidationError("background blur must be between 0 and 100")
        if self.key_color:
            self.key_color = _clean_color(self.key_color, "background key_color")


@dataclass(slots=True)
class BrandKit:
    name: str = "Avatar Studio"
    primary_color: str = "#6c5ce7"
    secondary_color: str = "#00cec9"
    background_color: str = "#10131a"
    text_color: str = "#ffffff"
    font_family: str = "system-ui"
    logo_path: str = ""
    glossary: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "BrandKit":
        value = value or {}
        return cls(
            name=str(value.get("name") or "Avatar Studio"),
            primary_color=str(value.get("primary_color") or "#6c5ce7"),
            secondary_color=str(value.get("secondary_color") or "#00cec9"),
            background_color=str(value.get("background_color") or "#10131a"),
            text_color=str(value.get("text_color") or "#ffffff"),
            font_family=str(value.get("font_family") or "system-ui"),
            logo_path=str(value.get("logo_path") or ""),
            glossary={str(k): str(v) for k, v in dict(value.get("glossary") or {}).items()},
        )

    def validate(self) -> None:
        self.primary_color = _clean_color(self.primary_color, "primary_color")
        self.secondary_color = _clean_color(self.secondary_color, "secondary_color")
        self.background_color = _clean_color(self.background_color, "background_color")
        self.text_color = _clean_color(self.text_color, "text_color")
        if len(self.font_family) > 120 or any(c in self.font_family for c in "<>\n\r"):
            raise ValidationError("font_family contains unsafe characters")


@dataclass(slots=True)
class Scene:
    id: str
    index: int
    start_s: float
    duration_s: float
    script: str
    visual: str = "presenter"
    layout: str = "presenter_left"
    transition: str = "crossfade"
    expression: str = "warm"
    gesture: str = "natural"
    caption: str = ""
    media_path: str = ""
    media_position: str = "right"
    media_scale: float = 0.36
    title_text: str = ""
    title_position: str = "top"
    title_color: str = "#ffffff"
    title_size: int = 42
    camera: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Scene":
        index = int(value.get("index", 0))
        script = str(value.get("script") or value.get("caption") or "").strip()
        return cls(
            id=str(value.get("id") or stable_id("scene", index, script)),
            index=index,
            start_s=float(value.get("start_s", value.get("start", 0.0))),
            duration_s=float(value.get("duration_s", value.get("dur", 0.0))),
            script=script,
            visual=str(value.get("visual") or "presenter"),
            layout=str(value.get("layout") or "presenter_left"),
            transition=str(value.get("transition") or "crossfade"),
            expression=str(value.get("expression") or "warm"),
            gesture=str(value.get("gesture") or "natural"),
            caption=str(value.get("caption") or script),
            media_path=str(value.get("media_path") or ""),
            media_position=str(value.get("media_position") or "right"),
            media_scale=float(value.get("media_scale", 0.36)),
            title_text=str(value.get("title_text") or ""),
            title_position=str(value.get("title_position") or "top"),
            title_color=str(value.get("title_color") or "#ffffff"),
            title_size=int(value.get("title_size", 42)),
            camera=dict(value.get("camera") or {}),
            performance=dict(value.get("performance") or {}),
        )

    def validate(self) -> None:
        if self.index < 0 or self.start_s < 0 or self.duration_s <= 0:
            raise ValidationError("scene index/start/duration must be positive")
        if not self.script.strip() and not self.media_path:
            raise ValidationError(f"scene {self.index} has no script or media")
        if self.layout not in {"presenter_left", "presenter_right", "center", "full_bleed", "performance"}:
            raise ValidationError(f"unsupported scene layout: {self.layout}")
        if self.media_position not in {"left", "right", "center", "full_bleed"}:
            raise ValidationError(f"unsupported scene media_position: {self.media_position}")
        if not 0.1 <= self.media_scale <= 1.0:
            raise ValidationError("scene media_scale must be between 0.1 and 1.0")
        if self.title_position not in {"top", "center", "bottom"}:
            raise ValidationError(f"unsupported scene title_position: {self.title_position}")
        self.title_color = _clean_color(self.title_color, "scene title_color")
        if not 12 <= self.title_size <= 160:
            raise ValidationError("scene title_size must be between 12 and 160")


@dataclass(slots=True)
class VideoProject:
    id: str
    title: str
    prompt: str = ""
    script: str = ""
    target_duration_s: float = 30.0
    aspect_ratio: str = "16:9"
    language: str = "en"
    source_language: str = "en"
    avatar: AvatarProfile = field(default_factory=AvatarProfile)
    voice: VoiceProfile = field(default_factory=VoiceProfile)
    background: Background = field(default_factory=Background)
    brand: BrandKit = field(default_factory=BrandKit)
    scenes: list[Scene] = field(default_factory=list)
    status: str = "draft"
    narration_audio_path: str = ""
    reference_image_path: str = ""
    performance_timeline_path: str = ""
    captions_enabled: bool = True
    callback_url: str = ""
    template_id: str = ""
    background_music_path: str = ""
    background_music_volume: float = 0.12
    output_format: str = "mp4"
    output_resolution: str = "720p"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VideoProject":
        title = str(value.get("title") or "Untitled avatar video")
        prompt = str(value.get("prompt") or "")
        project_id = str(value.get("id") or stable_id("project", title, prompt))
        return cls(
            id=project_id,
            title=title,
            prompt=prompt,
            script=str(value.get("script") or ""),
            target_duration_s=float(value.get("target_duration_s", 30.0)),
            aspect_ratio=str(value.get("aspect_ratio") or "16:9"),
            language=str(value.get("language") or "en"),
            source_language=str(value.get("source_language") or value.get("language") or "en"),
            avatar=AvatarProfile.from_dict(value.get("avatar")),
            voice=VoiceProfile.from_dict(
                value.get("voice"),
                fallback_id=str((value.get("avatar") or {}).get("voice_id") or "default"),
            ),
            background=Background.from_dict(value.get("background")),
            brand=BrandKit.from_dict(value.get("brand")),
            scenes=[Scene.from_dict(x) for x in value.get("scenes") or []],
            status=str(value.get("status") or "draft"),
            narration_audio_path=str(value.get("narration_audio_path") or ""),
            reference_image_path=str(value.get("reference_image_path") or ""),
            performance_timeline_path=str(value.get("performance_timeline_path") or ""),
            captions_enabled=bool(value.get("captions_enabled", True)),
            callback_url=str(value.get("callback_url") or ""),
            template_id=str(value.get("template_id") or ""),
            background_music_path=str(value.get("background_music_path") or ""),
            background_music_volume=float(value.get("background_music_volume", 0.12)),
            output_format=str(value.get("output_format") or "mp4"),
            output_resolution=str(value.get("output_resolution") or "720p"),
            metadata=dict(value.get("metadata") or {}),
            created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()),
        )

    @property
    def dimensions(self) -> tuple[int, int]:
        """Pixel dimensions for the selected aspect and output resolution.

        Resolution labels use the short edge. This keeps portrait output at
        720x1280 when 720p is selected instead of incorrectly shrinking it to
        405x720.
        """
        short_edge = _RESOLUTION_SHORT_EDGE[self.output_resolution]
        width_ratio, height_ratio = (int(part) for part in self.aspect_ratio.split(":"))
        if width_ratio >= height_ratio:
            height = short_edge
            width = round(short_edge * width_ratio / height_ratio)
        else:
            width = short_edge
            height = round(short_edge * height_ratio / width_ratio)
        return width + width % 2, height + height % 2

    @property
    def duration_s(self) -> float:
        return max((scene.end_s for scene in self.scenes), default=self.target_duration_s)

    def validate(self, *, require_approved: bool = False) -> None:
        if not self.prompt.strip() and not self.script.strip() and not self.scenes:
            raise ValidationError("project requires prompt, script, or scenes")
        if not 1.0 <= self.target_duration_s <= 1800.0:
            raise ValidationError("target_duration_s must be between 1 and 1800")
        if self.aspect_ratio not in ASPECT_DIMENSIONS:
            raise ValidationError(f"unsupported aspect_ratio: {self.aspect_ratio}")
        if self.status not in PROJECT_STATES:
            raise ValidationError(f"unsupported project status: {self.status}")
        if require_approved and self.status != "approved":
            raise ValidationError("render requires an explicitly approved project")
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", self.language):
            raise ValidationError("language must be a BCP-47-like language tag")
        self.avatar.validate()
        self.voice.validate()
        self.background.validate()
        self.brand.validate()
        if self.output_format not in {"mp4", "webm", "mov"}:
            raise ValidationError(f"unsupported output_format: {self.output_format}")
        if self.output_resolution not in {"480p", "720p", "1080p", "4k"}:
            raise ValidationError(f"unsupported output_resolution: {self.output_resolution}")
        if not 0.0 <= self.background_music_volume <= 1.0:
            raise ValidationError("background_music_volume must be between 0 and 1")
        if self.background.kind == "transparent" and self.output_format == "mp4":
            raise ValidationError("transparent background requires webm or mov output")
        previous_end = 0.0
        for expected, scene in enumerate(self.scenes):
            scene.validate()
            if scene.index != expected:
                raise ValidationError("scene indices must be contiguous from zero")
            if abs(scene.start_s - previous_end) > 0.002:
                raise ValidationError("scenes must tile the timeline without gaps or overlaps")
            previous_end = scene.end_s

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def clone(self) -> "VideoProject":
        return VideoProject.from_dict(self.to_dict())

    def touch(self) -> None:
        self.updated_at = utc_now()

    @classmethod
    def load(cls, path: str | Path) -> "VideoProject":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def dump(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return output
