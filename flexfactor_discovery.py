"""Route-catalog discovery for flexfactor_rotation.

Reads ai-time config.json, Obsidian vault provider notes, and local Cursor
config locations and produces a `catalog.auto.json` in the shape that
`flexfactor_rotation.load_catalog` already understands.

SAFETY CONTRACT
---------------
- Never reads, logs, or emits secrets.  API key fields in config files are
  skipped; the catalog records only the *shape* of a route (backend, model,
  pool, cost class, tier) and never the credential needed to call it.
- All I/O is read-only and fail-soft: a missing file, a bad JSON parse, or an
  unreadable directory is logged to stderr and skipped; it never aborts
  discovery of the remaining sources.
- Cursor detection never starts any process unless `FLEXFACTOR_CURSOR_PROBE=1`
  is set explicitly.  Default is discover-only (reads config files, no
  subprocess invocation).

FEATURE FLAG
------------
This module is activated only when `FLEXFACTOR_ROTATION_EXTENSIONS=1` (env).
`discover_routes()` returns an empty list when the flag is absent so callers
need no conditional logic; `write_auto_catalog()` is likewise a no-op.

CONFIGURATION
-------------
FLEXFACTOR_AITIME_CONFIG   – absolute path to ai-time's config.json
                             (default: auto-detect in ../ai-time/config.json
                             relative to this file, then $AITIME_STATE_DIR)
FLEXFACTOR_CURSOR_PROBE=1  – allow subprocess queries to the Cursor daemon
PURPOSE_FOUNDRY_OBSIDIAN_INBOX – Obsidian vault inbox directory
FLEXFACTOR_ROTATION_EXTENSIONS=1 – master flag (must be 1 to activate)
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_SENTINEL_VALUES = frozenset({"", "YOUR_KEY_HERE", "sk-...", "sk-ant-...",
                               "placeholder", "change_me", "todo"})


def _looks_like_secret(value: Any) -> bool:
    """Return True for anything that could be a real credential string."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if v in _SENTINEL_VALUES or not v:
        return False
    # Anything that looks like an API key format.
    if any(v.startswith(p) for p in ("sk-", "sk-ant-", "Bearer ", "eyJ")):
        return True
    if len(v) >= 32 and v.replace("-", "").replace("_", "").isalnum():
        return True
    return False


def _warn(msg: str) -> None:
    print(f"[discovery] WARNING: {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"[discovery] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Feature flag
# --------------------------------------------------------------------------- #

def extensions_enabled() -> bool:
    """Return True only when the caller explicitly opts in."""
    return os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS", "").strip() == "1"


# --------------------------------------------------------------------------- #
# Route-entry factory (mirrors flexfactor_rotation.Route fields)
# --------------------------------------------------------------------------- #

def _route_entry(
    rid: str,
    backend: str,
    model: str,
    pool: str,
    cost_class: str,          # "local-unlimited" | "subscription" | "free-tier" | "paid-metered"
    tier: str,                # "frontier" | "strong" | "light"
    api: str = "openai",
    base_url: str = "",
    source: str = "",
) -> Dict[str, Any]:
    return {
        "id": rid,
        "backend": backend,
        "backend_label": backend,
        "model": model,
        "wire_model": model,
        "api": api,
        "base_url": base_url,
        "pool": pool,
        "cost_class": cost_class,
        "tier": tier,
        "enabled": True,
        "_source": source,          # discovery provenance, not used by rotator
    }


# --------------------------------------------------------------------------- #
# ai-time config.json reader
# --------------------------------------------------------------------------- #

def _aitime_config_path() -> Optional[str]:
    explicit = os.environ.get("FLEXFACTOR_AITIME_CONFIG", "").strip()
    if explicit:
        return explicit

    # Sibling-repo heuristic: ai-time/ next to this file's parent.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "ai-time", "config.json"),
        os.path.join(here, "..", "..", "ai-time", "config.json"),
    ]
    state_dir = os.environ.get("AITIME_STATE_DIR", "")
    if state_dir:
        candidates.append(os.path.join(state_dir, "config.json"))
    for c in candidates:
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


# Known ai-time config keys that may hold a bearer-style credential — never copied.
_SECRET_KEYS = frozenset({"api_key", "apiKey", "key", "token", "secret",
                           "bearer", "password", "auth"})


