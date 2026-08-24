#!/usr/bin/env python3
"""A BLOCKED or RED baseline must reach the run's error ledger.

Measured live 2026-08-24 (10-program audit, pid 21424): 8 of 8 real programs
had the containment gate refuse their entire build+test baseline, each wrote
`<run dir>/baseline-publication-failure.log` to disk - and
`grep -c flexfactor-containment errors.md` returned 0 for ALL TEN programs.
The single most consequential failure of the run was the one failure the
ledger (and therefore the dashboard's per-program error box) never mentioned.

Two defects, tested separately here:

1. `classify()`'s containment row matched `containment_blocked` (the ATTRIBUTE
   name, `cp.flexfactor_containment_blocked`) and `rc 126`, but the message the
   gate actually emits is `[flexfactor-containment] REFUSED: ...`. Neither
   matched, so even a recorded refusal would have been filed `unknown`.
2. Nothing called `_ledger` at the baseline-failure site at all.

Defect 2 is this repo's recurring "written but never wired" trap (four prior
instances: flexfactor_runstate, the set_phase/record_cycle/record_spend group,
_UI_EXPLORER_JS, and gather_purpose_evidence's injected runners). A source grep
is explicitly NOT accepted as a guard here - CLAUDE.md: "a check that cannot
fail proves nothing". So the wiring test walks the real AST and proves the
`_ledger` call is inside the same `if not repair.get("ok"):` block that writes
the log file, and the behaviour test drives a REAL ErrorLedger end to end.
"""
from __future__ import annotations

import ast
import os
import shutil
import tempfile
import unittest

import flexfactor_errors as fe


class ContainmentClassificationTests(unittest.TestCase):
    """The message the gate really emits must classify, and only it."""

    REAL = ("[flexfactor-containment] REFUSED: refusing to run third-party "
            "install/build/test for C:/Users/firer/Iplay: no OS sandbox on this "
            "host (missing OS enforcement of: network_isolation) and the "
            "repository is not trusted (no trusted_repos configured).")

    def test_the_real_refusal_message_classifies_as_environment(self):
        kind, suggestion = fe.classify(self.REAL)
        self.assertEqual(kind, fe.KIND_ENV)
        self.assertIn("trusted_repos", suggestion)

    def test_the_suggestion_names_an_actionable_remedy(self):
        _, suggestion = fe.classify(self.REAL)
        self.assertTrue(
            any(t in suggestion for t in ("--trust-repo", "FLEXFACTOR_TRUSTED_REPOS")),
            f"suggestion must name how to authorize, got: {suggestion!r}")

    def test_unrelated_text_is_not_swept_into_this_row(self):
        """Negative control - a row that matches everything classifies nothing."""
        kind, _ = fe.classify("TypeError: unsupported operand type(s) for +")
        self.assertNotEqual(kind, fe.KIND_ENV)


class BaselineReachesTheLedgerTests(unittest.TestCase):
    """Drive a REAL ErrorLedger, then read the artifacts the owner reads."""

    def setUp(self):
        self.run_dir = tempfile.mkdtemp(prefix="ffbaseline-")
        self.led = fe.ErrorLedger(self.run_dir, "Iplay",
                                  os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def _record_blocked(self):
        self.led.record(
            "baseline",
            "baseline publication gate BLOCKED: the project's build/test "
            "commands were refused before they ran",
            kind=fe.KIND_ENV,
            detail=ContainmentClassificationTests.REAL,
            suggestion=('Add its path to ~/.flexfactor/policy.json '
                        '"trusted_repos", set FLEXFACTOR_TRUSTED_REPOS, or pass '
                        "--trust-repo. Until then NOTHING is verified."))

    def test_a_blocked_baseline_lands_in_errors_md(self):
        self._record_blocked()
        with open(os.path.join(self.run_dir, "errors.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("baseline", md)
        self.assertIn("BLOCKED", md)
        self.assertIn("trusted_repos", md)

    def test_it_is_counted_as_an_environment_failure_not_unknown(self):
        self._record_blocked()
        counts = fe.counts_by_kind(fe.load_entries(self.run_dir))
        self.assertEqual(counts.get(fe.KIND_ENV), 1, counts)
        self.assertFalse(counts.get(fe.KIND_UNKNOWN), counts)

    def test_the_live_defect_would_now_be_visible(self):
        """The exact query that returned 0 across ten programs on 2026-08-24."""
        self._record_blocked()
        for name in ("errors.md", "errors.json"):
            with open(os.path.join(self.run_dir, name), encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("flexfactor-containment", body,
                          f"{name} still does not mention the containment gate")


class BaselineLedgerWiringTests(unittest.TestCase):
    """Prove the call SITE exists in the right block - not merely that a
    ledger API exists somewhere (the four-times-repeated trap in this repo)."""

    @staticmethod
    def _baseline_block():
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "flexfactor.py"), encoding="utf-8") as fh:
            src = fh.read()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.If):
                continue
            calls = [c for c in ast.walk(node)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
            names = {c.func.id for c in calls}
            if "_persist_baseline_failure" in names:
                return node, names
        return None, set()

    def test_the_log_writing_block_also_records_to_the_ledger(self):
        node, names = self._baseline_block()
        self.assertIsNotNone(node, "could not find the _persist_baseline_failure block")
        self.assertIn("_ledger", names,
                      "the block that writes baseline-publication-failure.log does "
                      "NOT call _ledger - the log is on disk but the owner's error "
                      "box stays empty, which is the 2026-08-24 defect verbatim")

    def test_the_recorded_phase_is_named_baseline(self):
        node, _ = self._baseline_block()
        phases = [c.args[0].value for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                  and c.func.id == "_ledger" and c.args
                  and isinstance(c.args[0], ast.Constant)]
        self.assertIn("baseline", phases, phases)

    def test_it_distinguishes_a_blocked_gate_from_a_genuinely_red_suite(self):
        """A refused gate is an ENVIRONMENT problem the owner can fix in one
        line; a red suite is the PROGRAM's. Filing both the same way would tell
        the owner to debug tests that never ran."""
        node, _ = self._baseline_block()
        src = ast.unparse(node)
        self.assertIn("flexfactor-containment", src)
        self.assertIn("KIND_ENV", src)
        self.assertIn("KIND_PROGRAM", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
