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


if __name__ == "__main__":
    unittest.main(verbosity=2)