def _safe_str(raw: Any) -> str:
    """Return a string only when it is safe to record."""
    if not isinstance(raw, str):
        return ""
    if _looks_like_secret(raw):
        return ""
    return raw.strip()


_AITIME_COST_MAP = {
    "free": "free-tier",
    "subscription": "subscription",
    "paid": "paid-metered",
    "local": "local-unlimited",
    "unlimited": "local-unlimited",
}

_AITIME_TIER_MAP = {
    "frontier": "frontier",
    "strong": "strong",
    "light": "light",
    "fast": "light",
    "mini": "light",
}


def _parse_aitime_entry(entry: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    """Convert one ai-time config provider entry into a route dict, or None."""
    if not isinstance(entry, dict):
        return None

    # Skip purely-browser entries (no base_url / api_type that we can drive).
    api_type = _safe_str(entry.get("api_type") or entry.get("apiType") or "openai")
    if api_type not in ("openai", "anthropic", "ollama", "cursor"):
        return None

    base_url = _safe_str(entry.get("base_url") or entry.get("baseUrl") or "")
    model = _safe_str(entry.get("model") or entry.get("modelId") or "")
    name = _safe_str(entry.get("name") or entry.get("label") or entry.get("id") or "")
    if not name and not model:
        return None

    backend = _safe_str(entry.get("backend") or entry.get("provider") or name or "")
    if not backend:
        backend = name or model.split("/")[0] or f"aitime-{idx}"

    pool = _safe_str(entry.get("pool") or entry.get("quota_group") or f"aitime:{backend}")

    cost_raw = _safe_str(entry.get("cost_class") or entry.get("cost") or
                          entry.get("billing") or "")
    cost_class = _AITIME_COST_MAP.get(cost_raw.lower(), "paid-metered")

    tier_raw = _safe_str(entry.get("tier") or entry.get("capability") or "")
    tier = _AITIME_TIER_MAP.get(tier_raw.lower(), "frontier")

    rid = f"{backend}/{model}" if model else backend
    return _route_entry(rid, backend, model, pool, cost_class, tier,
                        api=api_type, base_url=base_url, source="aitime")


def discover_from_aitime() -> List[Dict[str, Any]]:
    """Return route dicts sourced from ai-time's config.json."""
    path = _aitime_config_path()
    if path is None:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"could not read ai-time config {path!r}: {exc}")
        return []

    entries = raw if isinstance(raw, list) else raw.get("providers", []) or raw.get("models", [])
    routes: List[Dict[str, Any]] = []
    for i, entry in enumerate(entries):
        r = _parse_aitime_entry(entry, i)
        if r:
            routes.append(r)
    _info(f"ai-time: {len(routes)} route(s) from {path}")
    return routes


# --------------------------------------------------------------------------- #
# Cursor config reader
# --------------------------------------------------------------------------- #

_CURSOR_TIERS = {
    # Names that suggest frontier-class capability.
    "claude-opus": "frontier",
    "claude-3-opus": "frontier",
    "claude-3-5-sonnet": "frontier",
    "claude-sonnet-4": "frontier",
    "claude-opus-4": "frontier",
    "gpt-4o": "frontier",
    "gpt-4.5": "frontier",
    "o3": "frontier",
    "o4": "frontier",
    # Strong.
    "claude-haiku": "strong",
    "claude-3-haiku": "strong",
    "gpt-4o-mini": "strong",
    "gpt-4-turbo": "strong",
    "o1": "strong",
    "o3-mini": "strong",
}


def _cursor_tier(model_id: str) -> str:
    lower = model_id.lower()
    for prefix, tier in _CURSOR_TIERS.items():
        if lower.startswith(prefix):
            return tier
    return "strong"


def _cursor_config_dirs() -> List[str]:
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(home, ".cursor"),
        os.path.join(home, ".cursor", "agents"),
    ]
    # APPDATA on Windows.
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(os.path.join(appdata, "Cursor", "User"))
    # VS Code-style settings location used by Cursor on some systems.
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        dirs.append(os.path.join(local_appdata, "Cursor", "User"))
    return dirs


