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


if __name__ == "__main__":
    unittest.main(verbosity=2)
