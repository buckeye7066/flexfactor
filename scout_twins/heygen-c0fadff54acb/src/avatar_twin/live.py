from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import json
import os
import re
import tempfile
import uuid

from .models import ValidationError, utc_now


@dataclass(frozen=True, slots=True)
class LiveSession:
    id: str
    provider_session_id: str
    avatar_id: str
    language: str
    state: str
    created_at: str
    join_url: str
    expires_at: str = ""

    def to_dict(self, *, include_join_url: bool = True) -> dict[str, Any]:
        value = {
            "id": self.id, "provider_session_id": self.provider_session_id,
            "avatar_id": self.avatar_id, "language": self.language,
            "state": self.state, "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if include_join_url:
            value["join_url"] = self.join_url
        return value


class HttpLiveAvatarProvider:
    """Provider-neutral adapter for a real low-latency avatar streaming service."""

    def __init__(self, endpoint: str, api_key: str = "", *,
                 opener: Callable[..., Any] = urlopen, timeout_s: float = 30.0) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValidationError("live avatar endpoint must be credential-free HTTPS")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.opener = opener
        self.timeout_s = timeout_s

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint + path,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST", headers=headers)
        try:
            with self.opener(request, timeout=self.timeout_s) as response:
                if int(getattr(response, "status", 200)) not in {200, 201, 202}:
                    raise ValidationError("live avatar provider rejected the request")
                value = json.loads(response.read(2_000_001))
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(f"live avatar provider failed: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("live avatar provider returned malformed JSON")
        return value

    def create(self, *, avatar_id: str, context: str, voice_id: str = "",
               language: str = "en") -> LiveSession:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", avatar_id):
            raise ValidationError("live avatar_id contains unsafe characters")
        if not context.strip() or len(context) > 100_000:
            raise ValidationError("live avatar context must be between 1 and 100000 characters")
        response = self._request("/sessions", {
            "avatar_id": avatar_id, "context": context,
            "voice_id": voice_id, "language": language,
            "transport": "webrtc", "bidirectional_audio": True,
        })
        provider_id = str(response.get("session_id") or response.get("id") or "").strip()
        join_url = str(response.get("join_url") or response.get("room_url") or "").strip()
        parsed_join = urlparse(join_url)
        if not provider_id or parsed_join.scheme not in {"https", "wss"} or not parsed_join.hostname:
            raise ValidationError("live avatar provider omitted a valid session_id or join_url")
        return LiveSession(
            id="live_" + uuid.uuid4().hex,
            provider_session_id=provider_id,
            avatar_id=avatar_id,
            language=language,
            state="ready",
            created_at=utc_now(),
            join_url=join_url,
            expires_at=str(response.get("expires_at") or ""),
        )

    def end(self, provider_session_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", provider_session_id):
            raise ValidationError("provider session id contains unsafe characters")
        return self._request("/sessions/end", {"session_id": provider_session_id})


class LiveSessionStore:
    """Persists session lifecycle metadata but never provider room tokens."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, session: LiveSession) -> None:
        destination = self.root / f"{session.id}.json"
        handle, temporary_name = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(session.to_dict(include_join_url=False), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(self.root.glob("live_*.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


def live_provider_from_env() -> HttpLiveAvatarProvider:
    endpoint = os.environ.get("AVATAR_TWIN_LIVE_URL", "").strip()
    if not endpoint:
        raise ValidationError("AVATAR_TWIN_LIVE_URL is not configured")
    return HttpLiveAvatarProvider(endpoint, os.environ.get("AVATAR_TWIN_LIVE_KEY", ""))
