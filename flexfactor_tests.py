"""Unit tests for flexfactor's pure helpers (no API keys, no network).

Run:  python flexfactor_tests.py

Uses the hermetic module-load pattern: register the module in sys.modules
BEFORE exec_module, or @dataclass with future annotations dies at import.
"""
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("flexfactor", os.path.join(_HERE, "flexfactor.py"))
ff = importlib.util.module_from_spec(_SPEC)
sys.modules["flexfactor"] = ff
_SPEC.loader.exec_module(ff)


class ApplyEditsTests(unittest.TestCase):
    TEXT = "line one\nline two\nline three\nline four\n"

    def test_single_unique_edit_applies(self):
        new, err = ff._apply_edits(self.TEXT, [{"search": "line two", "replace": "LINE 2"}])
        self.assertEqual(err, "")
        self.assertEqual(new, "line one\nLINE 2\nline three\nline four\n")

    def test_multiple_sequential_edits(self):
        edits = [
            {"search": "line one", "replace": "first"},
            {"search": "line four\n", "replace": ""},
        ]
        new, err = ff._apply_edits(self.TEXT, edits)
        self.assertEqual(err, "")
        self.assertEqual(new, "first\nline two\nline three\n")

    def test_anchor_not_found_fails_closed(self):
        new, err = ff._apply_edits(self.TEXT, [{"search": "missing", "replace": "x"}])
        self.assertIsNone(new)
        self.assertIn("not found", err)

    def test_ambiguous_anchor_fails_closed(self):
        text = "dup\nother\ndup\n"
        new, err = ff._apply_edits(text, [{"search": "dup", "replace": "x"}])
        self.assertIsNone(new)
        self.assertIn("not unique", err)

    def test_empty_or_missing_edits_fail_closed(self):
        self.assertIsNone(ff._apply_edits(self.TEXT, [])[0])
        self.assertIsNone(ff._apply_edits(self.TEXT, None)[0])
        self.assertIsNone(ff._apply_edits(self.TEXT, [{"replace": "x"}])[0])

    def test_whitespace_exactness_is_required(self):
        new, err = ff._apply_edits("  indented\n", [{"search": "indented", "replace": "changed"}])
        # substring matches inside the indented line — exactly once, so it applies
        self.assertEqual(new, "  changed\n")
        new2, _ = ff._apply_edits("  indented\n", [{"search": "\tindented", "replace": "x"}])
        self.assertIsNone(new2)  # wrong whitespace anchor never silently applies

    def test_deletion_via_empty_replace(self):
        new, err = ff._apply_edits(self.TEXT, [{"search": "line three\n", "replace": ""}])
        self.assertEqual(err, "")
        self.assertNotIn("line three", new)