def _extract_models_from_cursor_json(data: Any) -> List[str]:
    """Pull model IDs out of a Cursor settings/agents JSON blob."""
    models: List[str] = []
    if isinstance(data, list):
        for item in data:
            models.extend(_extract_models_from_cursor_json(item))
        return models
    if isinstance(data, dict):
        for key in ("model", "models", "modelId", "selectedModel", "defaultModel"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                models.append(val.strip())
            elif isinstance(val, list):
                for m in val:
                    if isinstance(m, str) and m.strip():
                        models.append(m.strip())
        for v in data.values():
            if isinstance(v, (dict, list)):
                models.extend(_extract_models_from_cursor_json(v))
    return models


def _cursor_models_from_config_files() -> List[str]:
    """Read known Cursor config file locations and collect model IDs."""
    models: List[str] = []
    config_filenames = ("settings.json", "agents.json", "mcp.json",
                        "cursor-settings.json", "cursor_models.json")
    for d in _cursor_config_dirs():
        if not os.path.isdir(d):
            continue
        for fname in config_filenames:
            fpath = os.path.join(d, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, encoding="utf-8") as fh:
                    data = json.load(fh)
                found = _extract_models_from_cursor_json(data)
                if found:
                    _info(f"cursor: {len(found)} model(s) from {fpath}")
                models.extend(found)
            except (OSError, json.JSONDecodeError) as exc:
                _warn(f"could not read {fpath!r}: {exc}")
    return models


def _cursor_models_from_rules_files() -> List[str]:
    """Scan .cursor/rules files for model mentions (best-effort)."""
    models: List[str] = []
    search_dirs = _cursor_config_dirs()
    cwd = os.getcwd()
    search_dirs.append(os.path.join(cwd, ".cursor", "rules"))

    known_models = list(_CURSOR_TIERS.keys()) + [
        "cursor-small", "cursor-fast", "claude-3-7-sonnet", "gemini-2.5-pro",
        "gemini-2.5-flash", "deepseek-r1", "deepseek-coder",
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fname in entries:
            if not fname.endswith((".md", ".txt", ".mdc", ".rules")):
                continue
            fpath = os.path.join(d, fname)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(8192)
                for m in known_models:
                    if m in text and m not in models:
                        models.append(m)
            except OSError:
                continue
    return models


def _cursor_models_from_daemon() -> List[str]:
    """Query the Cursor daemon/CLI for configured models.

    Only runs when FLEXFACTOR_CURSOR_PROBE=1.  Falls back to an empty list on
    any error — a missing Cursor binary is never a fatal error.
    """
    if os.environ.get("FLEXFACTOR_CURSOR_PROBE", "").strip() != "1":
        return []
    for exe in ("cursor", "cursor-agent"):
        try:
            result = subprocess.run(
                [exe, "--list-models"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                _info(f"cursor daemon: {len(lines)} model(s) via `{exe} --list-models`")
                return lines
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    return []


def _dedupe(models: List[str]) -> List[str]:
    seen: List[str] = []
    for m in models:
        if m not in seen:
            seen.append(m)
    return seen


def discover_from_cursor() -> List[Dict[str, Any]]:
    """Return route dicts for every Cursor model discovered."""
    all_models: List[str] = []
    all_models.extend(_cursor_models_from_config_files())
    all_models.extend(_cursor_models_from_rules_files())
    all_models.extend(_cursor_models_from_daemon())
    all_models = _dedupe(all_models)

    if not all_models:
        # Surface a minimal default set so the catalog is never entirely empty
        # when Cursor is likely installed but no config files were found.
        import shutil
        if shutil.which("cursor") or shutil.which("cursor-agent"):
            all_models = ["claude-3-5-sonnet", "gpt-4o", "cursor-small"]
            _info(f"cursor: using default model set (Cursor binary found, no config parsed)")

    routes: List[Dict[str, Any]] = []
    for model in all_models:
        tier = _cursor_tier(model)
        rid = f"cursor/{model}"
        routes.append(_route_entry(
            rid=rid, backend="cursor", model=model,
            pool="cursor:subscription",
            cost_class="subscription",
            tier=tier,
            api="cursor",
            base_url="",
            source="cursor",
        ))
    if routes:
        _info(f"cursor: {len(routes)} route(s) discovered")
    return routes


# --------------------------------------------------------------------------- #
# Obsidian vault reader
# --------------------------------------------------------------------------- #

def _obsidian_inbox_dir() -> Optional[str]:
    d = os.environ.get("PURPOSE_FOUNDRY_OBSIDIAN_INBOX", "").strip()
    return d if d and os.path.isdir(d) else None


def discover_from_obsidian() -> List[Dict[str, Any]]:
    """Scan the Obsidian vault inbox for provider/model notes.

    Best-effort: any markdown file whose name contains 'cursor', 'model',
    'provider', or 'ai' is scanned for model ID strings.
    """
    inbox = _obsidian_inbox_dir()
    if inbox is None:
        return []

    routes: List[Dict[str, Any]] = []
    known_models = list(_CURSOR_TIERS.keys()) + [
        "cursor-small", "claude-3-7-sonnet", "gemini-2.5-pro",
        "gemini-2.5-flash", "deepseek-r1",
    ]
    keywords = ("cursor", "model", "provider", "ai", "llm")

    try:
        entries = os.listdir(inbox)
    except OSError as exc:
        _warn(f"cannot list Obsidian inbox {inbox!r}: {exc}")
        return []

    found_models: List[str] = []
    for fname in entries:
        if not any(kw in fname.lower() for kw in keywords):
            continue
        if not fname.endswith((".md", ".txt", ".json")):
            continue
        fpath = os.path.join(inbox, fname)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                text = fh.read(16384)
        except OSError:
            continue
        for m in known_models:
            if m in text and m not in found_models:
                found_models.append(m)

    for model in found_models:
        tier = _cursor_tier(model)
        rid = f"cursor/{model}"
        routes.append(_route_entry(
            rid=rid, backend="cursor", model=model,
            pool="cursor:subscription",
            cost_class="subscription",
            tier=tier,
            api="cursor",
            base_url="",
            source="obsidian",
        ))
    if routes:
        _info(f"obsidian: {len(routes)} route(s) from {inbox}")
    return routes


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def discover_routes() -> List[Dict[str, Any]]:
    """Aggregate routes from all discovery sources.

    Returns an empty list when `FLEXFACTOR_ROTATION_EXTENSIONS=1` is not set.
    """
    if not extensions_enabled():
        return []

    routes: List[Dict[str, Any]] = []
    routes.extend(discover_from_aitime())
    routes.extend(discover_from_cursor())
    routes.extend(discover_from_obsidian())

    # Deduplicate by route id, preferring later entries (more specific source).
    seen: Dict[str, Dict[str, Any]] = {}
    for r in routes:
        seen[r["id"]] = r
    return list(seen.values())


def _auto_catalog_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "catalog.auto.json")


def write_auto_catalog(
    routes: Optional[List[Dict[str, Any]]] = None,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """Write `catalog.auto.json` in the flexfactor_rotation catalog shape.

    Returns the path written, or None when extensions are disabled or there
    are no routes to write.
    """
    if not extensions_enabled():
        return None

    if routes is None:
        routes = discover_routes()
    if not routes:
        return None

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    catalog_blob = {
        "schema": 1,
        "generated_at": now,
        "routes": routes,
    }
    path = output_path or _auto_catalog_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(catalog_blob, fh, indent=2)
        os.replace(tmp, path)
        _info(f"wrote {len(routes)} route(s) to {path}")
        return path
    except OSError as exc:
        _warn(f"could not write auto catalog to {path!r}: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


def load_auto_catalog() -> List[Dict[str, Any]]:
    """Read routes from `catalog.auto.json` if it exists; else return []."""
    path = _auto_catalog_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        return blob.get("routes", [])
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"could not read auto catalog {path!r}: {exc}")
        return []


# --------------------------------------------------------------------------- #
# CLI entry point  (python -m flexfactor_discovery  or  python flexfactor_discovery.py)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    if not extensions_enabled():
        print("Set FLEXFACTOR_ROTATION_EXTENSIONS=1 to enable discovery.", file=sys.stderr)
        sys.exit(0)
    written = write_auto_catalog()
    if written:
        print(f"Discovery complete. Catalog written to: {written}")
    else:
        print("No routes discovered.", file=sys.stderr)
