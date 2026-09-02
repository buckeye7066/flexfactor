"""Inference through the ChatGPT subscription owned by the Codex CLI.

The official Codex client signs in with ChatGPT and stores OAuth material in
``CODEX_HOME/auth.json``.  On an ordinary desktop install that file contains an
access token and an account id; those credentials can call the same Responses
endpoint used by Codex without creating a nested coding-agent process.

This module is deliberately small and stdlib-only.  It never prints or returns
credentials, never stores a response, and does not have filesystem tools.  A
managed environment may replace the OAuth values with broker placeholders.  In
that case :func:`load_exportable_oauth` returns ``None`` and the caller must use
another supported transport rather than sending the placeholders over HTTP.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional


RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"

# These are catalog aliases, not wire model ids.  AI Time used ``codex`` while
# delegating model choice to the CLI; a direct request must resolve that alias
# against the authenticated account's live model catalog first.
_DEFAULT_MODEL_ALIASES = {"", "codex", "default", "auto"}


class SubscriptionUnavailable(RuntimeError):
    """The ChatGPT subscription transport could not serve this call."""


class SubscriptionAuthenticationError(SubscriptionUnavailable):
    """The stored OAuth credential was rejected and may need CLI refresh."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


@dataclass(frozen=True)
class CodexOAuth:
    access_token: str
    account_id: str
    source: str


def codex_auth_path() -> Path:
    override = os.environ.get("FLEXFACTOR_CODEX_AUTH_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "auth.json"


def load_exportable_oauth(path: Optional[Path] = None) -> Optional[CodexOAuth]:
    """Return usable ChatGPT OAuth material, never managed placeholders.

    The account id is mandatory.  Besides binding a request to the intended
    paid workspace, its absence is the reliable signal used by managed Codex
    environments whose short sentinel tokens must not be exported.
    """
    target = path or codex_auth_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("auth_mode") not in (None, "chatgpt"):
        return None
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    account = tokens.get("account_id")
    if not isinstance(access, str) or not access.strip():
        return None
    if not isinstance(account, str) or not account.strip():
        return None
    return CodexOAuth(access.strip(), account.strip(), str(target))


def _codex_version(binary: str) -> str:
    """Read the installed client version without ever invoking an agent."""
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, shell=False,
        )
    except Exception:
        return ""
    if proc.returncode:
        return ""
    match = re.search(r"(?:codex(?:-cli)?\s+)?([0-9]+(?:\.[0-9A-Za-z-]+)+)",
                      proc.stdout or "")
    return match.group(1) if match else ""


def _message_from_error_body(body: bytes) -> str:
    text = body.decode("utf-8", "replace")[:1000]
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return " ".join(text.split())[:400]
    if isinstance(raw, dict):
        error = raw.get("error") or raw.get("detail") or raw.get("message")
        if isinstance(error, dict):
            error = error.get("message") or error.get("code")
        if error:
            return " ".join(str(error).split())[:400]
    return "request was rejected"


def _output_text(response: Any) -> str:
    """Extract text from a completed Responses-API object."""
    if not isinstance(response, dict):
        return ""
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct
    chunks = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and part.get("type") in (
                    None, "output_text", "text"):
                chunks.append(text)
    return "".join(chunks)


def _sse_events(lines: Iterable[bytes], *, deadline: Optional[float] = None,
                timeout: Optional[float] = None,
                clock: Callable[[], float] = time.monotonic
                ) -> Iterator[Dict[str, Any]]:
    """Yield JSON data objects from SSE while enforcing a wall-clock deadline.

    Heartbeat/comment lines intentionally yield no event.  Checking time only in
    the consumer's event loop therefore allowed a server to keep a socket alive
    forever with heartbeats.  Check around *every raw line* instead.
    """
    def check_deadline() -> None:
        if deadline is not None and clock() >= deadline:
            label = f"{float(timeout):.0f}s" if timeout is not None else "its deadline"
            raise SubscriptionUnavailable(
                f"ChatGPT subscription exceeded {label}")

    data: list[str] = []
    check_deadline()
    for raw_line in lines:
        check_deadline()
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        check_deadline()
        if not line:
            if data:
                payload = "\n".join(data)
                data.clear()
                if payload != "[DONE]":
                    try:
                        value = json.loads(payload)
                    except json.JSONDecodeError:
                        value = None
                    if isinstance(value, dict):
                        yield value
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        check_deadline()
        try:
            value = json.loads("\n".join(data))
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            yield value


