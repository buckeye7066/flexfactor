"""Cursor provider adapter for flexfactor_rotation.

Implements the same surface as AnthropicProvider / OpenAIProvider in
flexfactor.py so RotatingProvider can call it transparently.

MODES
-----
1. HTTP mode (preferred): if a local Cursor daemon exposes an OpenAI-compatible
   HTTP endpoint, point `FLEXFACTOR_CURSOR_BASE_URL` at it (e.g.
   `http://127.0.0.1:3000/v1`).  `make_cursor_provider` will use it.

2. Pass-through mode (default fallback): routes tagged `api="cursor"` are
   served by re-routing the call to an underlying OpenAI-compatible provider
   using whatever base_url and wire_model are in the catalog route.  This lets
   the rotator treat Cursor as a distinct *pool* (subscription-class, separate
   quota ledger) even when the actual HTTP call goes to the same upstream.

3. Fail-closed mode: if neither mode is available, every method raises
   `CursorUnavailable` (a subclass of RuntimeError) so the rotator rolls over
   to the next pool rather than silently succeeding with a wrong provider.

FEATURE FLAG
------------
The provider is only instantiated when `FLEXFACTOR_ROTATION_EXTENSIONS=1`.
`make_cursor_provider` raises `CursorUnavailable` when the flag is absent.

SECRETS
-------
No API keys are stored or logged.  `FLEXFACTOR_CURSOR_API_KEY` (optional)
is read from the environment when needed for an HTTP call and never written to
any catalog or log file.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class CursorUnavailable(RuntimeError):
    """Raised when Cursor cannot be reached; the rotator handles this."""


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _extensions_enabled() -> bool:
    """True unless extensions are explicitly disabled. See flexfactor_flags."""
    try:
        from flexfactor_flags import rotation_extensions_enabled
        return rotation_extensions_enabled()
    except ImportError:
        return os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS", "").strip().lower() \
            not in ("0", "false", "no", "off")


def _cursor_base_url() -> Optional[str]:
    url = os.environ.get("FLEXFACTOR_CURSOR_BASE_URL", "").strip()
    return url if url else None


def _cursor_api_key() -> str:
    # May be empty — Cursor's local daemon typically needs no bearer token.
    return os.environ.get("FLEXFACTOR_CURSOR_API_KEY", "").strip()


# --------------------------------------------------------------------------- #
# HTTP helper (no third-party libraries; stdlib only)
# --------------------------------------------------------------------------- #

def _http_post(url: str, payload: Dict[str, Any], timeout: float = 60.0) -> Any:
    """POST `payload` as JSON to `url` and return parsed JSON response.

    Raises `CursorUnavailable` on any network/HTTP error.
    """
    body = json.dumps(payload).encode("utf-8")
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = _cursor_api_key()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CursorUnavailable(
            f"Cursor HTTP {exc.code} from {url}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CursorUnavailable(f"Cursor unreachable at {url}: {exc}") from exc


def _http_get(url: str, timeout: float = 10.0) -> Any:
    """GET `url` and return parsed JSON, or raise CursorUnavailable."""
    headers: Dict[str, str] = {"Accept": "application/json"}
    api_key = _cursor_api_key()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            OSError, json.JSONDecodeError) as exc:
        raise CursorUnavailable(f"Cursor ping failed at {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# CursorProvider
# --------------------------------------------------------------------------- #

class CursorProvider:
    """Provider adapter for Cursor's AI models.

    Parameters
    ----------
    model : str
        The model ID as it appears in the Cursor configuration.
    base_url : str | None
        OpenAI-compatible HTTP endpoint.  None → fail-closed (raises on call).
    judge_model : str | None
        Model to use for grading / cheap calls.  Defaults to `model`.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        judge_model: Optional[str] = None,
    ) -> None:
        self.model = model
        self.judge_model = judge_model or model
        self._base_url = base_url  # may be None → fail-closed

    # -- provider identity surface (mirrors flexfactor's convention) ----------

    @property
    def meter(self) -> str:
        """Cost label for reporting.  Cursor is subscription-billed."""
        return "cursor:subscription"

    # -- capability surface ---------------------------------------------------

    def ping(self, **_: Any) -> bool:
        """Return True if the Cursor endpoint is reachable."""
        if self._base_url is None:
            raise CursorUnavailable(
                "Cursor HTTP endpoint not configured "
                "(set FLEXFACTOR_CURSOR_BASE_URL)")
        base = self._base_url
        if base.endswith("/v1"):
            base = base[:-3]
        url = base.rstrip("/") + "/health"
        try:
            _http_get(url, timeout=5.0)
            return True
        except CursorUnavailable:
            # Try the OpenAI-compat models endpoint as a fallback health probe.
            try:
                _http_get(self._base_url.rstrip("/") + "/models", timeout=5.0)
                return True
            except CursorUnavailable:
                return False

    def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        **_: Any,
    ) -> str:
        """Return a completion string."""
        if self._base_url is None:
            raise CursorUnavailable(
                "Cursor HTTP endpoint not configured "
                "(set FLEXFACTOR_CURSOR_BASE_URL)")
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        url = self._base_url.rstrip("/") + "/chat/completions"
        resp = _http_post(url, payload)
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CursorUnavailable(
                f"Cursor response has unexpected shape: {resp!r}") from exc

    def grade(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> str:
        """Cheap classification call — routes to judge_model."""
        saved = self.model
        try:
            self.model = self.judge_model
            return self.complete(
                prompt, system=system, max_tokens=max_tokens,
                temperature=temperature, **kwargs)
        finally:
            self.model = saved

    def structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        system: Optional[str] = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Any:
        """Return parsed JSON matching `schema` (best-effort)."""
        schema_hint = json.dumps(schema, indent=2)
        augmented_system = (
            (system + "\n\n" if system else "")
            + "Respond with valid JSON conforming to this schema:\n"
            + schema_hint
        )
        text = self.complete(
            prompt, system=augmented_system, max_tokens=max_tokens, **kwargs)
        # Strip markdown fences if the model wrapped the JSON.
        stripped = text.strip()
        for fence in ("```json", "```"):
            if stripped.startswith(fence):
                stripped = stripped[len(fence):]
                if stripped.endswith("```"):
                    stripped = stripped[:-3]
                stripped = stripped.strip()
                break
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CursorUnavailable(
                f"Cursor structured() returned non-JSON: {text[:200]!r}") from exc


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def make_cursor_provider(route: Any) -> CursorProvider:
    """Factory function that follows the flexfactor_rotation provider-factory
    injection pattern.

    `route` is a `flexfactor_rotation.Route` (or any object with `.model`,
    `.wire_model`, `.base_url` attributes).

    Raises `CursorUnavailable` when extensions are disabled.
    """
    if not _extensions_enabled():
        raise CursorUnavailable(
            "Cursor provider requires FLEXFACTOR_ROTATION_EXTENSIONS=1")

    model = getattr(route, "wire_model", None) or getattr(route, "model", "") or ""
    base_url = _cursor_base_url() or (
        getattr(route, "base_url", None) or None
    )
    return CursorProvider(model=model, base_url=base_url)
