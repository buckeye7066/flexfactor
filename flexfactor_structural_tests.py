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

    def test_rename_destination_can_be_rewritten_after_the_move(self):
        author = _Author([
            plan(changed=False, need_files=["other.py"]),
            plan(
                writes=[{"path": "renamed.py", "contents": "y = 3\n"}],
                renames=[{"from": "other.py", "to": "renamed.py"}],
            ),
        ])
        applied, _, notes = self.run_fix(author)
        self.assertEqual(applied, ["bad.py"])
        self.assertEqual(author.structural_calls, 2)
        self.assertIsNone(self.read("other.py"))
        self.assertEqual(self.read("renamed.py"), "y = 3\n")
        self.assertTrue(any("rewrite renamed.py" in note for note in notes))

    def test_rename_preserves_bytes_beyond_the_review_read_ceiling(self):
        source = os.path.join(self.proj, "large.toml")
        payload = ("# " + ("x" * (F.MAX_REVIEW_BYTES + 32)) + "\n").encode()
        with open(source, "wb") as stream:
            stream.write(payload)
        author = _Author([plan(
            renames=[{"from": "large.toml", "to": "moved.toml"}],
        )])
        self.run_fix(author)
        self.assertFalse(os.path.exists(source))
        with open(os.path.join(self.proj, "moved.toml"), "rb") as stream:
            self.assertEqual(payload, stream.read())

    def test_same_extension_document_rename_needs_no_source_parser(self):
        os.makedirs(os.path.join(self.proj, "docs"))
        source = os.path.join(self.proj, "docs", "old.md")
        with open(source, "w", encoding="utf-8") as stream:
            stream.write("# Existing owner documentation\n")
        author = _Author([plan(
            renames=[{"from": "docs/old.md", "to": "docs/new.md"}],
        )])
        kind, detail = F.attempt_structural_fix(
            author, None, self.proj, "bad.py", [dict(FINDING)],
            {}, True, NOFIX_NOTE,
        )
        self.assertEqual("fixed", kind, detail)
        self.assertFalse(os.path.exists(source))
        self.assertEqual(
            "# Existing owner documentation\n", self.read("docs/new.md")
        )

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
    def _assert_malformed_operation_field_is_preflight_refused(
            self, malformed, expected="not lists"):
        author = _Author([malformed])
        forbidden = mock.Mock(
            side_effect=AssertionError("malformed plan passed preflight")
        )
        with mock.patch.object(F, "_contained_existence", forbidden), \
             mock.patch.object(F, "_read_bytes_contained", forbidden), \
             mock.patch.object(F, "_replace_contained", forbidden), \
             mock.patch.object(F, "_unlink_contained", forbidden), \
             mock.patch.object(F, "_gate_file", forbidden), \
             mock.patch.object(F, "_cross_verify_structural", forbidden):
            kind, detail = F.attempt_structural_fix(
                author, object(), self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn(expected, detail)
        self.assertEqual(self.read("bad.py"), GOOD_PRIMARY)
        self.assertEqual(self.read("other.py"), "y = 2\n")
        forbidden.assert_not_called()

    def test_falsey_non_list_renames_refuse_an_otherwise_valid_write(self):
        self._assert_malformed_operation_field_is_preflight_refused({
            "changed": True,
            "writes": [{"path": "bad.py", "contents": GOOD_FIXED}],
            "renames": False,
            "fixed_titles": [FINDING["title"]],
            "notes": "malformed renames",
        })

    def test_falsey_non_list_writes_refuse_an_otherwise_valid_rename(self):
        self._assert_malformed_operation_field_is_preflight_refused({
            "changed": True,
            "writes": {},
            "renames": [{"from": "other.py", "to": "renamed.py"}],
            "fixed_titles": [FINDING["title"]],
            "notes": "malformed writes",
        })

    def test_non_object_structural_plan_is_refused_without_raising(self):
        author = _Author([False])
        kind, detail = F.attempt_structural_fix(
            author, None, self.proj, "bad.py", [dict(FINDING)],
            {}, True, NOFIX_NOTE,
        )
        self.assertEqual("failed", kind)
        self.assertIn("not an object", detail)
        self.assertEqual(self.read("bad.py"), GOOD_PRIMARY)

    def test_non_boolean_changed_is_refused_before_valid_write(self):
        malformed = {
            "changed": "true",
            "writes": [{"path": "bad.py", "contents": GOOD_FIXED}],
            "renames": [],
            "fixed_titles": [FINDING["title"]],
            "notes": "malformed changed",
        }
        self._assert_malformed_operation_field_is_preflight_refused(
            malformed, expected="changed"
        )

    def test_falsey_non_list_need_files_is_refused_before_second_round(self):
        malformed = {
            "changed": False,
            "need_files": {},
            "writes": [],
            "renames": [],
            "fixed_titles": [],
            "notes": "malformed need_files",
        }
        author = _Author([malformed])
        kind, detail = F.attempt_structural_fix(
            author, None, self.proj, "bad.py", [dict(FINDING)],
            {}, True, NOFIX_NOTE,
        )
        self.assertEqual("failed", kind)
        self.assertIn("need_files", detail)
        self.assertEqual(1, author.structural_calls)
        self.assertEqual(self.read("bad.py"), GOOD_PRIMARY)

    def test_malformed_fixed_titles_is_refused_before_every_operation(self):
        for malformed_titles in (1, [1]):
            with self.subTest(fixed_titles=malformed_titles):
                malformed = {
                    "changed": True,
                    "writes": [{"path": "bad.py", "contents": GOOD_FIXED}],
                    "renames": [],
                    "fixed_titles": malformed_titles,
                    "notes": "malformed fixed titles",
                }
                self._assert_malformed_operation_field_is_preflight_refused(
                    malformed, expected="fixed_titles"
                )

    def test_rename_destination_rewrite_requires_source_contents(self):
        author = _Author([plan(
            writes=[{"path": "renamed.py", "contents": "y = 3\n"}],
            renames=[{"from": "other.py", "to": "renamed.py"}],
        )])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("unseen owner reached a write")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write):
            kind, detail = F.attempt_structural_fix(
                author, None, self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("after moving other.py", detail)
        self.assertIn("without having seen", detail)
        self.assertEqual("y = 2\n", self.read("other.py"))
        self.assertIsNone(self.read("renamed.py"))
        forbidden_write.assert_not_called()

    def test_empty_rename_destination_rewrite_cannot_erase_moved_owner(self):
        author = _Author([
            plan(changed=False, need_files=["other.py"]),
            plan(
                writes=[{"path": "renamed.py", "contents": ""}],
                renames=[{"from": "other.py", "to": "renamed.py"}],
            ),
        ])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("empty rename rewrite reached a write")
        )
        forbidden_unlink = mock.Mock(
            side_effect=AssertionError("empty rename rewrite reached an unlink")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_unlink_contained", forbidden_unlink):
            kind, detail = F.attempt_structural_fix(
                author, None, self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("source rejected before write", detail)
        self.assertIn("empty whole-file response", detail)
        self.assertEqual("y = 2\n", self.read("other.py"))
        self.assertIsNone(self.read("renamed.py"))
        forbidden_write.assert_not_called()
        forbidden_unlink.assert_not_called()

    def test_oversized_rename_is_refused_before_every_mutation(self):
        source = os.path.join(self.proj, "large.toml")
        with open(source, "wb") as stream:
            stream.write(b"value = 1\n" * 8)
        author = _Author([plan(
            renames=[{"from": "large.toml", "to": "moved.toml"}],
        )])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("oversized rename reached a write")
        )
        forbidden_unlink = mock.Mock(
            side_effect=AssertionError("oversized rename reached an unlink")
        )
        with mock.patch.object(F, "STRUCTURAL_RENAME_MAX_BYTES", 32), \
             mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_unlink_contained", forbidden_unlink):
            kind, detail = F.attempt_structural_fix(
                author, None, self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("exceeds 32 bytes", detail)
        self.assertTrue(os.path.exists(source))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "moved.toml")))
        forbidden_write.assert_not_called()
        forbidden_unlink.assert_not_called()

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

    def test_unparsed_rename_destination_is_rejected_before_every_write(self):
        notes = os.path.join(self.proj, "notes.txt")
        with open(notes, "w", encoding="utf-8") as stream:
            stream.write("This is documentation, not TSX source.\n")
        author = _Author([plan(
            renames=[{"from": "notes.txt", "to": "src/new.tsx"}],
        )])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("unparsed rename reached a write")
        )
        forbidden_unlink = mock.Mock(
            side_effect=AssertionError("unparsed rename reached an unlink")
        )
        forbidden_gate = mock.Mock(
            side_effect=AssertionError("unparsed rename reached a gate")
        )
        forbidden_review = mock.Mock(
            side_effect=AssertionError("unparsed rename reached review")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_unlink_contained", forbidden_unlink), \
             mock.patch.object(F, "_gate_file", forbidden_gate), \
             mock.patch.object(F, "_cross_verify_structural", forbidden_review):
            kind, detail = F.attempt_structural_fix(
                author, object(), self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("rename source rejected before write", detail)
        self.assertIn("no safe in-process parser", detail)
        self.assertEqual(self.read("notes.txt"),
                         "This is documentation, not TSX source.\n")
        self.assertIsNone(self.read("src/new.tsx"))
        forbidden_write.assert_not_called()
        forbidden_unlink.assert_not_called()
        forbidden_gate.assert_not_called()
        forbidden_review.assert_not_called()

    def test_non_utf8_rename_is_rejected_using_the_exact_source_bytes(self):
        notes = os.path.join(self.proj, "notes.bin")
        original = b"# invalid byte in a comment: \xff\nvalue: int\n"
        with open(notes, "wb") as stream:
            stream.write(original)
        author = _Author([plan(
            renames=[{"from": "notes.bin", "to": "types/new.pyi"}],
        )])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("non-UTF-8 rename reached a write")
        )
        forbidden_unlink = mock.Mock(
            side_effect=AssertionError("non-UTF-8 rename reached an unlink")
        )
        forbidden_gate = mock.Mock(
            side_effect=AssertionError("non-UTF-8 rename reached a gate")
        )
        forbidden_review = mock.Mock(
            side_effect=AssertionError("non-UTF-8 rename reached review")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_unlink_contained", forbidden_unlink), \
             mock.patch.object(F, "_gate_file", forbidden_gate), \
             mock.patch.object(F, "_cross_verify_structural", forbidden_review):
            kind, detail = F.attempt_structural_fix(
                author, object(), self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("rename source rejected before write", detail)
        self.assertIn("invalid UTF-8", detail)
        with open(notes, "rb") as stream:
            self.assertEqual(original, stream.read())
        self.assertFalse(os.path.exists(os.path.join(self.proj, "types", "new.pyi")))
        forbidden_write.assert_not_called()
        forbidden_unlink.assert_not_called()
        forbidden_gate.assert_not_called()
        forbidden_review.assert_not_called()

    def test_portable_aliases_are_rejected_before_every_structural_write(self):
        author = _Author([plan(writes=[
            {"path": "pkg/caf\u00e9.py", "contents": "VALUE = 1\n"},
            {"path": "pkg/cafe\u0301.py", "contents": "VALUE = 2\n"},
        ])])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("structural alias reached a write")
        )
        forbidden_gate = mock.Mock(
            side_effect=AssertionError("structural alias reached a gate")
        )
        forbidden_review = mock.Mock(
            side_effect=AssertionError("structural alias reached review")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_gate_file", forbidden_gate), \
             mock.patch.object(F, "_cross_verify_structural", forbidden_review):
            kind, detail = F.attempt_structural_fix(
                author, object(), self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("aliases one repository path", detail)
        self.assertFalse(os.path.exists(os.path.join(self.proj, "pkg")))
        forbidden_write.assert_not_called()
        forbidden_gate.assert_not_called()
        forbidden_review.assert_not_called()

    def test_rename_write_exemption_rejects_portable_alias_spelling(self):
        author = _Author([plan(
            writes=[{"path": "RENAMED.py", "contents": "y = 3\n"}],
            renames=[{"from": "other.py", "to": "renamed.py"}],
        )])
        forbidden_write = mock.Mock(
            side_effect=AssertionError("rename alias reached a write")
        )
        forbidden_unlink = mock.Mock(
            side_effect=AssertionError("rename alias reached an unlink")
        )
        with mock.patch.object(F, "_replace_contained", forbidden_write), \
             mock.patch.object(F, "_unlink_contained", forbidden_unlink):
            kind, detail = F.attempt_structural_fix(
                author, None, self.proj, "bad.py", [dict(FINDING)],
                {}, True, NOFIX_NOTE,
            )
        self.assertEqual("failed", kind)
        self.assertIn("aliases one repository path", detail)
        self.assertEqual(self.read("other.py"), "y = 2\n")
        self.assertIsNone(self.read("renamed.py"))
        self.assertIsNone(self.read("RENAMED.py"))
        forbidden_write.assert_not_called()
        forbidden_unlink.assert_not_called()

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
