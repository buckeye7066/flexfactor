from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
import json
import re

from .models import ValidationError, VideoProject, stable_id


class TranslationProvider(Protocol):
    def translate(self, text: str, source_language: str, target_language: str,
                  glossary: dict[str, str]) -> str: ...


_PHRASES = {
    ("en", "es"): {
        "welcome": "bienvenido", "create": "crear", "music": "música",
        "performance": "interpretación", "thank you": "gracias",
        "play": "tocar", "video": "video", "your": "tu",
    },
    ("en", "fr"): {
        "welcome": "bienvenue", "create": "créer", "music": "musique",
        "performance": "performance", "thank you": "merci",
        "play": "jouer", "video": "vidéo", "your": "votre",
    },
    ("en", "de"): {
        "welcome": "willkommen", "create": "erstellen", "music": "musik",
        "performance": "aufführung", "thank you": "danke",
        "play": "spielen", "video": "video", "your": "dein",
    },
}


@dataclass(slots=True)
class RuleTranslationProvider:
    """Small deterministic test/demo translator; never claims general coverage."""

    def translate(self, text: str, source_language: str, target_language: str,
                  glossary: dict[str, str]) -> str:
        source = source_language.split("-")[0].lower()
        target = target_language.split("-")[0].lower()
        if source == target:
            return text
        table = _PHRASES.get((source, target))
        if table is None:
            raise ValidationError(
                f"local translator does not cover {source_language}->{target_language}; "
                "configure an HTTP translation provider"
            )
        protected: dict[str, str] = {}
        output = text
        for number, term in enumerate(sorted(glossary, key=len, reverse=True)):
            marker = f"__GLOSSARY_{number}__"
            protected[marker] = glossary.get(term) or term
            output = re.sub(re.escape(term), marker, output, flags=re.IGNORECASE)
        for phrase, translated in sorted(table.items(), key=lambda item: len(item[0]), reverse=True):
            output = re.sub(rf"\b{re.escape(phrase)}\b", translated, output, flags=re.IGNORECASE)
        for marker, replacement in protected.items():
            output = output.replace(marker, replacement)
        return output


@dataclass(slots=True)
class HttpTranslationProvider:
    endpoint: str
    api_key: str = ""
    timeout_s: float = 60.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    max_response_bytes: int = 2 * 1024 * 1024

    def translate(self, text: str, source_language: str, target_language: str,
                  glossary: dict[str, str]) -> str:
        parsed = urlsplit(self.endpoint)
        loopback = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValidationError("translation endpoint must use HTTPS (HTTP is allowed for loopback)")
        payload = json.dumps({
            "text": text,
            "source_language": source_language,
            "target_language": target_language,
            "glossary": glossary,
        }).encode()
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=self.timeout_s) as response:
            raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise ValidationError("translation response exceeded the configured byte limit")
        result = json.loads(raw.decode())
        translated = result.get("translated_text") or result.get("text")
        if not isinstance(translated, str) or not translated.strip():
            raise ValidationError("translation provider returned no translated_text")
        return translated


def localize_project(source: VideoProject, target_language: str,
                     provider: TranslationProvider | None = None) -> VideoProject:
    if provider is None:
        raise ValidationError(
            "general translation requires a configured provider; the small rule translator is test/demo-only"
        )
    project = source.clone()
    project.source_language = source.language
    project.language = target_language
    project.voice.language = target_language
    project.voice.locale = target_language
    project.id = stable_id("project", source.id, target_language)
    project.title = f"{source.title} [{target_language}]"
    project.prompt = provider.translate(source.prompt, source.language, target_language,
                                        source.brand.glossary) if source.prompt else ""
    project.script = provider.translate(source.script, source.language, target_language,
                                        source.brand.glossary) if source.script else ""
    for scene in project.scenes:
        scene.script = provider.translate(scene.script, source.language, target_language,
                                          source.brand.glossary)
        scene.caption = scene.script
    project.metadata["localization"] = {
        "source_project_id": source.id,
        "source_language": source.language,
        "target_language": target_language,
        "provider": provider.__class__.__name__,
        "review_required": True,
        "source_narration_audio_path": source.narration_audio_path or None,
    }
    # A translated script must not silently reuse narration in the source
    # language. Approval followed by render synthesizes/dubs the target voice.
    project.narration_audio_path = ""
    project.status = "awaiting_review"
    project.touch()
    project.validate()
    return project
