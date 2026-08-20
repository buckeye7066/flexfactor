#!/usr/bin/env python3
"""Tests for the unconditional pre-work repo cleanup.

The property under test is the accounting identity:

    candidates == acted_on + skipped + failed

plus the two things the owner's rules make non-negotiable: uncommitted work is
COMMITTED (never discarded), and a red pull request is left OPEN rather than
force-merged.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

import flexfactor_autoclean as ac


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _repo():
    d = tempfile.mkdtemp()
    _git(["init", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@example.com"], d)
    _git(["config", "user.name", "T"], d)
    with open(os.path.join(d, "seed.txt"), "w") as fh:
        fh.write("seed\n")
    _git(["add", "-A"], d)
    _git(["commit", "-qm", "seed"], d)
    return d


class AccountingIdentityTests(unittest.TestCase):
    def test_summarise_rejects_a_step_that_loses_items(self):
        """The identity must be ENFORCED, not merely documented."""
        bad = {"step": "x", "candidates": 3, "acted_on": [1],
               "skipped": [], "failed": []}
        with self.assertRaises(AssertionError):
            ac.summarise([bad])

    def test_summarise_accepts_a_fully_accounted_step(self):
        ok = {"step": "x", "candidates": 3, "acted_on": [1],
              "skipped": [{"item": 2, "reason": "r"}],
              "failed": [{"item": 3, "reason": "r"}]}
        total = ac.summarise([ok])
        self.assertEqual(total["candidates"], 3)
        self.assertEqual((total["acted_on"], total["skipped"], total["failed"]),
                         (1, 1, 1))


class CommitPendingChangesTests(unittest.TestCase):
    def test_uncommitted_work_is_committed_not_discarded(self):
        d = _repo()
        with open(os.path.join(d, "wip.txt"), "w") as fh:
            fh.write("owner work\n")
        res = ac.commit_pending_changes(d)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(len(res["acted_on"]), 1)
        # The content must still exist on disk AND be in the commit.
        with open(os.path.join(d, "wip.txt")) as fh:
            self.assertEqual(fh.read(), "owner work\n")
        self.assertEqual(_git(["status", "--porcelain"], d).stdout.strip(), "")
        log = _git(["log", "--oneline", "-1"], d).stdout
        self.assertIn("autoclean", log)

    def test_clean_tree_reports_zero_candidates_not_success(self):
        d = _repo()
        res = ac.commit_pending_changes(d)
        self.assertEqual(res["candidates"], 0)
        self.assertEqual(res["acted_on"], [])

    def test_identity_holds_on_a_dirty_tree(self):
        d = _repo()
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(name)
        res = ac.commit_pending_changes(d)
        ac.summarise([res])  # must not raise


class PullRequestPolicyTests(unittest.TestCase):
    """Red/draft/conflicting PRs are SKIPPED WITH A REASON, never merged."""

    def _list_stub(self, prs):
        def fake(args, cwd):
            return prs, ""
        return fake

    def test_red_pr_is_skipped_with_the_failing_check_named(self):
        prs = [{"number": 7, "title": "t", "isDraft": False,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": [{"name": "ci", "conclusion": "FAILURE"}]}]
        orig = ac._gh_json
        ac._gh_json = self._list_stub(prs)
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r")
        finally:
            ac._gh_json = orig
        self.assertEqual(res["acted_on"], [])
        self.assertEqual(len(res["skipped"]), 1)
        self.assertIn("ci=FAILURE", res["skipped"][0]["reason"])
        ac.summarise([res])

    def test_draft_and_conflicting_are_skipped_with_distinct_reasons(self):
        prs = [
            {"number": 1, "isDraft": True, "mergeable": "MERGEABLE",
             "statusCheckRollup": []},
            {"number": 2, "isDraft": False, "mergeable": "CONFLICTING",
             "statusCheckRollup": []},
        ]
        orig = ac._gh_json
        ac._gh_json = self._list_stub(prs)
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r")
        finally:
            ac._gh_json = orig
        self.assertEqual(res["acted_on"], [])
        by_num = {s["item"]: s["reason"] for s in res["skipped"]}
        self.assertIn("draft", by_num[1])
        self.assertIn("conflict", by_num[2])
        ac.summarise([res])

    def test_green_pr_is_merged(self):
        prs = [{"number": 9, "isDraft": False, "mergeable": "MERGEABLE",
                "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}]}]
        orig_json, orig_run = ac._gh_json, ac._run
        merged = []

        def fake_run(cmd, cwd, timeout=ac.TIMEOUT_S):
            merged.append(cmd)
            return 0, "merged"

        ac._gh_json = self._list_stub(prs)
        ac._run = fake_run
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r")
        finally:
            ac._gh_json, ac._run = orig_json, orig_run
        self.assertEqual(res["acted_on"], [9])
        self.assertTrue(any("merge" in c for c in merged[0]))
        ac.summarise([res])

    def test_unreachable_gh_is_a_FAILURE_not_a_silent_clean_repo(self):
        orig = ac._gh_json
        ac._gh_json = lambda args, cwd: (None, "gh: not found")
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r")
        finally:
            ac._gh_json = orig
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(len(res["failed"]), 1)
        ac.summarise([res])


class ReportingStepsTests(unittest.TestCase):
    def test_open_issues_are_accounted_not_dropped(self):
        orig = ac._gh_json
        ac._gh_json = lambda args, cwd: ([{"number": 3, "title": "x"},
                                          {"number": 4, "title": "y"}], "")
        try:
            res = ac.report_open_issues("/nonexistent", repo="o/r")
        finally:
            ac._gh_json = orig
        self.assertEqual(res["candidates"], 2)
        self.assertEqual(len(res["skipped"]), 2)
        ac.summarise([res])

    def test_missing_repo_slug_is_reported_not_assumed_clean(self):
        res = ac.report_dependabot("/nonexistent", repo=None)
        self.assertEqual(res["candidates"], 1)
        self.assertIn("no GitHub repo slug", res["skipped"][0]["reason"])
        ac.summarise([res])


class FormatSummaryTests(unittest.TestCase):
    def test_every_skip_reason_survives_into_the_report(self):
        step = {"step": "open-pull-requests", "candidates": 2,
                "acted_on": [1],
                "skipped": [{"item": 2, "reason": "checks not green: ci=FAILURE"}],
                "failed": []}
        text = ac.format_summary(ac.summarise([step]))
        self.assertIn("ci=FAILURE", text)
        self.assertIn("1 actioned", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
