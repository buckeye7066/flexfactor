"""Regression proof for the stale-desktop-runtime failure seen 2026-09-03.

The live run executed pre-fix purpose handling even though GitHub main already
contained the structured-output rotation repair. A dirty working tree could
survive because the source refresher returned early when HEAD == origin/main,
and several refresh failures deliberately continued with the installed tree.
Those are forbidden states for the owner's canonical desktop checkout.
"""
from __future__ import annotations

import inspect
from pathlib import Path
import unittest

import flexfactor as ff
import flexfactor_rotation as rotation


ROOT = Path(__file__).resolve().parent
REFRESH = ROOT / "scripts" / "flexfactor_source_refresh.ps1"


class StaleRuntimeRecurrenceTests(unittest.TestCase):
    def test_source_refresh_checks_worktree_before_any_current_head_shortcut(self):
        source = REFRESH.read_text(encoding="ascii")
        status = source.index("status --porcelain --untracked-files=all")
        incoming = source.index("$incomingChanges")
        self.assertLess(status, incoming)
        self.assertNotIn("$head\".Trim() -eq \"$upstream\".Trim()) {\n        return", source)

    def test_dirty_and_divergent_work_are_preserved_before_main_is_rebound(self):
        source = REFRESH.read_text(encoding="ascii")
        self.assertIn("stash push --include-untracked", source)
        self.assertIn("flexfactor/local-preserved-", source)
        self.assertIn("branch $rescue HEAD", source)
        self.assertIn("reset --hard origin/main", source)
        self.assertIn("exact clean origin/main tree", source)

    def test_desktop_refresh_never_silently_runs_unverified_installed_source(self):
        source = REFRESH.read_text(encoding="ascii")
        self.assertIn("FlexFactor will not run from an unverified/stale checkout", source)
        self.assertNotIn("using the installed checkout", source.lower())
        self.assertNotIn("automatic refresh was not attempted", source.lower())
        source.encode("ascii")

    def test_program_understanding_shape_failure_still_rotates_inside_same_call(self):
        inference = inspect.getsource(ff._infer_purpose_contract)
        self.assertIn("structured_validated", inference)
        self.assertIn("validator=_validate_program_understanding_response", inference)
        provider = inspect.getsource(rotation.RotatingProvider._run)
        self.assertIn("_result_validator", provider)
        self.assertIn("result_validator(result)", provider)

    def test_current_queue_preflight_resolves_targets_before_work_when_dashboard_active(self):
        source = inspect.getsource(ff.run_audit)
        self.assertIn("resolved_targets", source)
        self.assertIn("if prompts or session_prompt or getattr(args, \"dashboard\", True)", source)
        self.assertLess(source.index("resolved_targets"), source.index("audit_one_program"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