class ChatGPTSubscriptionClient:
    """One-shot text inference over the authenticated Codex Responses route."""

    def __init__(
        self,
        oauth: CodexOAuth,
        *,
        model: str,
        binary: str,
        timeout: float,
        urlopen: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._oauth = oauth
        self._requested_model = str(model or "").strip()
        self._binary = binary
        self._timeout = max(1.0, float(timeout))
        self._urlopen = urlopen or urllib.request.urlopen
        self._version = _codex_version(binary)
        self.model = "" if self._requested_model.lower() in _DEFAULT_MODEL_ALIASES \
            else self._requested_model

    def _headers(self, *, json_body: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._oauth.access_token}",
            "ChatGPT-Account-Id": self._oauth.account_id,
            "Accept": "text/event-stream, application/json",
            "OpenAI-Beta": "responses=v1",
            # This route is Codex subscription capacity selected by AI Time.
            # The official originator keeps account model entitlements aligned
            # with the installed Codex client rather than a web-chat surface.
            "Originator": "codex_cli_rs",
            "User-Agent": "FlexFactor/0.6.1 (Codex subscription transport)",
        }
        if self._version:
            headers["Version"] = self._version
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        try:
            return self._urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                detail = _message_from_error_body(exc.read())
            except Exception:
                detail = "request was rejected"
            reset = exc.headers.get("Retry-After") if exc.headers else None
            suffix = f"; retry after {reset}s" if reset else ""
            message = f"ChatGPT subscription HTTP {exc.code}: {detail}{suffix}"
            if exc.code in (401, 403):
                raise SubscriptionAuthenticationError(message, exc.code) from None
            error = SubscriptionUnavailable(message)
            error.status_code = int(exc.code)
            raise error from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SubscriptionUnavailable(
                f"ChatGPT subscription connection failed: {reason}"
            ) from None

    def _resolve_model(self) -> str:
        if self.model:
            return self.model
        query = urllib.parse.urlencode({"client_version": self._version or "unknown"})
        request = urllib.request.Request(
            f"{MODELS_URL}?{query}", headers=self._headers(), method="GET")
        with self._open(request, min(self._timeout, 20.0)) as response:
            try:
                raw = json.loads(response.read().decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise SubscriptionUnavailable(
                    f"ChatGPT model catalog was not valid JSON: {exc}"
                ) from None
        values = raw.get("models") or raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            raise SubscriptionUnavailable("ChatGPT model catalog contained no models")
        candidates = []
        default = ""
        for item in values:
            if not isinstance(item, dict):
                continue
            model_id = item.get("slug") or item.get("id") or item.get("model")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_id = model_id.strip()
            candidates.append(model_id)
            if item.get("is_default") or item.get("isDefault") or item.get("default"):
                default = model_id
        if not candidates:
            raise SubscriptionUnavailable("ChatGPT model catalog contained no usable model id")
        # The authenticated catalog's default is the highest model the account
        # is presently entitled to (Sol on the current paid ladder).  When an
        # older catalog lacks that bit, preserve its strongest-first order.
        self.model = default or candidates[0]
        return self.model

    def complete(self, prompt: str, *, system: Optional[str] = None,
                 max_tokens: int = 4096,
                 timeout: Optional[float] = None) -> str:
        call_timeout = max(1.0, min(self._timeout, float(timeout or self._timeout)))
        model = self._resolve_model()
        payload = {
            "model": model,
            "instructions": system or (
                "You are a text inference engine inside FlexFactor. Follow the "
                "user request exactly. Do not call tools or modify files."
            ),
            "input": [{
                "role": "user",
                "content": [{"type": "input_text", "text": str(prompt)}],
            }],
            "tools": [],
            "store": False,
            "stream": True,
            "max_output_tokens": max(1, int(max_tokens)),
            "reasoning": {"effort": "high", "summary": "auto"},
        }
        request = urllib.request.Request(
            RESPONSES_URL,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=self._headers(json_body=True),
            method="POST",
        )
        started = time.monotonic()
        with self._open(request, min(call_timeout, 60.0)) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if "text/event-stream" not in content_type:
                try:
                    raw = json.loads(response.read().decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise SubscriptionUnavailable(
                        f"ChatGPT response was not valid JSON: {exc}"
                    ) from None
                answer = _output_text(raw)
                if not answer.strip():
                    raise SubscriptionUnavailable("ChatGPT subscription returned no output")
                return answer.strip()

            deltas = []
            completed: Optional[Dict[str, Any]] = None
            for event in _sse_events(
                    response, deadline=started + call_timeout,
                    timeout=call_timeout):
                kind = str(event.get("type") or "")
                if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    deltas.append(event["delta"])
                elif kind == "response.completed":
                    value = event.get("response")
                    completed = value if isinstance(value, dict) else event
                elif kind in ("error", "response.failed", "response.incomplete"):
                    error = event.get("error") or event.get("response") or event
                    if isinstance(error, dict):
                        error = error.get("message") or error.get("code") or kind
                    raise SubscriptionUnavailable(
                        f"ChatGPT subscription stream failed: {error}"
                    )
            answer = "".join(deltas) or _output_text(completed)
            if not answer.strip():
                raise SubscriptionUnavailable("ChatGPT subscription returned no output")
            return answer.strip()