class FixDiffTests(unittest.TestCase):
    def test_diff_contains_only_changed_hunks(self):
        original = "\n".join(f"row {i}" for i in range(200)) + "\n"
        fixed = original.replace("row 100", "row one hundred")
        diff = ff._fix_diff(original, fixed, "src/x.py")
        self.assertIn("row one hundred", diff)
        self.assertIn("a/src/x.py", diff)
        # The diff must be dramatically smaller than original+fixed (the point).
        self.assertLess(len(diff), len(original) // 4)

    def test_identical_files_produce_empty_diff(self):
        self.assertEqual(ff._fix_diff("same\n", "same\n", "f"), "")


class PricingAndEconomyTests(unittest.TestCase):
    def test_claude5_family_priced(self):
        # Missing entries silently fall back to Opus-tier pricing (5/25), which
        # overbills the meter and stops budget-capped runs early.
        self.assertEqual(ff._price_for("claude-sonnet-5"), (3.0, 15.0))
        self.assertEqual(ff._price_for("claude-fable-5"), (10.0, 50.0))
        self.assertEqual(ff._price_for("claude-haiku-4-5"), (1.0, 5.0))
        self.assertEqual(ff._price_for("claude-opus-4-8"), (5.0, 25.0))

    def test_sonnet5_key_does_not_shadow_sonnet46(self):
        self.assertEqual(ff._price_for("claude-sonnet-4-6"), (3.0, 15.0))

    def test_economy_tier_defined_for_anthropic(self):
        self.assertEqual(ff.ECONOMY_MODELS.get("anthropic"), "claude-sonnet-5")

    def test_economy_routes_author_model(self):
        # Exercise build_audit_providers itself (stubbed provider + key check):
        # --economy picks the economy author when no explicit --model was given;
        # an explicit --model always wins.
        class Args:
            provider = "anthropic"
            model = None
            economy = True
            use_both = False
            secondary_model = None
            judge_model = None
            no_preflight = True  # this test checks model routing, not the live key ping

        picked = []
        real_key, real_make = ff._provider_key_present, ff.make_provider
        ff._provider_key_present = lambda name: name == "anthropic"
        ff.make_provider = lambda name, model, meter=None, judge_model=None: (
            picked.append(model) or object())
        try:
            ff.build_audit_providers(Args)
            self.assertEqual(picked, ["claude-sonnet-5"])
            picked.clear()
            Args.model = "claude-opus-4-8"
            ff.build_audit_providers(Args)
            self.assertEqual(picked, ["claude-opus-4-8"])
            picked.clear()
            Args.model, Args.economy = None, False
            ff.build_audit_providers(Args)
            self.assertEqual(picked, [ff.DEFAULT_MODELS["anthropic"]])
        finally:
            ff._provider_key_present, ff.make_provider = real_key, real_make

    def test_preflight_drops_dead_primary_and_falls_back(self):
        # anthropic key is present but DEAD (out of credits); openai is healthy.
        # Preflight must drop anthropic as author and fall back to openai, not
        # return [] and not crash a later fix call by picking the broke provider.
        class Args:
            provider = "anthropic"
            model = None
            economy = False
            use_both = True
            secondary_model = None
            judge_model = None
            no_preflight = False

        picked = []
        real_key = ff._provider_key_present
        real_make = ff.make_provider
        real_health = ff._provider_health
        ff._provider_key_present = lambda name: name in ("anthropic", "openai")
        ff._provider_health = lambda name: (
            (False, "credit balance is too low") if name == "anthropic" else (True, "ok"))
        ff.make_provider = lambda name, model, meter=None, judge_model=None: (
            picked.append(name) or object())
        try:
            out = ff.build_audit_providers(Args)
            # Only openai survives, and it is the (fallback) primary author.
            self.assertEqual([n for n, _ in out], ["openai"])
            self.assertEqual(picked, ["openai"])
        finally:
            ff._provider_key_present = real_key
            ff.make_provider = real_make
            ff._provider_health = real_health

    def test_preflight_all_keys_dead_returns_empty_with_diagnosis(self):
        # Every present key is rejected -> return [] AND set a credit-aware reason
        # so the caller can tell the user to top up (vs "no key set").
        class Args:
            provider = "anthropic"
            model = None
            economy = False
            use_both = True
            secondary_model = None
            judge_model = None
            no_preflight = False

        real_key = ff._provider_key_present
        real_health = ff._provider_health
        ff._provider_key_present = lambda name: name in ("anthropic", "openai")
        ff._provider_health = lambda name: (False, "credit balance is too low")
        try:
            out = ff.build_audit_providers(Args)
            self.assertEqual(out, [])
            self.assertIn("credit", ff._PROVIDER_DIAGNOSIS.lower())
        finally:
            ff._provider_key_present = real_key
            ff._provider_health = real_health


class CrossVerifyPromptTests(unittest.TestCase):
    def test_large_diff_is_capped_not_replaced_by_full_files(self):
        # A whole-file rewrite used to fall back to sending BOTH full copies to
        # the judge (~2x file size). Now it must always send a (capped) diff.
        original = "\n".join(f"line {i}" for i in range(9000)) + "\n"
        fixed = "\n".join(f"LINE {i}" for i in range(9000)) + "\n"
        captured = {}
        real = ff._judge

        def fake_judge(prov, system, prompt, schema, max_tokens=8000):
            captured["prompt"] = prompt
            return {"resolves": True, "regressions": False, "issues": [], "verdict": "keep"}

        ff._judge = fake_judge
        try:
            keep, _ = ff._cross_verify_fix(object(), "f.py", original, fixed, [])
        finally:
            ff._judge = real
        self.assertTrue(keep)
        p = captured["prompt"]
        self.assertIn("UNIFIED DIFF", p)
        self.assertNotIn("ORIGINAL FILE:", p)
        self.assertIn("truncated for verification", p)
        self.assertLess(len(p), 100_000)

    def test_no_diff_short_circuits_keep(self):
        keep, reason = ff._cross_verify_fix(object(), "f.py", "same\n", "same\n", [])
        self.assertTrue(keep)
        self.assertIn("no textual diff", reason)


class SchemaAndWiringTests(unittest.TestCase):
    def test_edits_schema_shape(self):
        s = ff.FIX_EDITS_SCHEMA
        self.assertEqual(set(s["required"]), {"changed", "edits", "fixed_titles", "notes"})
        item = s["properties"]["edits"]["items"]
        self.assertEqual(set(item["required"]), {"search", "replace"})

    def test_edit_generation_and_fallback_symbols_exist(self):
        self.assertTrue(callable(ff.generate_file_fix_edits))
        self.assertTrue(callable(ff.generate_file_fix))  # whole-file fallback kept

    def test_winify_survived_edits(self):
        # Regression guard from the WinError-2 trap: _winify must exist and
        # resolve commands via PATHEXT-aware lookup (npm.CMD etc.). For python
        # it resolves to a real executable path and keeps the arguments.
        self.assertTrue(callable(ff._winify))
        cmd = ff._winify(["python", "-V"])
        self.assertTrue(os.path.exists(cmd[0]) or cmd[0] == "python")
        self.assertEqual(cmd[1:], ["-V"])


class GitAwareEnumerationTests(unittest.TestCase):
    """Regression guard for the GrantFlow-public-audit trap: a gitignored stale
    snapshot of the app inside its own repo must NOT be enumerated for review."""

    def _make_repo(self, tmp):
        import subprocess
        subprocess.run(["git", "init", "-q", tmp], capture_output=True)
        env_files = {
            os.path.join("src", "app.js"): "console.log('real');\n",
            os.path.join("stale-copy", "app.js"): "console.log('stale');\n",
            ".gitignore": "stale-copy/\n",
        }
        for rel, body in env_files.items():
            full = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(full) or tmp, exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(body)

    def test_gitignored_nested_copy_is_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._make_repo(tmp)
            if ff._git_real_files(tmp) is None:
                self.skipTest("git unavailable")
            files = ff._enumerate_source_files(tmp, max_files=0)
            slashed = [f.replace("\\", "/") for f in files]
            self.assertIn("src/app.js", slashed)
            self.assertNotIn("stale-copy/app.js", slashed)

    def test_flexfactor_can_review_itself(self):
        # Regression guard: the old 200k size cap silently excluded
        # flexfactor.py (212k) from any audit of this repo - a permanent
        # blind spot on the tool's own largest file.
        files = [f.replace("\\", "/") for f in
                 ff._enumerate_source_files(_HERE, max_files=0)]
        self.assertIn("flexfactor.py", files)

    def test_non_git_dir_falls_back_to_walk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "src"))
            with open(os.path.join(tmp, "src", "app.js"), "w", encoding="utf-8") as fh:
                fh.write("console.log('x');\n")
            self.assertIsNone(ff._git_real_files(tmp))
            files = ff._enumerate_source_files(tmp, max_files=0)
            self.assertEqual([f.replace("\\", "/") for f in files], ["src/app.js"])


