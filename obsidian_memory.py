"""FlexFactor bridge to the shared Obsidian AI Bus.

Recall project continuity before an audit/repair/model task and remember only
non-sensitive verified conclusions. The shared vault never receives credentials
or end-user/source data.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib import request as urlrequest

APP = "flexfactor"
VAULT = os.environ.get("AIBUS_VAULT", r"G:\\Obsidian Vault").strip() or r"G:\\Obsidian Vault"
SCRIPT = os.environ.get("OBSIDIAN_MEMORY_AIBUS_PATH", "").strip() or str(Path(VAULT) / "AI Bus" / "aibus.py")
PYTHON = os.environ.get("OBSIDIAN_MEMORY_PYTHON", "").strip() or sys.executable
BLOCKED = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\\b(?:api[_-]?key|secret|token|password|authorization)\\b\\s*[:=]", re.I),
    re.compile(r"\\b(?:sk|rk|ghp|github_pat)_[A-Za-z0-9_-]{12,}"),
    re.compile(r"\\b(?:\\d[ -]*?){13,19}\\b"),
)

BRIDGE_URL = os.environ.get("OBSIDIAN_MEMORY_BRIDGE_URL", "").strip().rstrip("/")
BRIDGE_TOKEN = os.environ.get("OBSIDIAN_MEMORY_BRIDGE_TOKEN", "").strip()

def bridge_request(route: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if not BRIDGE_URL or not BRIDGE_TOKEN:
        return {"ok": False, "code": "bridge_credentials_missing", "detail": "Hosted memory bridge credentials are not configured."}
    data = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    request = urlrequest.Request(
        BRIDGE_URL + route,
        data=data,
        headers={"Authorization": "Bearer " + BRIDGE_TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "code": "bridge_unavailable", "detail": "Hosted memory bridge is unavailable."}
    if not isinstance(body, dict) or not body.get("ok"):
        return {"ok": False, "code": "bridge_unavailable", "detail": "Hosted memory bridge is unavailable."}
    return {"ok": True, "output": str(body.get("results") or body.get("detail") or "")}

def run_aibus(args: list[str], timeout_seconds: int = 30) -> Mapping[str, Any]:
    if not Path(SCRIPT).is_file():
        return {"ok": False, "code": "aibus_unavailable", "detail": f"AI Bus engine not found at {SCRIPT}"}
    try:
        done = subprocess.run([PYTHON, SCRIPT, *args], shell=False, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "code": "aibus_spawn_failed", "detail": str(exc)}
    if done.returncode:
        return {"ok": False, "code": "aibus_failed", "detail": (done.stderr or done.stdout or f"exit {done.returncode}").strip()[:500]}
    return {"ok": True, "output": done.stdout.strip()}

def recall(query: str, limit: int = 8) -> Mapping[str, Any]:
    clean = str(query or "").strip()
    if not clean:
        return {"ok": False, "code": "empty_query", "detail": "A recall query is required."}
    try:
        safe_limit = max(1, min(10, int(limit)))
    except (TypeError, ValueError):
        safe_limit = 8
    result = bridge_request("/v1/recall", {"project": APP, "query": clean, "limit": safe_limit}) if BRIDGE_URL else run_aibus(
        ["recall", "--limit", str(safe_limit), APP, *clean.split()]
    )
    return {"ok": True, "query": clean, "results": result.get("output") or "(nothing in the vault matches)"} if result.get("ok") else result

def remember(title: str, content: str, tag: str = "project") -> Mapping[str, Any]:
    heading, body = str(title or "").strip(), str(content or "").strip()
    combined = heading + "\n" + body
    if not heading or not body:
        return {"ok": False, "code": "empty_memory", "detail": "A title and non-empty project lesson are required."}
    if len(combined) > 4000 or any(pattern.search(combined) for pattern in BLOCKED):
        return {"ok": False, "code": "unsafe_memory", "detail": "Shared memory rejects sensitive, secret-bearing, or oversized content."}
    result = bridge_request("/v1/note", {
        "project": APP, "agent": os.environ.get("OBSIDIAN_MEMORY_AGENT", APP), "title": heading, "content": body, "tag": tag or "project"
    }) if BRIDGE_URL else run_aibus(
        ["note", "--from", os.environ.get("OBSIDIAN_MEMORY_AGENT", APP), "--title", f"[{APP}] {heading}", "--tag", tag or "project", body]
    )
    return {"ok": True, "title": heading, "detail": result.get("output")} if result.get("ok") else result

def startup() -> Mapping[str, Any]:
    result = recall("continuity decisions blockers", limit=1)
    print("[obsidian-memory] recall available" if result.get("ok") else f"[obsidian-memory] unavailable: {result.get('detail', result.get('code'))}", file=sys.stderr)
    return result

if __name__ == "__main__":
    result = startup()
    raise SystemExit(0)
