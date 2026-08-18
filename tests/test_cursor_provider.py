"""Tests for the Cursor provider adapter.

All tests run fully offline: no network calls, no subprocess invocations,
no real Cursor installation required.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from providers.cursor_provider import (
    CursorProvider,
    CursorUnavailable,
    make_cursor_provider,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fake_route(model: str = "gpt-4o", base_url: str = "") -> MagicMock:
    r = MagicMock()
    r.model = model
    r.wire_model = model
    r.base_url = base_url
    return r


# --------------------------------------------------------------------------- #
# CursorProvider unit tests
# --------------------------------------------------------------------------- #

class CursorProviderFailClosedTests(unittest.TestCase):
    """Without a base_url, every call must raise CursorUnavailable."""

    def setUp(self):
        self.provider = CursorProvider(model="gpt-4o", base_url=None)

    def test_complete_raises_when_no_base_url(self):
        with self.assertRaises(CursorUnavailable):
            self.provider.complete("hello")

    def test_grade_raises_when_no_base_url(self):
        with self.assertRaises(CursorUnavailable):
            self.provider.grade("hello")

    def test_structured_raises_when_no_base_url(self):
        with self.assertRaises(CursorUnavailable):
            self.provider.structured("hello", schema={})

    def test_ping_raises_when_no_base_url(self):
        # ping raises CursorUnavailable when no base URL is configured so the
        # rotator knows to roll over to the next pool.
        with self.assertRaises(CursorUnavailable):
            self.provider.ping()

    def test_meter_label(self):
        self.assertEqual(self.provider.meter, "cursor:subscription")

    def test_judge_model_defaults_to_model(self):
        p = CursorProvider(model="gpt-4o")
        self.assertEqual(p.judge_model, "gpt-4o")

    def test_judge_model_can_be_overridden(self):
        p = CursorProvider(model="gpt-4o", judge_model="cursor-small")
        self.assertEqual(p.judge_model, "cursor-small")


class CursorProviderHttpTests(unittest.TestCase):
    """CursorProvider with a base_url routes calls to _http_post."""

    def setUp(self):
        self.provider = CursorProvider(
            model="gpt-4o",
            base_url="http://127.0.0.1:3000/v1",
        )

    def _patch_http(self, response: dict):
        """Return a context manager that patches _http_post with a fixed response."""
        import providers.cursor_provider as cp
        return patch.object(cp, "_http_post", return_value=response)

    def test_complete_extracts_content(self):
        fake_resp = {
            "choices": [{"message": {"content": "hello world"}}]
        }
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value=fake_resp) as m:
            result = self.provider.complete("say hello")
        self.assertEqual(result, "hello world")
        args, kwargs = m.call_args
        url, payload = args
        self.assertIn("chat/completions", url)
        self.assertEqual(payload["model"], "gpt-4o")

    def test_complete_raises_on_malformed_response(self):
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value={"choices": []}):
            with self.assertRaises(CursorUnavailable):
                self.provider.complete("hello")

    def test_grade_uses_judge_model(self):
        provider = CursorProvider(
            model="gpt-4o", base_url="http://127.0.0.1:3000/v1",
            judge_model="cursor-small",
        )
        fake_resp = {"choices": [{"message": {"content": "A"}}]}
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value=fake_resp) as m:
            provider.grade("classify this")
        _, payload = m.call_args[0]
        self.assertEqual(payload["model"], "cursor-small")

    def test_grade_restores_model_after_call(self):
        provider = CursorProvider(
            model="gpt-4o", base_url="http://127.0.0.1:3000/v1",
            judge_model="cursor-small",
        )
        fake_resp = {"choices": [{"message": {"content": "A"}}]}
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value=fake_resp):
            provider.grade("classify this")
        self.assertEqual(provider.model, "gpt-4o")

    def test_structured_parses_json(self):
        fake_resp = {"choices": [{"message": {"content": '{"answer": 42}'}}]}
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value=fake_resp):
            result = self.provider.structured("question", schema={})
        self.assertEqual(result, {"answer": 42})

    def test_structured_strips_markdown_fence(self):
        fenced = "```json\n{\"answer\": 42}\n```"
        fake_resp = {"choices": [{"message": {"content": fenced}}]}
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value=fake_resp):
            result = self.provider.structured("question", schema={})
        self.assertEqual(result, {"answer": 42})

    def test_structured_raises_on_non_json(self):
        fake_resp = {"choices": [{"message": {"content": "not json at all"}}]}
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_post", return_value=fake_resp):
            with self.assertRaises(CursorUnavailable):
                self.provider.structured("question", schema={})

    def test_ping_true_on_200(self):
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_get", return_value={"status": "ok"}):
            self.assertTrue(self.provider.ping())

    def test_ping_false_on_network_error(self):
        import providers.cursor_provider as cp
        with patch.object(cp, "_http_get", side_effect=CursorUnavailable("down")):
            self.assertFalse(self.provider.ping())


# --------------------------------------------------------------------------- #
# make_cursor_provider factory tests
# --------------------------------------------------------------------------- #

class MakeCursorProviderTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        os.environ.pop("FLEXFACTOR_CURSOR_BASE_URL", None)

    def tearDown(self):
        os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        os.environ.pop("FLEXFACTOR_CURSOR_BASE_URL", None)

    def test_raises_when_extensions_disabled(self):
        with self.assertRaises(CursorUnavailable):
            make_cursor_provider(_fake_route())

    def test_returns_provider_when_extensions_enabled(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        p = make_cursor_provider(_fake_route("claude-3-5-sonnet"))
        self.assertIsInstance(p, CursorProvider)
        self.assertEqual(p.model, "claude-3-5-sonnet")

    def test_uses_cursor_base_url_env(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        os.environ["FLEXFACTOR_CURSOR_BASE_URL"] = "http://localhost:9999/v1"
        p = make_cursor_provider(_fake_route("gpt-4o"))
        self.assertEqual(p._base_url, "http://localhost:9999/v1")

    def test_falls_back_to_route_base_url(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        route = _fake_route("gpt-4o", base_url="http://cursor.local/v1")
        p = make_cursor_provider(route)
        self.assertEqual(p._base_url, "http://cursor.local/v1")

    def test_base_url_none_when_no_source(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        p = make_cursor_provider(_fake_route("gpt-4o", base_url=""))
        self.assertIsNone(p._base_url)

    def test_wire_model_used_over_model(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"
        route = _fake_route()
        route.wire_model = "cursor-small"
        route.model = "gpt-4o"
        p = make_cursor_provider(route)
        self.assertEqual(p.model, "cursor-small")


# --------------------------------------------------------------------------- #
# CursorUnavailable is retryable by the rotator
# --------------------------------------------------------------------------- #

class CursorUnavailableRetryableTests(unittest.TestCase):
    """The rotator's _is_retryable must return True for CursorUnavailable so
    the router can roll over to the next pool."""

    def test_cursor_unavailable_is_runtime_error(self):
        exc = CursorUnavailable("down")
        self.assertIsInstance(exc, RuntimeError)

    def test_rotator_retries_on_cursor_unavailable(self):
        import flexfactor_rotation as R
        exc = CursorUnavailable("connection refused")
        self.assertTrue(R._is_retryable(exc),
                        "rotator should retry CursorUnavailable")


# --------------------------------------------------------------------------- #
# Competitors JSON sanity checks
# --------------------------------------------------------------------------- #

class CompetitorProfilesTests(unittest.TestCase):
    def test_default_profiles_load_and_have_five_entries(self):
        profiles_path = os.path.join(
            _ROOT, "competitors", "default_profiles.json")
        with open(profiles_path, encoding="utf-8") as fh:
            profiles = json.load(fh)
        self.assertIsInstance(profiles, list)
        self.assertGreaterEqual(len(profiles), 5,
                                "must have at least 5 competitor profiles")

    def test_each_profile_has_required_keys(self):
        profiles_path = os.path.join(
            _ROOT, "competitors", "default_profiles.json")
        with open(profiles_path, encoding="utf-8") as fh:
            profiles = json.load(fh)
        required = {"name", "url", "license", "category", "description",
                    "notable_features", "ideas"}
        for p in profiles:
            missing = required - set(p.keys())
            self.assertFalse(
                missing,
                f"Profile {p.get('name', '?')} missing keys: {missing}")

    def test_no_secrets_in_profiles(self):
        profiles_path = os.path.join(
            _ROOT, "competitors", "default_profiles.json")
        with open(profiles_path, encoding="utf-8") as fh:
            text = fh.read()
        secret_markers = ("sk-", "Bearer ", "password", "api_key")
        for marker in secret_markers:
            self.assertNotIn(
                marker, text,
                f"Competitor profiles must not contain secret markers ({marker!r})")


if __name__ == "__main__":
    unittest.main()
