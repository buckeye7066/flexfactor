from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import json
import os

from .models import ValidationError


_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"RIFF",
)


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    is_loopback = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
        raise ValidationError("background-generation endpoint must use HTTPS (HTTP is allowed for loopback)")


@dataclass(slots=True)
class HttpBackgroundProvider:
    """Generate an actual background image through an operator-selected image model.

    The endpoint receives a provider-neutral JSON request and must return raw PNG,
    JPEG, or WebP bytes. Streaming and a hard byte ceiling keep generated assets
    from being buffered without limit.
    """

    endpoint: str
    api_key: str = ""
    timeout_s: float = 180.0
    max_bytes: int = 64 * 1024 * 1024

    def generate(self, prompt: str, width: int, height: int, destination: Path) -> Path:
        if not prompt.strip():
            raise ValidationError("generated background requires a prompt")
        _validate_endpoint(self.endpoint)
        body = json.dumps({
            "prompt": prompt,
            "width": width,
            "height": height,
            "format": "png",
            "purpose": "video_background",
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "image/png,image/jpeg,image/webp"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".download")
        total = 0
        prefix = b""
        try:
            with urlopen(request, timeout=self.timeout_s) as response, temporary.open("wb") as output:
                content_type = response.headers.get("Content-Type", "").lower()
                if not any(kind in content_type for kind in ("image/png", "image/jpeg", "image/webp")):
                    raise ValidationError(
                        f"background provider returned unsupported Content-Type: {content_type or '(missing)'}"
                    )
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > self.max_bytes:
                        raise ValidationError("generated background exceeded the configured byte limit")
                    if len(prefix) < 16:
                        prefix += block[:16 - len(prefix)]
                    output.write(block)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        is_webp = prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
        if total < 32 or not (prefix.startswith(_IMAGE_SIGNATURES[:2]) or is_webp):
            temporary.unlink(missing_ok=True)
            raise ValidationError("background provider did not return a recognized image artifact")
        temporary.replace(destination)
        return destination


def background_provider_from_env() -> HttpBackgroundProvider:
    endpoint = os.environ.get("AVATAR_TWIN_BACKGROUND_URL", "").strip()
    if not endpoint:
        raise ValidationError(
            "generated backgrounds require AVATAR_TWIN_BACKGROUND_URL; color, image, and video "
            "backgrounds work without that service"
        )
    return HttpBackgroundProvider(
        endpoint=endpoint,
        api_key=os.environ.get("AVATAR_TWIN_BACKGROUND_KEY", "").strip(),
        timeout_s=float(os.environ.get("AVATAR_TWIN_BACKGROUND_TIMEOUT_S", "180")),
        max_bytes=int(os.environ.get("AVATAR_TWIN_BACKGROUND_MAX_BYTES", str(64 * 1024 * 1024))),
    )
