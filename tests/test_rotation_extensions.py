"""Tests for flexfactor_rotation extensions (auto-catalog merge, feature flag).

Runs fully offline.  No credentials, no network, no tokens spent.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# Ensure the repo root is on sys.path so flexfactor_rotation and
# flexfactor_discovery can be imported from a tests/ subdirectory.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import flexfactor_rotation as R
import flexfactor_discovery as D


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _route_entry(rid: str, pool: str, tier: str = R.FRONTIER,
                 cost: str = R.SUBSCRIPTION) -> dict:
    return {
        "id": rid, "backend": rid.split("/")[0],
        "backend_label": rid.split("/")[0],
        "model": rid.split("/", 1)[-1],
        "wire_model": rid.split("/", 1)[-1],
        "api": "openai",
        "base_url": "https://example.invalid/v1",
        "pool": pool, "cost_class": cost, "tier": tier, "enabled": True,
    }


def _write_catalog(path: str, entries: list) -> None:
    blob = {
        "schema": 1,
        "generated_at": "2026-08-18T00:00:00+00:00",
        "routes": entries,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)


# Aliases kept for call-site readability in tests that distinguish the two roles.
_make_auto_catalog = _write_catalog
_make_primary_catalog = _write_catalog


# --------------------------------------------------------------------------- #
# Feature-flag tests
# --------------------------------------------------------------------------- #

class ExtensionFlagTests(unittest.TestCase):
    """The feature flag must gate everything; nothing must change by default."""

    def setUp(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)

    def tearDown(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)

    def test_extensions_disabled_by_default(self):
        self.assertFalse(R._rotation_extensions_enabled())
        self.assertFalse(D.extensions_enabled())

    def test_extensions_enabled_by_env(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        self.assertTrue(R._rotation_extensions_enabled())
        self.assertTrue(D.extensions_enabled())

    def test_non_one_value_is_not_enabled(self):
        for val in ("true", "yes", "on", "1 ", " 1", "0", ""):
            os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = val
            # Whitespace is stripped; only '1' (after strip) enables the flag.
            expected = val.strip() == "1"
            self.assertEqual(R._rotation_extensions_enabled(), expected,
                             f"Expected {expected} for {val!r}")

    def test_discover_routes_returns_empty_when_flag_off(self):
        routes = D.discover_routes()
        self.assertEqual(routes, [])

    def test_write_auto_catalog_is_noop_when_flag_off(self):
        result = D.write_auto_catalog(routes=[{"id": "x"}])
        self.assertIsNone(result)


# --------------------------------------------------------------------------- #
# Auto-catalog merge in load_catalog
# --------------------------------------------------------------------------- #

class AutoCatalogMergeTests(unittest.TestCase):
    """load_catalog merges auto-discovered routes when the flag is on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = os.getcwd()
        # Point discovery auto-catalog path to our tmp dir by patching the
        # discovery module's _auto_catalog_path.
        self._real_auto_path = D._auto_catalog_path
        auto_json = os.path.join(self._tmp.name, "catalog.auto.json")
        D._auto_catalog_path = lambda: auto_json
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"

    def tearDown(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        D._auto_catalog_path = self._real_auto_path
        self._tmp.cleanup()

    def _primary_path(self) -> str:
        return os.path.join(self._tmp.name, "routes.json")

    def _auto_path(self) -> str:
        return D._auto_catalog_path()

    def test_auto_routes_are_added_to_primary_catalog(self):
        _make_primary_catalog(self._primary_path(), [
            _route_entry("openai/gpt-4o", "openai:paid", cost=R.PAID_METERED),
        ])
        _make_auto_catalog(self._auto_path(), [
            _route_entry("cursor/claude-3-5-sonnet", "cursor:subscription",
                         cost=R.SUBSCRIPTION),
        ])
        cat = R.load_catalog(self._primary_path())
        self.assertIsNotNone(cat)
        ids = {r.id for r in cat.routes}
        self.assertIn("openai/gpt-4o", ids)
        self.assertIn("cursor/claude-3-5-sonnet", ids)

    def test_duplicate_ids_are_not_doubled(self):
        _make_primary_catalog(self._primary_path(), [
            _route_entry("cursor/gpt-4o", "cursor:subscription", cost=R.SUBSCRIPTION),
        ])
        _make_auto_catalog(self._auto_path(), [
            _route_entry("cursor/gpt-4o", "cursor:subscription", cost=R.SUBSCRIPTION),
            _route_entry("cursor/claude-3-5-sonnet", "cursor:subscription",
                         cost=R.SUBSCRIPTION),
        ])
        cat = R.load_catalog(self._primary_path())
        ids = [r.id for r in cat.routes]
        self.assertEqual(ids.count("cursor/gpt-4o"), 1, "duplicate id should appear once")
        self.assertIn("cursor/claude-3-5-sonnet", ids)

    def test_missing_auto_catalog_is_not_fatal(self):
        _make_primary_catalog(self._primary_path(), [
            _route_entry("openai/gpt-4o", "openai:paid", cost=R.PAID_METERED),
        ])
        # Do NOT write the auto catalog — it should be a soft miss.
        cat = R.load_catalog(self._primary_path())
        self.assertIsNotNone(cat)
        self.assertEqual(len(cat.routes), 1)

    def test_flag_off_skips_auto_routes(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        _make_primary_catalog(self._primary_path(), [
            _route_entry("openai/gpt-4o", "openai:paid", cost=R.PAID_METERED),
        ])
        _make_auto_catalog(self._auto_path(), [
            _route_entry("cursor/claude-3-5-sonnet", "cursor:subscription",
                         cost=R.SUBSCRIPTION),
        ])
        cat = R.load_catalog(self._primary_path())
        self.assertIsNotNone(cat)
        ids = {r.id for r in cat.routes}
        self.assertNotIn("cursor/claude-3-5-sonnet", ids)


# --------------------------------------------------------------------------- #
# Pool-first rotation with mixed pools (local / paid / cursor)
# --------------------------------------------------------------------------- #

class MixedPoolRotationTests(unittest.TestCase):
    """Pool-first semantics hold when Cursor routes join local and paid pools."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, "state.json")
        self.store = R.StateStore(self.state_path)
        for var in ("AI_ROTATE", "AI_ROTATE_PIN", "AI_ROTATE_CATALOG",
                    "AI_ROTATE_STATE", "AITIME_STATE_DIR"):
            os.environ.pop(var, None)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_route(self, rid: str, pool: str, tier: str = R.FRONTIER,
                    cost: str = R.SUBSCRIPTION) -> R.Route:
        return R.Route(
            id=rid, backend=rid.split("/")[0],
            backend_label=rid.split("/")[0],
            model=rid.split("/", 1)[-1],
            wire_model=rid.split("/", 1)[-1],
            api="openai",
            base_url="https://example.invalid/v1",
            pool=pool, cost_class=cost, tier=tier, enabled=True,
        )

    def _catalog(self, *routes: R.Route) -> R.Catalog:
        return R.Catalog(routes=list(routes),
                         generated_at="2026-08-18T00:00:00+00:00",
                         age_seconds=0.0, path="<test>")

    def test_cursor_pool_participates_in_pool_rotation(self):
        cat = self._catalog(
            self._make_route("ollama/qwen", "local:ollama",
                             cost=R.LOCAL_UNLIMITED),
            self._make_route("openai/gpt-4o", "openai:paid",
                             cost=R.PAID_METERED),
            self._make_route("cursor/claude-sonnet", "cursor:subscription",
                             cost=R.SUBSCRIPTION),
        )
        rot = R.Rotator(catalog=cat, store=self.store, app="test")
        # Three distinct pools — each should be picked once in 3 picks.
        pools = [rot.next_route(allow_paid=True).pool for _ in range(3)]
        self.assertEqual(sorted(pools),
                         ["cursor:subscription", "local:ollama", "openai:paid"])

    def test_multiple_cursor_models_same_pool_count_once(self):
        """Two cursor models in the same pool should not double the pool's turns."""
        cat = self._catalog(
            self._make_route("cursor/claude-sonnet", "cursor:subscription"),
            self._make_route("cursor/gpt-4o", "cursor:subscription"),
            self._make_route("ollama/qwen", "local:ollama",
                             cost=R.LOCAL_UNLIMITED),
        )
        rot = R.Rotator(catalog=cat, store=self.store, app="test")
        pool_seq = [rot.next_route().pool for _ in range(4)]
        # With 2 pools (cursor, ollama), the sequence should alternate — each
        # pool serves exactly 2 of the 4 calls.
        cursor_count = pool_seq.count("cursor:subscription")
        ollama_count = pool_seq.count("local:ollama")
        self.assertEqual(cursor_count + ollama_count, 4)
        # Neither pool should dominate unfairly (within 1 of each other at 4 picks).
        self.assertLessEqual(abs(cursor_count - ollama_count), 1)

    def test_cursor_pool_cooled_skips_to_next_pool(self):
        """When cursor is cooling down, the rotator serves the other pool."""
        cat = self._catalog(
            self._make_route("cursor/claude-sonnet", "cursor:subscription"),
            self._make_route("ollama/qwen", "local:ollama",
                             cost=R.LOCAL_UNLIMITED),
        )
        rot = R.Rotator(catalog=cat, store=self.store, app="test")
        # Force a rate-limit on the cursor route.
        cursor_route = cat.routes[0]
        rot.report(cursor_route, "rate_limited", 3600.0)
        # Next pick must be from the ollama pool.
        selection = rot.next_route()
        self.assertEqual(selection.pool, "local:ollama")


# --------------------------------------------------------------------------- #
# Discovery sources (offline, no filesystem side-effects)
# --------------------------------------------------------------------------- #

class AitimeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        os.environ.pop("FLEXFACTOR_AITIME_CONFIG", None)
        self._tmp.cleanup()

    def _write_config(self, data) -> str:
        path = os.path.join(self._tmp.name, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.environ["FLEXFACTOR_AITIME_CONFIG"] = path
        return path

    def test_list_of_provider_entries_parsed(self):
        entries = [
            {"name": "openai-main", "model": "gpt-4o",
             "api_type": "openai", "cost_class": "paid",
             "tier": "frontier", "pool": "openai:paid"},
        ]
        self._write_config(entries)
        routes = D.discover_from_aitime()
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["model"], "gpt-4o")
        self.assertEqual(routes[0]["cost_class"], "paid-metered")

    def test_providers_key_in_object_form(self):
        config = {"providers": [
            {"name": "ollama-local", "model": "qwen3-coder:30b",
             "api_type": "ollama", "cost": "local", "tier": "strong"},
        ]}
        self._write_config(config)
        routes = D.discover_from_aitime()
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["cost_class"], "local-unlimited")

    def test_api_key_fields_are_never_copied(self):
        entries = [
            {"name": "secret-provider", "model": "gpt-4",
             "api_type": "openai", "api_key": "sk-secret1234567890abcdef"},
        ]
        self._write_config(entries)
        routes = D.discover_from_aitime()
        # The route must not contain any secret-looking string.
        blob = json.dumps(routes)
        self.assertNotIn("sk-secret", blob)

    def test_missing_config_returns_empty(self):
        os.environ.pop("FLEXFACTOR_AITIME_CONFIG", None)
        routes = D.discover_from_aitime()
        self.assertEqual(routes, [])

    def test_malformed_json_returns_empty(self):
        path = os.path.join(self._tmp.name, "config.json")
        with open(path, "w") as fh:
            fh.write("{not valid json")
        os.environ["FLEXFACTOR_AITIME_CONFIG"] = path
        routes = D.discover_from_aitime()
        self.assertEqual(routes, [])


class CursorDiscoveryTests(unittest.TestCase):
    def setUp(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        os.environ.pop("FLEXFACTOR_CURSOR_PROBE", None)
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        os.environ.pop("FLEXFACTOR_CURSOR_PROBE", None)
        self._tmp.cleanup()

    def test_models_parsed_from_settings_file(self):
        cursor_dir = os.path.join(self._tmp.name, ".cursor")
        os.makedirs(cursor_dir)
        settings = {"model": "claude-3-5-sonnet", "models": ["gpt-4o"]}
        with open(os.path.join(cursor_dir, "settings.json"), "w") as fh:
            json.dump(settings, fh)

        # Patch the config dirs to point at our tmp.
        orig = D._cursor_config_dirs
        D._cursor_config_dirs = lambda: [cursor_dir]
        try:
            routes = D.discover_from_cursor()
        finally:
            D._cursor_config_dirs = orig

        model_ids = [r["model"] for r in routes]
        self.assertIn("claude-3-5-sonnet", model_ids)
        self.assertIn("gpt-4o", model_ids)

    def test_cursor_routes_have_subscription_cost_class(self):
        cursor_dir = os.path.join(self._tmp.name, ".cursor")
        os.makedirs(cursor_dir)
        with open(os.path.join(cursor_dir, "settings.json"), "w") as fh:
            json.dump({"model": "gpt-4o"}, fh)

        orig = D._cursor_config_dirs
        D._cursor_config_dirs = lambda: [cursor_dir]
        try:
            routes = D.discover_from_cursor()
        finally:
            D._cursor_config_dirs = orig

        for r in routes:
            self.assertEqual(r["cost_class"], "subscription")

    def test_daemon_probe_skipped_without_flag(self):
        """The subprocess must never be invoked unless FLEXFACTOR_CURSOR_PROBE=1."""
        called = []
        import subprocess as sp
        orig = sp.run
        sp.run = lambda *a, **kw: called.append(True) or orig(*a, **kw)
        try:
            D._cursor_models_from_daemon()
        except Exception:
            pass
        finally:
            sp.run = orig
        self.assertEqual(called, [], "subprocess.run called without CURSOR_PROBE flag")


class WriteAutoCatalogTests(unittest.TestCase):
    def setUp(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path_fn = D._auto_catalog_path
        auto_path = os.path.join(self._tmp.name, "catalog.auto.json")
        D._auto_catalog_path = lambda: auto_path

    def tearDown(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        D._auto_catalog_path = self._orig_path_fn
        self._tmp.cleanup()

    def test_writes_valid_catalog(self):
        routes = [
            D._route_entry("cursor/gpt-4o", "cursor", "gpt-4o",
                           "cursor:subscription", "subscription", "frontier"),
        ]
        out = D.write_auto_catalog(routes=routes)
        self.assertIsNotNone(out)
        with open(out, encoding="utf-8") as fh:
            blob = json.load(fh)
        self.assertEqual(blob["schema"], 1)
        self.assertEqual(len(blob["routes"]), 1)

    def test_load_auto_catalog_round_trips(self):
        routes = [
            D._route_entry("cursor/claude-sonnet", "cursor", "claude-sonnet",
                           "cursor:subscription", "subscription", "frontier"),
        ]
        D.write_auto_catalog(routes=routes)
        loaded = D.load_auto_catalog()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["model"], "claude-sonnet")


if __name__ == "__main__":
    unittest.main()