class FuzzyDedupeTests(unittest.TestCase):
    """Cross-model dedupe: two models wording ONE bug differently must count once."""

    def test_reworded_same_bug_merges_and_keeps_worse_severity(self):
        items = [
            {"file": "a.js", "line": 12, "severity": "medium",
             "title": "SQL injection in query builder"},
            {"file": "a.js", "line": 14, "severity": "high",
             "title": "Possible SQL injection in the query builder"},
        ]
        out = ff._dedupe_findings(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "high")

    def test_distinct_bugs_in_same_bucket_survive(self):
        items = [
            {"file": "a.js", "line": 12, "severity": "high",
             "title": "SQL injection in query builder"},
            {"file": "a.js", "line": 13, "severity": "low",
             "title": "unused variable shadows import"},
        ]
        self.assertEqual(len(ff._dedupe_findings(items)), 2)

    def test_same_title_different_file_not_merged(self):
        items = [
            {"file": "a.js", "line": 12, "severity": "high", "title": "missing null check"},
            {"file": "b.js", "line": 12, "severity": "high", "title": "missing null check"},
        ]
        self.assertEqual(len(ff._dedupe_findings(items)), 2)


class AuditLockTests(unittest.TestCase):
    """One audit per program: a live holder blocks, a dead holder's lock is stale."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._lock = os.path.join(self._tmp.name, "audit-test.lock")
        self._orig_path = ff._audit_lock_path
        self._orig_alive = ff._pid_alive
        ff._audit_lock_path = lambda project_dir: self._lock

    def tearDown(self):
        ff._audit_lock_path = self._orig_path
        ff._pid_alive = self._orig_alive
        self._tmp.cleanup()

    def test_acquire_then_live_holder_blocks_second(self):
        got = ff._acquire_audit_lock("X")
        self.assertEqual(got, self._lock)
        with open(self._lock, "w", encoding="utf-8") as fh:
            fh.write("999999")  # simulate a DIFFERENT process holding it
        ff._pid_alive = lambda pid: True
        self.assertIsNone(ff._acquire_audit_lock("X"))

    def test_stale_lock_from_dead_pid_is_taken_over(self):
        with open(self._lock, "w", encoding="utf-8") as fh:
            fh.write("999999")
        ff._pid_alive = lambda pid: False
        self.assertEqual(ff._acquire_audit_lock("X"), self._lock)
        self.assertEqual(open(self._lock, encoding="utf-8").read(), str(os.getpid()))

    def test_release_removes_lock(self):
        got = ff._acquire_audit_lock("X")
        ff._release_audit_lock(got)
        self.assertFalse(os.path.exists(self._lock))

    def test_pid_alive_on_self_and_bogus(self):
        self.assertTrue(ff._pid_alive(os.getpid()))
        self.assertFalse(ff._pid_alive(-1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
