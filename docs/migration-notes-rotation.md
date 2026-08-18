# Migration Notes — Rotation Extensions & Cursor Provider

**Version introduced:** 0.4.2 (feature branch `feature/rotation-cursor-competitors`)

---

## What changed

### 1. Feature flag — nothing changes by default

All new behaviour is gated behind a single environment variable:

```
FLEXFACTOR_ROTATION_EXTENSIONS=1
```

If this variable is absent or set to anything other than the exact string `"1"`,
FlexFactor behaves identically to previous versions.  No existing code paths are
altered.

---

### 2. New module: `flexfactor_discovery.py`

Discovers model routes from three sources and writes `catalog.auto.json` in the
shape that `flexfactor_rotation.load_catalog` already understands.

| Source | Config | What is read |
|--------|--------|--------------|
| **ai-time** | `FLEXFACTOR_AITIME_CONFIG` or auto-detected sibling repo path | `config.json` provider entries (model, pool, cost class, tier) |
| **Cursor** | `~/.cursor/settings.json`, `~/.cursor/agents.json`, per-workspace `.cursor/rules/` | model IDs configured in Cursor |
| **Obsidian vault** | `PURPOSE_FOUNDRY_OBSIDIAN_INBOX` | markdown files mentioning model names |

**Safety rules:**
- API key fields are never copied into the catalog.
- No subprocess is ever started unless `FLEXFACTOR_CURSOR_PROBE=1` is also set.
- Every source failure is logged to stderr and skipped; it never aborts discovery.

**Run manually:**

```bash
FLEXFACTOR_ROTATION_EXTENSIONS=1 python flexfactor_discovery.py
# → writes catalog.auto.json next to flexfactor_discovery.py
```

---

### 3. `flexfactor_rotation.load_catalog` now merges auto-discovered routes

When `FLEXFACTOR_ROTATION_EXTENSIONS=1`:

1. The primary catalog file is read as before.
2. `catalog.auto.json` (if present) is read and any routes with IDs not already
   in the primary catalog are appended.
3. All downstream rotation logic (pool-first, cooldown, strikes) applies equally
   to the merged routes.

When the flag is absent: `load_catalog` behaves exactly as before.

---

### 4. New provider adapter: `providers/cursor_provider.py`

Implements `CursorProvider` with the same surface as `AnthropicProvider` /
`OpenAIProvider` (`complete`, `grade`, `structured`, `ping`, `meter`).

**Modes:**

| Mode | Trigger | Description |
|------|---------|-------------|
| HTTP | `FLEXFACTOR_CURSOR_BASE_URL=http://...` | Routes calls to an OpenAI-compatible endpoint (local Cursor daemon or proxy) |
| Fail-closed | No base URL | Every method raises `CursorUnavailable`; the rotator rolls over to the next pool |

**Factory injection pattern:**

```python
from providers.cursor_provider import make_cursor_provider
# pass to RotatingProvider via the factory= argument
rotating = RotatingProvider(rotator, factory=make_cursor_provider, ...)
```

**Optional API key:** set `FLEXFACTOR_CURSOR_API_KEY` if your Cursor daemon
requires a bearer token.  It is never logged or written to any file.

---

### 5. Competitor profiles: `competitors/default_profiles.json`

A JSON array of five competitor entries (Aider, SWE-agent, Cursor, Devin,
OpenHands), each with:

- `name`, `url`, `license`, `category`, `description`
- `notable_features` — list of differentiating capabilities
- `ideas` — proposals that the FlexFactor audit pipeline may incorporate,
  filtered through the purpose contract

This file is read-only data; no code in the main audit pipeline imports it
automatically yet.  It is intended for use by the Scout / competitor-integration
phase described in `flexfactor_competitors.py`.

---

### 6. New CI workflow: `.github/workflows/rotation.yml`

Runs on every push to `main` and on PR to `main`.

| Job | What it does |
|-----|-------------|
| `unit-tests` | Imports new modules, runs existing rotation tests, runs extension and cursor-provider tests (Ubuntu + Windows) |
| `integration-tests` | Runs discovery without network; validates `catalog.auto.json` shape (only when `CI_INTEGRATION=1`) |
| `lint` | `py_compile` syntax check on all new files |

---

## Migration steps

### Upgrading from ≤ 0.4.1

No action required.  The flag is off by default.

### Enabling Cursor rotation

1. Install and configure Cursor on your machine.
2. Set the environment variable:
   ```
   FLEXFACTOR_ROTATION_EXTENSIONS=1
   ```
3. (Optional) Run discovery to pre-build the catalog:
   ```bash
   python flexfactor_discovery.py
   ```
4. (Optional) If Cursor exposes a local HTTP daemon:
   ```
   FLEXFACTOR_CURSOR_BASE_URL=http://127.0.0.1:3000/v1
   ```
5. Launch FlexFactor as normal.  Cursor routes will participate in pool-first
   rotation alongside your existing providers.

### Enabling ai-time integration

Point `FLEXFACTOR_AITIME_CONFIG` at your `ai-time/config.json`:

```
FLEXFACTOR_AITIME_CONFIG=C:\Users\firer\ai-time\config.json
FLEXFACTOR_ROTATION_EXTENSIONS=1
```

Then run `python flexfactor_discovery.py` to build the catalog.

### Enabling Obsidian vault discovery

```
PURPOSE_FOUNDRY_OBSIDIAN_INBOX=C:\Users\firer\ObsidianVault\Inbox
FLEXFACTOR_ROTATION_EXTENSIONS=1
```

---

## Rollback

Remove or unset `FLEXFACTOR_ROTATION_EXTENSIONS`.  All new behaviour is
completely disabled.  The `catalog.auto.json` file (if present) is inert without
the flag.

---

## Files added / changed

| File | Change |
|------|--------|
| `flexfactor_discovery.py` | **New** — discovery service |
| `providers/__init__.py` | **New** — providers namespace package |
| `providers/cursor_provider.py` | **New** — Cursor provider adapter |
| `competitors/default_profiles.json` | **New** — top-5 competitor profiles |
| `flexfactor_rotation.py` | **Extended** — `_rotation_extensions_enabled`, `_merge_auto_routes`, updated `load_catalog` docstring |
| `tests/test_rotation_extensions.py` | **New** — 22 unit tests |
| `tests/test_cursor_provider.py` | **New** — 27 unit tests |
| `.github/workflows/rotation.yml` | **New** — CI workflow |
| `docs/migration-notes-rotation.md` | **New** — this file |
