"""STRUCTURAL (cross-file) fixes - owner order 2026-08-23:
"It sure would be nice if flexfactor would fix errors it found."

A '[no-op: no fix found]' defect gets ONE bounded cross-file escalation:
new files, rewrites of files the model was shown, renames - applied
transactionally, syntax-gated, fully rolled back on any failure.

Runs offline: fake author provider, throwaway project dirs, no network.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import unittest
from unittest import mock

import flexfactor as F

NOFIX_NOTE = ("THE DEFECT IS REAL but cannot be fixed in this file alone "
              "(needs changes outside this file)")

FINDING = {"severity": "high", "line": 1, "title": "Deterministic resume",
           "problem": "state is lost across restarts", "fix": "persist a ledger",
           "category": "correctness", "source_excerpt": "x = 1",
           "trigger": "restart mid-job", "observable_failure": "job restarts from zero"}

GOOD_PRIMARY = "x = 1\n"
GOOD_FIXED = "x = 1\nRESUME = True\n"
BAD_PYTHON = "def broken(:\n"


class _Author:
    """Author double: in-file fix always no-ops with a no-fix reason; the
    structural call replays the scripted plans in order."""

    def __init__(self, plans):
        self.plans = list(plans)
        self.structural_calls = 0
        self.model = "fake-author"

    def structured(self, system, prompt, schema, max_tokens=8000, **kw):
        if schema is F.STRUCTURAL_FIX_SCHEMA:
            self.structural_calls += 1
            if not self.plans:
                raise AssertionError("structural called with no scripted plan")
            plan = self.plans.pop(0)
            return plan(prompt) if callable(plan) else plan
        # Both the edits and whole-file in-place paths must decline.
        if schema is F.FIX_EDITS_SCHEMA:
            return {"changed": False, "edits": [], "fixed_titles": [],
                    "notes": NOFIX_NOTE}
        return {"changed": False, "contents": "", "fixed_titles": [],
                "notes": NOFIX_NOTE}


def plan(writes=None, renames=None, changed=True, need_files=None, notes="plan"):
    p = {"changed": changed, "writes": writes or [], "renames": renames or [],
         "fixed_titles": [FINDING["title"]], "notes": notes}
    if need_files is not None:
        p["need_files"] = need_files
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="ff-structural-")
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)
        with open(os.path.join(self.proj, "bad.py"), "w", encoding="utf-8") as f:
            f.write(GOOD_PRIMARY)
        with open(os.path.join(self.proj, "other.py"), "w", encoding="utf-8") as f:
            f.write("y = 2\n")
        self.args = argparse.Namespace(fix_severity="high", whole_file_fixes=False,
                                       fix_prefetch=0, structural_fixes=True)

    def run_fix(self, author):
        return F._fix_files(author, None, self.proj, {"bad.py": [dict(FINDING)]},
                            {}, True, self.args)

    def read(self, rel):
        p = os.path.join(self.proj, rel)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()


class StructuralApplies(_Base):
    def test_create_plus_rewrite_lands_and_counts_as_fixed(self):
        author = _Author([plan(writes=[
            {"path": "bad.py", "contents": GOOD_FIXED},
            {"path": "pkg/__init__.py", "contents": ""},
        ])])
        applied, unverified, notes = self.run_fix(author)
        self.assertEqual(applied, ["bad.py"])
        self.assertEqual(unverified, [])          # .py files gate via py_compile
        self.assertEqual(self.read("bad.py"), GOOD_FIXED)
        self.assertEqual(self.read("pkg/__init__.py"), "")
        self.assertTrue(any("STRUCTURAL fix applied" in n for n in notes))

    def test_rename_lands(self):
        author = _Author([plan(
            writes=[{"path": "bad.py", "contents": GOOD_FIXED}],
            renames=[{"from": "other.py", "to": "renamed.py"}])])
        applied, _, _ = self.run_fix(author)
        self.assertEqual(applied, ["bad.py"])
        self.assertIsNone(self.read("other.py"))
        self.assertEqual(self.read("renamed.py"), "y = 2\n")

    def test_need_files_round_grants_rewrite_of_requested_file(self):
        author = _Author([
            plan(changed=False, need_files=["other.py"]),
            plan(writes=[{"path": "other.py", "contents": "y = 3\n"}]),
        ])
        applied, _, _ = self.run_fix(author)
        self.assertEqual(applied, ["bad.py"])
        self.assertEqual(author.structural_calls, 2)
        self.assertEqual(self.read("other.py"), "y = 3\n")


class StructuralRollsBack(_Base):
    def test_unparsed_structural_source_is_rejected_before_every_write(self):
        author = _Author([plan(writes=[
            {"path": "bad.py", "contents": GOOD_FIXED},
            {"path": "ui.tsx", "contents": "This is not TSX source.\n"},
        ])])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("unparsed structural source reached a write")
        )
        forbidden_gate = mock.Mock(
            side_effect=AssertionError("unparsed structural source reached a gate")
        )
        forbidden_review = mock.Mock(
            side_effect=AssertionError("unparsed structural source reached review")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_gate_file", forbidden_gate), \
             mock.patch.object(F, "_cross_verify_structural", forbidden_review):
            kind, detail = F.attempt_structural_fix(
                author, object(), self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("rejected before write", detail)
        self.assertIn("no safe in-process parser", detail)
        self.assertEqual(self.read("bad.py"), GOOD_PRIMARY)
        self.assertIsNone(self.read("ui.tsx"))
        forbidden_write.assert_not_called()
        forbidden_gate.assert_not_called()
        forbidden_review.assert_not_called()

    def test_broken_python_rolls_back_every_operation(self):
        author = _Author([plan(writes=[
            {"path": "bad.py", "contents": BAD_PYTHON},
            {"path": "junk.py", "contents": "z = 1\n"},
        ])])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, [])
        self.assertEqual(self.read("bad.py"), GOOD_PRIMARY)   # restored
        self.assertIsNone(self.read("junk.py"))               # created file removed
        self.assertTrue(any("structural attempt failed" in n for n in notes))
        self.assertTrue(any("NO FIX FOUND" in n for n in notes))  # still honest

    def test_rewriting_an_unseen_file_is_refused(self):
        author = _Author([plan(writes=[
            {"path": "other.py", "contents": "y = 9\n"}])])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, [])
        self.assertEqual(self.read("other.py"), "y = 2\n")    # untouched
        self.assertTrue(any("without having seen" in n for n in notes))

    def test_path_escape_is_refused(self):
        author = _Author([plan(writes=[
            {"path": "../evil.py", "contents": "boom = 1\n"}])])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, [])
        parent = os.path.dirname(self.proj)
        self.assertFalse(os.path.exists(os.path.join(parent, "evil.py")))
        self.assertTrue(any("structural attempt failed" in n for n in notes))


class StructuralGating(_Base):
    def test_declined_plan_keeps_the_honest_noop_accounting(self):
        author = _Author([plan(changed=False, notes="no safe cross-file fix")])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, [])
        self.assertTrue(any("structural attempt declined" in n for n in notes))
        self.assertTrue(any("NO FIX FOUND" in n for n in notes))

    def test_flag_off_never_calls_the_planner(self):
        self.args.structural_fixes = False
        author = _Author([])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, [])
        self.assertEqual(author.structural_calls, 0)
        self.assertTrue(any("NO FIX FOUND" in n for n in notes))

    def test_rejected_noop_is_not_escalated(self):
        class _Rejecting(_Author):
            def structured(self, system, prompt, schema, max_tokens=8000, **kw):
                if schema is F.STRUCTURAL_FIX_SCHEMA:
                    self.structural_calls += 1
                return {"changed": False, "edits": [], "contents": "",
                        "fixed_titles": [],
                        "notes": "THE FINDING IS WRONG for this file - already correct"}
        author = _Rejecting([])
        applied, _, _ = self.run_fix(author)
        self.assertEqual(applied, [])
        self.assertEqual(author.structural_calls, 0)

    def test_planner_crash_never_kills_the_audit(self):
        def boom(_prompt):
            raise RuntimeError("planner exploded")
        author = _Author([boom])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, [])
        self.assertEqual(self.read("bad.py"), GOOD_PRIMARY)
        self.assertTrue(any("structural attempt failed" in n for n in notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
