#!/usr/bin/env python3
"""Fail-closed live Cursor/AI-Time authentication and inference proof.

The probe sends no repository source, user data, or credentials in its prompt.
It first requires the endpoint to reject an unauthenticated completion request,
then sends one authenticated nonce and requires the exact nonce in the result.
Only redacted, deterministic evidence is written to stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class LiveProofFailure(RuntimeError):
    """The live proof did not establish every required property."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "")).strip()
    if not value:
        raise LiveProofFailure(f"required configuration is absent: {name}")
    return value


def _catalog_path(env: Mapping[str, str]) -> Path:
    explicit = str(env.get("AI_ROTATE_CATALOG", "")).strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    state = str(env.get("AITIME_STATE_DIR", "")).strip()
    if state:
        return (Path(state).expanduser() / "routes.json").resolve()
    local = str(env.get("LOCALAPPDATA", "")).strip()
    root = Path(local).expanduser() if local else Path.home()
    return (root / "AITime" / "routes.json").resolve()


def _load_cursor_route(env: Mapping[str, str]) -> tuple[dict[str, Any], Path, float, str]:
    path = _catalog_path(env)
    try:
        raw_bytes = path.read_bytes()
        catalog = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveProofFailure(f"AI-Time catalog is unreadable: {path}: {exc}") from exc
    if not isinstance(catalog, dict) or catalog.get("schema") != 1:
        raise LiveProofFailure(f"AI-Time catalog has unsupported schema: {path}")

    try:
        maximum_age = int(str(env.get("FLEXFACTOR_LIVE_CATALOG_MAX_AGE_S", "86400")))
        generated = datetime.fromisoformat(str(catalog.get("generated_at", "")))
        if generated.tzinfo is None:
            raise ValueError("timezone is absent")
        now = datetime.now(timezone.utc).timestamp()
        generated_at = generated.timestamp()
    except (TypeError, ValueError) as exc:
        raise LiveProofFailure("AI-Time catalog has no valid generated_at timestamp") from exc
    if maximum_age <= 0:
        raise LiveProofFailure("FLEXFACTOR_LIVE_CATALOG_MAX_AGE_S must be positive")
    if generated_at > now + 300:
        raise LiveProofFailure("AI-Time catalog generated_at is implausibly in the future")
    age = max(0.0, now - generated_at)
    if age > maximum_age:
        raise LiveProofFailure(
            f"AI-Time catalog is stale: age={age:.0f}s limit={maximum_age}s"
        )

    requested = str(env.get("FLEXFACTOR_CURSOR_ROUTE_ID", "")).strip()
    routes = []
    for row in catalog.get("routes") or []:
        if not isinstance(row, dict) or not row.get("enabled", True):
            continue
        api = str(row.get("api", "")).lower()
        backend = str(row.get("backend", "")).lower()
        if api == "cursor" or backend == "cursor":
            routes.append(row)
    if requested:
        routes = [row for row in routes if str(row.get("id", "")) == requested]
    if len(routes) != 1:
        qualifier = f" matching {requested!r}" if requested else ""
        raise LiveProofFailure(
            f"expected exactly one enabled Cursor route{qualifier}; found {len(routes)}"
        )
    return routes[0], path, age, hashlib.sha256(raw_bytes).hexdigest()


def _validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LiveProofFailure("FLEXFACTOR_CURSOR_BASE_URL must be an HTTP(S) endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LiveProofFailure("Cursor base URL must not embed credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LiveProofFailure("remote Cursor endpoints must use HTTPS")
    return value.rstrip("/")


def _post(url: str, payload: dict[str, Any], token: str, timeout: float) -> tuple[int, Any]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise LiveProofFailure(f"Cursor endpoint is unreachable: {type(exc).__name__}") from exc
    try:
        parsed: Any = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
    return status, parsed


def run_live_proof(
    env: Mapping[str, str] | None = None,
    *,
    nonce: str | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    base_url = _validate_base_url(_required(env, "FLEXFACTOR_CURSOR_BASE_URL"))
    api_key = _required(env, "FLEXFACTOR_CURSOR_API_KEY")
    route, catalog_path, age, catalog_sha = _load_cursor_route(env)
    route_base = str(route.get("base_url", "")).rstrip("/")
    if route_base and route_base != base_url:
        raise LiveProofFailure(
            "configured endpoint does not match the selected AI-Time Cursor route"
        )
    model = str(route.get("wire_model") or route.get("model") or "").strip()
    route_id = str(route.get("id") or "").strip()
    if not model or not route_id:
        raise LiveProofFailure("selected AI-Time Cursor route has no id/model")

    timeout = float(str(env.get("FLEXFACTOR_LIVE_PROBE_TIMEOUT_S", "60")))
    if timeout <= 0:
        raise LiveProofFailure("FLEXFACTOR_LIVE_PROBE_TIMEOUT_S must be positive")
    completion_url = base_url + "/chat/completions"
    unauth_payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reject this unauthenticated probe."}],
        "max_tokens": 1,
        "temperature": 0,
    }
    unauth_status, _ = _post(completion_url, unauth_payload, "", timeout)
    if unauth_status not in {401, 403}:
        raise LiveProofFailure(
            f"authentication boundary not proven: unauthenticated status={unauth_status}"
        )

    nonce = nonce or secrets.token_hex(16)
    expected = "FLEXFACTOR_LIVE_OK:" + nonce
    auth_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return only the exact sentinel requested. No markdown or explanation.",
            },
            {"role": "user", "content": "Return exactly: " + expected},
        ],
        "max_tokens": 64,
        "temperature": 0,
    }
    started = time.monotonic()
    auth_status, response = _post(completion_url, auth_payload, api_key, timeout)
    duration_ms = round((time.monotonic() - started) * 1000)
    if auth_status != 200:
        raise LiveProofFailure(f"authenticated inference failed: status={auth_status}")
    try:
        content = str(response["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LiveProofFailure("authenticated response has no completion content") from exc
    if content != expected:
        raise LiveProofFailure("authenticated inference did not return the exact sentinel")

    return {
        "schema": 1,
        "live": True,
        "authentication": {
            "unauthenticated_status": unauth_status,
            "authenticated_status": auth_status,
            "bearer_boundary_proven": True,
        },
        "catalog": {
            "path_name": catalog_path.name,
            "sha256": catalog_sha,
            "age_seconds": round(age),
            "route_id": route_id,
        },
        "inference": {
            "model": model,
            "exact_sentinel": True,
            "response_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "duration_ms": duration_ms,
        },
    }


def main() -> int:
    try:
        evidence = run_live_proof()
    except (LiveProofFailure, ValueError) as exc:
        print(f"live Cursor/AI-Time proof FAILED: {exc}", file=sys.stderr)
        return 1
    output = json.dumps(evidence, indent=2, sort_keys=True)
    api_key = os.environ.get("FLEXFACTOR_CURSOR_API_KEY", "")
    if api_key and api_key in output:
        print("live Cursor/AI-Time proof FAILED: credential entered evidence", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
