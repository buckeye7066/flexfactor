"""The per-run error ledger: errors, responsible code, suggested fix.

Runs offline. No network, no model calls, no tokens spent.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import flexfactor_errors as E

HERE = os.path.dirname(os.path.abspath(__file__))


def _boom():
    raise ValueError("unknown model architecture: 'muse-glimmer'")


class Classification(unittest.TestCase):
    def test_known_signature_gives_kind_and_fix(self):
        kind, sugg = E.classify("RuntimeError: unknown model architecture 'muse-glimmer'")
        self.assertEqual(kind, E.KIND_ENV)
        self.assertIn("Upgrade Ollama", sugg)

    def test_program_test_failure_is_the_programs(self):
        kind, sugg = E.classify("FAILED tests/test_x.py::test_a - AssertionError")
        self.assertEqual(kind, E.KIND_PROGRAM)
        self.assertIn("test", sugg.lower())

    def test_unknown_is_honest(self):
        kind, sugg = E.classify("SomethingNobodyHasSeen: zzz")
        self.assertEqual(kind, E.KIND_UNKNOWN)
        self.assertIn("no known fix", sugg)


class ResponsibleFrame(unittest.TestCase):
    def test_innermost_flexfactor_frame_with_source(self):
        try:
            _boom()
        except ValueError as exc:
            where = E.responsible_frame(exc, HERE)
        self.assertIsNotNone(where)
        self.assertEqual(where["file"], "flexfactor_errors_tests.py")
        self.assertEqual(where["function"], "_boom")
        self.assertIn("raise ValueError", where["source"])

    def test_no_flexfactor_frame_means_none(self):
        try:
            json.loads("{bad")
        except ValueError as exc:
            # The stack passes through this test file (flexfactor_*), so use a
            # root that cannot match.
            self.assertIsNone(E.responsible_frame(exc, os.path.join(HERE, "nowhere")))


class LedgerWritesEveryRecord(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ff-errors-")
        self.ledger = E.ErrorLedger(self.dir, "prog", HERE)

    def test_record_writes_json_and_markdown_immediately(self):
        try:
            _boom()
        except ValueError as exc:
            e = self.ledger.record("phase-x", exc, program_file="src/a.py")
        self.assertTrue(os.path.exists(self.ledger.json_path))
        self.assertTrue(os.path.exists(self.ledger.md_path))
        data = json.load(open(self.ledger.json_path, encoding="utf-8"))
        self.assertEqual(data["entries"][0]["n"], 1)
        self.assertEqual(e["kind"], E.KIND_ENV)
        self.assertEqual(e["responsible"]["function"], "_boom")
        md = open(self.ledger.md_path, encoding="utf-8").read()
        self.assertIn("unknown model architecture", md)
        self.assertIn("Suggested fix", md)
        self.assertIn("Upgrade Ollama", md)
        self.assertIn("`flexfactor_errors_tests.py:", md)

    def test_string_errors_and_routes_are_attributed(self):
        e = self.ledger.record("rotation", "HTTPError: 529 overloaded", route="nvidia_nim/x")
        self.assertEqual(e["kind"], E.KIND_PROVIDER)
        self.assertIsNone(e["responsible"])
        self.assertIn("nvidia_nim/x", self.ledger.render_markdown())

    def test_model_suggester_is_used_only_for_unknown_and_labelled(self):
        calls = []
        led = E.ErrorLedger(self.dir, "prog", HERE,
                            suggester=lambda text, where: calls.append(text) or "try X")
        known = led.record("p", "RuntimeError: unknown model architecture")
        unknown = led.record("p", "Weird: never seen")
        self.assertEqual(len(calls), 1)
        self.assertEqual(known["suggestion_source"], "signature")
        self.assertEqual(unknown["suggestion_source"], "model")
        self.assertTrue(unknown["suggestion"].startswith("model suggestion, unverified"))

    def test_suggester_failure_never_raises(self):
        def bad(text, where): raise RuntimeError("nope")
        led = E.ErrorLedger(self.dir, "prog", HERE, suggester=bad)
        e = led.record("p", "Weird: never seen")
        self.assertIn("no known fix", e["suggestion"])

    def test_empty_ledger_renders_none(self):
        self.assertIn("None recorded", self.ledger.render_markdown())
        self.assertEqual(self.ledger.summary_line(), "[errors] none recorded")


class RouteFailuresAreTheProviders(unittest.TestCase):
    """Live 2026-08-23: a gated 403 and a 'not a chat model' 404 were filed as
    flexfactor-defect because our HTTP client frame was on the stack."""

    def setUp(self):
        self.led = E.ErrorLedger(tempfile.mkdtemp(prefix="ff-errors-"), "prog", HERE)

    def test_402_is_budget_with_the_allowance_fix(self):
        e = self.led.record("rotation", "APIStatusError: Error code: 402 - requires more credits", route="openrouter/x:free")
        self.assertEqual(e["kind"], E.KIND_BUDGET); self.assertIn("allowance", e["suggestion"])

    def test_403_is_the_providers_not_ours(self):
        try:
            _boom()          # puts a FlexFactor frame on the stack
        except ValueError:
            pass
        e = self.led.record("rotation", "PermissionDeniedError: Error code: 403 - gated", route="openrouter/inkling")
        self.assertEqual(e["kind"], E.KIND_PROVIDER)

    def test_not_a_chat_model_names_the_unfit_list(self):
        e = self.led.record("rotation", "NotFoundError: 404 - This is not a chat model", route="openai_api/gpt-realtime")
        self.assertEqual(e["kind"], E.KIND_PROVIDER); self.assertIn("unfit list", e["suggestion"])

    def test_unknown_route_failure_defaults_to_provider_even_with_our_frame(self):
        try:
            _boom()
        except ValueError as exc:
            e = self.led.record("rotation", "Weird: never seen", route="r/x")
        self.assertEqual(e["kind"], E.KIND_PROVIDER)

    # -- 2026-08-24: 55 CLI failures filed "provider / no known fix" ---------
    #
    # local-ai-factory-20260824-005448-500119-21424 recorded 30x cli/codex and
    # 23x cli/claude-code, every one of them kind=provider with the fallback
    # suggestion. Neither is the provider's doing and neither is unknown: one
    # is a codex CLI configured for a model this ChatGPT account cannot use,
    # the other a CLI that had to be killed at its 600-second deadline. That
    # run reviewed 2 of 287 files.

    def test_a_codex_account_refusal_is_the_environments_and_names_the_fix(self):
        e = self.led.record(
            "rotation",
            "CliUnavailable: C:\\Users\\firer\\AppData\\Roaming\\npm\\codex.CMD: "
            'exited 1: ERROR: {"status":400,"error":{"message":"The '
            "'gpt-5.6-sol' model is not supported when using Codex with a "
            'ChatGPT account."}}',
            route="cli/codex")
        self.assertEqual(e["kind"], E.KIND_ENV)
        self.assertEqual(e["suggestion_source"], "signature")
        self.assertIn("FLEXFACTOR_ROTATION_EXTENSIONS", e["suggestion"])

    def test_a_killed_cli_is_the_environments_and_names_the_fix(self):
        e = self.led.record(
            "rotation",
            "CliUnavailable: C:\\Users\\firer\\.local\\bin\\claude.EXE: "
            "exceeded 600s and was killed",
            route="cli/claude-code")
        self.assertEqual(e["kind"], E.KIND_ENV)
        self.assertEqual(e["suggestion_source"], "signature")

    def test_an_account_wide_daily_quota_says_so_instead_of_generic_rate_limit(self):
        """574 of 898 entries in one day were this one refusal, re-tried."""
        e = self.led.record(
            "rotation",
            "RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit "
            "exceeded: free-models-per-day. Add 10 credits to unlock 1000 free "
            "model requests per day', 'code': 429, 'metadata': {'headers': "
            "{'X-RateLimit-Reset': '1787616000000'}, 'limit_source': "
            "'openrouter_free_tier_daily'}}}",
            route="openrouter/cohere/north-mini-code:free")
        self.assertEqual(e["suggestion_source"], "signature")
        self.assertIn("daily", e["suggestion"].lower())
        self.assertNotIn("nothing to fix", e["suggestion"])

    def test_a_per_minute_rate_limit_still_gets_the_generic_row(self):
        """Groq's TPM limit must NOT be described as a spent daily allowance."""
        e = self.led.record(
            "rotation",
            "RateLimitError: Error code: 429 - Rate limit reached for model "
            "`llama-4-scout` on tokens per minute (TPM): Limit 30000. Please "
            "try again in 10.132s.",
            route="groq/llama-4-scout")
        self.assertEqual(e["kind"], E.KIND_PROVIDER)
        self.assertNotIn("daily", e["suggestion"].lower())

    def test_model_suggester_is_never_asked_about_route_failures(self):
        calls = []
        led = E.ErrorLedger(self.led.run_dir, "prog", HERE, suggester=lambda t, w: calls.append(t) or "x")
        led.record("rotation", "Weird: never seen", route="r/x")
        led.record("fix", "Weird: never seen")
        self.assertEqual(len(calls), 1)

    def test_suggester_cannot_reenter_the_ledger(self):
        led = E.ErrorLedger(self.led.run_dir, "prog", HERE)
        def recursing(t, w):
            led.record("fix", "Weird: inner")     # would loop without the guard
            return "outer"
        led._suggester = recursing
        e = led.record("fix", "Weird: outer")
        self.assertEqual(len(led.entries), 2)
        self.assertEqual(e["suggestion_source"], "model")
        self.assertEqual(led.entries[0]["suggestion_source"], "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
