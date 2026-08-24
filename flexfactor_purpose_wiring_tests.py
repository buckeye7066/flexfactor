#!/usr/bin/env python3
"""The purpose-evidence gather must actually RUN inside an audit.

This repo has a documented trap with three prior instances: a module is written,
tested in isolation, committed - and then never actually reaches production
behaviour (`flexfactor_runstate`, `set_phase`/`record_cycle`/`record_spend`, the
`_UI_EXPLORER_JS` constant). This is the fourth, found live on 2026-08-23.

`gather_purpose_evidence(git_runner=..., gh_runner=...)` documents that both
runners return **stdout (a string) or None**, and calls `.splitlines()` on the
result. The audit was injecting:

    git_runner=lambda a, cwd: _git_argv(a, cwd)      # -> CompletedProcess
    def _gh_runner(a, cwd): cp = _run(list(a), ...)  # -> drops the "gh" argv[0]

so the FIRST git call raised `AttributeError: 'CompletedProcess' object has no
attribute 'splitlines'`, the whole gather aborted inside its own try/except, and
every audit put one line - "[purpose evidence gathering failed: ...]" - into the
prompt where the entire cited evidence block belonged. The evidence cache stayed
empty, so `_purpose_confidence_for` graded purpose confidence on nothing.

Measured before/after on this repository: **0 sources -> 78 sources** (50 commit
subjects, 6 branches, 30 pull requests).

The module's own unit tests could never have caught this: they inject their own
correct fakes. Only a test of THE WIRING can. That is what this file is.

    python flexfactor_purpose_wiring_tests.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load_flexfactor():
    if "flexfactor" in sys.modules:
        return sys.modules["flexfactor"]
    tmp = tempfile.mkdtemp(prefix="ff-purpose-state-")
    spec = importlib.util.spec_from_file_location(
        "flexfactor", os.path.join(HERE, "flexfactor.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["flexfactor"] = module          # BEFORE exec_module
    spec.loader.exec_module(module)
    # Never touch the owner's real state (see flexfactor_tests.py's hygiene note).
    module.BRAIN_PATH = os.path.join(tmp, "brain.json")
    module.STATUS_PATH = os.path.join(tmp, "status.json")
    module.RUNS_PATH = os.path.join(tmp, "runs")
    module._auto_activate_fcc_proxy = lambda *a, **k: None
    return module


ff = _load_flexfactor()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


class PurposeEvidenceWiringTests(unittest.TestCase):
    """Drive the REAL injection over a real (tiny) repository."""

    @classmethod
    def setUpClass(cls):
        cls.repo = tempfile.mkdtemp(prefix="ff-purpose-repo-")
        _git(cls.repo, "init", "-q")
        _git(cls.repo, "config", "user.email", "t@example.com")
        _git(cls.repo, "config", "user.name", "T")
        with open(os.path.join(cls.repo, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# Widget\n\nWidget turns invoices into receipts.\n")
        with open(os.path.join(cls.repo, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"name":"widget","version":"1.0.0","scripts":{"build":"tsc"}}\n')
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", "feat: turn invoices into receipts")
        _git(cls.repo, "commit", "-q", "--allow-empty", "-m", "fix: rounding on tax lines")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def setUp(self):
        ff._PURPOSE_EVIDENCE_CACHE.clear()
        self.addCleanup(ff._PURPOSE_EVIDENCE_CACHE.clear)

    def test_the_gather_does_not_abort_and_the_cache_is_populated(self):
        name, text = ff._gather_from_folder(self.repo)
        self.assertNotIn("purpose evidence gathering failed", text,
                         "the whole evidence block is replaced by this line when "
                         "an injected runner breaks the module's contract")
        self.assertEqual(len(ff._PURPOSE_EVIDENCE_CACHE), 1,
                         "an aborted gather leaves the cache empty, and "
                         "_purpose_confidence_for then grades on nothing")

    def test_git_history_actually_reaches_the_evidence(self):
        ff._gather_from_folder(self.repo)
        evidence = next(iter(ff._PURPOSE_EVIDENCE_CACHE.values()))
        commits = (evidence.get("history") or {}).get("commits") or []
        self.assertTrue(commits, "commit subjects are a purpose signal and were absent")
        self.assertTrue(any("invoices into receipts" in c for c in commits), commits)
        self.assertTrue(evidence.get("sources"), "evidence must cite its sources")

    def test_both_injected_runners_return_stdout_or_none_never_a_process(self):
        """The exact contract that was broken.

        `gather_purpose_evidence` calls `.splitlines()` on what a runner returns.
        A CompletedProcess has no `.splitlines()`, and the failure surfaces three
        frames away as a swallowed AttributeError.
        """
        captured = {}

        def capture(project_dir, *, git_runner=None, gh_runner=None, **kw):
            captured["git"] = git_runner
            captured["gh"] = gh_runner
            return {"sources": [], "contradictions": [], "unknowns": [],
                    "integrations": [], "schemas": [], "routes": [],
                    "product_claims": [], "deploy": [], "history": {},
                    "tests": [], "manifests": []}

        fp = ff._purpose_module()
        self.assertIsNotNone(fp, "the purpose module must be importable")
        real_gather = fp.gather_purpose_evidence
        real_render = fp.render_purpose_evidence_block
        fp.gather_purpose_evidence = capture
        fp.render_purpose_evidence_block = lambda ev: ""
        try:
            ff._gather_from_folder(self.repo)
        finally:
            fp.gather_purpose_evidence = real_gather
            fp.render_purpose_evidence_block = real_render

        out = captured["git"](["log", "-1", "--format=%s"], self.repo)
        self.assertIsInstance(out, str, f"git_runner returned {type(out).__name__}")
        self.assertIn("rounding on tax lines", out)
        self.assertIsNone(captured["git"](["not-a-git-command"], self.repo),
                          "a failed command must be None, not an empty success")

        # gh may be absent or unauthenticated on the machine running this; what
        # must hold either way is that the runner does not return a process and
        # does not execute something that merely SHARES A NAME with a gh
        # subcommand. `pr` is /usr/bin/pr on this machine - a text paginator -
        # and the old code ran exactly that.
        gh_out = captured["gh"](["pr", "list", "--limit", "1"], self.repo)
        self.assertTrue(gh_out is None or isinstance(gh_out, str),
                        f"gh_runner returned {type(gh_out).__name__}")
        if isinstance(gh_out, str):
            self.assertNotIn("Try '/usr/bin/pr --help'", gh_out)

    def test_the_gh_runner_actually_invokes_gh(self):
        """Dropping argv[0] made this look like 'GitHub unavailable' forever."""
        seen = []

        def fake_run(cmd, cwd=None, timeout=None, **kw):
            seen.append(list(cmd))
            class CP:
                returncode, stdout, stderr = 0, "[]", ""
            return CP()

        captured = {}

        def capture(project_dir, *, git_runner=None, gh_runner=None, **kw):
            captured["gh"] = gh_runner
            return {"sources": [], "contradictions": [], "unknowns": [],
                    "integrations": [], "schemas": [], "routes": [],
                    "product_claims": [], "deploy": [], "history": {},
                    "tests": [], "manifests": []}

        fp = ff._purpose_module()
        real_gather, real_render = fp.gather_purpose_evidence, fp.render_purpose_evidence_block
        real_run = ff._run
        fp.gather_purpose_evidence = capture
        fp.render_purpose_evidence_block = lambda ev: ""
        try:
            ff._gather_from_folder(self.repo)
            ff._run = fake_run
            captured["gh"](["pr", "list", "--state", "all"], self.repo)
        finally:
            ff._run = real_run
            fp.gather_purpose_evidence = real_gather
            fp.render_purpose_evidence_block = real_render

        self.assertTrue(seen, "the gh runner must go through _run (the policy gate)")
        self.assertEqual(seen[-1][0], "gh",
                         f"the executable was dropped: {seen[-1][:3]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
