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

    def test_git_exit_status_is_captured_before_the_pipe_is_torn_down(self):
        """A launcher that could not read git's exit code refused clean main.

        `Select-Object -First` stops the pipeline the instant it has its item,
        which kills the upstream git process and leaves $LASTEXITCODE at -1.
        The branch guard read that -1 as a git failure and rejected a checkout
        that was already on a clean, current main, so no desktop shortcut could
        open FlexFactor at all.
        """
        source = REFRESH.read_text(encoding="ascii")
        self.assertNotIn("2>$null | Select-Object", source)
        for captured in ("originExit", "branchExit", "headExit", "upstreamExit"):
            self.assertIn("${0} = $LASTEXITCODE".format(captured), source)
        self.assertIn("if ($originExit -ne 0", source)
        self.assertIn("if ($branchExit -ne 0", source)
        self.assertIn("if ($headExit -ne 0 -or $upstreamExit -ne 0", source)

    def test_start_process_exit_codes_survive_the_child_exiting(self):
        """Start-Process -PassThru does not cache the native handle.

        Once the child exits the exit code degrades to $null, and $null -ne 0
        is TRUE, so a healthy `git fetch` was indistinguishable from a failed
        one and the launcher refused to start. The handle must be retained
        before the exit code is ever read.
        """
        source = REFRESH.read_text(encoding="ascii")
        code = [line for line in source.splitlines()
                if not line.strip().startswith("#")]
        launched = [line for line in code if "-PassThru" in line]
        retained = [line for line in code if ".Handle" in line]
        self.assertTrue(launched)
        self.assertEqual(len(launched), len(retained))
        for name in ("fetch", "install"):
            retain = "$null = ${0}.Handle".format(name)
            self.assertIn(retain, source)
            self.assertLess(source.index(retain),
                            source.index("${0}.ExitCode".format(name)))

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
