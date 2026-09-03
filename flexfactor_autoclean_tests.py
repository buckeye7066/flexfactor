#!/usr/bin/env python3
"""Tests for the unconditional pre-work repo cleanup.

The property under test is the accounting identity:

    candidates == acted_on + skipped + failed

plus the two things the owner's rules make non-negotiable: uncommitted work is
COMMITTED (never discarded), and a red pull request is left OPEN rather than
force-merged.
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import tempfile
import unittest

import flexfactor_autoclean as ac


def _test_runner(cmd, cwd, timeout=ac.TIMEOUT_S):
    """The test scaffold's own runner.

    `flexfactor_autoclean` no longer owns a launcher (g-5 class: a raw
    subprocess outside FlexFactor's command chokepoint). Production injects a
    runner backed by `flexfactor._run`; these tests, which are not the product,
    inject this one.
    """
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


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
        res = ac.commit_pending_changes(d, run=_test_runner)
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
        res = ac.commit_pending_changes(d, run=_test_runner)
        self.assertEqual(res["candidates"], 0)
        self.assertEqual(res["acted_on"], [])

    def test_identity_holds_on_a_dirty_tree(self):
        d = _repo()
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(name)
        res = ac.commit_pending_changes(d, run=_test_runner)
        ac.summarise([res])  # must not raise


class InterruptedGitRecoveryTests(unittest.TestCase):
    def test_interrupted_merge_is_aborted_before_normal_cleanup(self):
        d = _repo()
        _git(["checkout", "-qb", "feature"], d)
        with open(os.path.join(d, "seed.txt"), "w") as fh:
            fh.write("feature\n")
        _git(["commit", "-am", "feature"], d)
        _git(["checkout", "-q", "main"], d)
        with open(os.path.join(d, "seed.txt"), "w") as fh:
            fh.write("main\n")
        _git(["commit", "-am", "main"], d)
        merged = _git(["merge", "feature"], d)
        self.assertNotEqual(0, merged.returncode)
        self.assertTrue(_git(["diff", "--name-only", "--diff-filter=U"], d).stdout.strip())

        recovered = ac.recover_interrupted_git_operation(d, run=_test_runner)

        self.assertEqual(["aborted interrupted merge"], recovered["acted_on"])
        self.assertEqual([], recovered["failed"])
        self.assertEqual("", _git(["status", "--porcelain"], d).stdout.strip())
        self.assertEqual("main", _git(["rev-parse", "--abbrev-ref", "HEAD"], d).stdout.strip())

    def test_bare_unmerged_index_is_blocked_without_guessing_a_side(self):
        d = _repo()
        _git(["update-index", "--index-info"], d)  # a harmless command to retain a normal index
        # A synthetic conflict entry does not carry MERGE_HEAD. Recovery must
        # report rather than destructively pick ours/theirs.
        blob = _git(["hash-object", "-w", "seed.txt"], d).stdout.strip()
        stream = f"100644 {blob} 1\tseed.txt\n100644 {blob} 2\tseed.txt\n100644 {blob} 3\tseed.txt\n"
        # Feed bytes so Windows does not translate LF to CRLF inside the
        # index-info protocol and silently turn the staged path into seed.txt\r.
        proc = subprocess.run(["git", "update-index", "--index-info"], cwd=d,
                              input=stream.encode("utf-8"), capture_output=True)
        self.assertEqual(0, proc.returncode)
        self.assertTrue(
            _git(["ls-files", "--unmerged"], d).stdout.strip(),
            "the test must create index stages 1/2/3 before exercising recovery",
        )
        recovered = ac.recover_interrupted_git_operation(d, run=_test_runner)
        self.assertEqual([], recovered["acted_on"])
        self.assertEqual(1, len(recovered["failed"]))
        self.assertIn("no side was chosen", recovered["failed"][0]["reason"])


class SweptTreeIsVerifiedTests(unittest.TestCase):
    """Autoclean VERIFIES what it commits, and never reads `None` as a pass.

    The regression these guard: for weeks `commit_pending_changes` ran
    `git add -A` with no verification of any kind and committed under a message
    whose "these changes were already on disk" reads as "not ours". FlexFactor
    aborts routinely and nothing restores the tree, so the sweep was frequently
    committing its OWN half-verified output from a dead run - 47 real
    regressions reached four repositories that way.

    The contract is deliberately narrow, because the fix must not cost the tool
    any capability: the commit ALWAYS happens (no work is ever discarded, no
    approval gate, no dry-run mode). Only the VERDICT changes.
    """

    def _dirty(self):
        d = _repo()
        with open(os.path.join(d, "wip.txt"), "w") as fh:
            fh.write("owner work\n")
        return d

    def test_a_green_gate_is_recorded_as_VERIFIED(self):
        d = self._dirty()
        res = ac.commit_pending_changes(
            d, run=_test_runner, verify=lambda: (True, "$ npm test\nok"))
        self.assertIs(res["verified"], True)
        body = _git(["log", "-1", "--format=%B"], d).stdout
        self.assertIn("Project gate: VERIFIED", body)

    def test_a_RED_gate_still_commits_but_is_never_called_a_pass(self):
        d = self._dirty()
        res = ac.commit_pending_changes(
            d, run=_test_runner, verify=lambda: (False, "$ npm test\n1 failing"))
        # The work is COMMITTED - losing a sibling agent's WIP would be worse
        # than committing it, and refusing would be the approval gate the owner
        # has banned.
        self.assertEqual(len(res["acted_on"]), 1)
        self.assertEqual(_git(["status", "--porcelain"], d).stdout.strip(), "")
        # ...but the verdict is the truth, in the result AND in history.
        self.assertIs(res["verified"], False)
        body = _git(["log", "-1", "--format=%B"], d).stdout
        self.assertIn("Project gate: RED", body)
        self.assertNotIn("1 failing", body)
        self.assertIn("1 failing", res["verify_note"])

    def test_NOTHING_RAN_is_UNVERIFIED_and_is_not_success(self):
        """`None` is the value that used to be reported as success."""
        d = self._dirty()
        res = ac.commit_pending_changes(
            d, run=_test_runner, verify=lambda: (None, "no verify command"))
        self.assertIsNone(res["verified"])
        self.assertIsNot(res["verified"], True)
        self.assertIn("Project gate: UNVERIFIED",
                      _git(["log", "-1", "--format=%B"], d).stdout)

    def test_a_caller_that_supplies_no_gate_gets_UNVERIFIED_not_a_pass(self):
        d = self._dirty()
        res = ac.commit_pending_changes(d, run=_test_runner)
        self.assertIsNone(res["verified"])
        self.assertIn("no verification gate", res["verify_note"])

    def test_a_gate_that_RAISES_is_UNVERIFIED_never_a_pass(self):
        def boom():
            raise RuntimeError("verify blew up")
        d = self._dirty()
        res = ac.commit_pending_changes(d, run=_test_runner, verify=boom)
        self.assertIsNone(res["verified"])
        self.assertIn("verify blew up", res["verify_note"])
        self.assertEqual(len(res["acted_on"]), 1)   # still committed

    def test_a_bare_tristate_is_accepted_as_well_as_the_ok_log_pair(self):
        d = self._dirty()
        res = ac.commit_pending_changes(d, run=_test_runner, verify=lambda: True)
        self.assertIs(res["verified"], True)

    def test_an_unrecognisable_gate_return_is_UNVERIFIED_never_truthy(self):
        """A truthy non-tri-state must not sneak through as a pass."""
        for weird in ("passed", 1, [], {"ok": True}):
            with self.subTest(weird=weird):
                ok, _ = ac._verdict(weird)
                self.assertIsNone(ok)

    def test_a_clean_tree_runs_no_gate_at_all(self):
        """Verification costs a build; do not spend one with nothing to commit."""
        calls = []
        d = _repo()
        ac.commit_pending_changes(
            d, run=_test_runner, verify=lambda: calls.append(1) or (True, ""))
        self.assertEqual(calls, [])

    def test_the_gate_runs_BEFORE_the_commit_not_after(self):
        """It must read the tree a reader of the commit will check out."""
        seen = {}
        d = self._dirty()

        def verify():
            seen["dirty_at_verify_time"] = bool(
                _git(["status", "--porcelain"], d).stdout.strip())
            return True, ""
        ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertTrue(seen["dirty_at_verify_time"])


    def test_gate_created_source_is_not_swept_and_invalidates_green(self):
        d = self._dirty()

        def verify():
            with open(os.path.join(d, "generated-source.py"), "w") as fh:
                fh.write("print('gate output')\n")
            return True, "all passed"

        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"])
        self.assertIn("non-generated path", res["verify_note"])
        committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], d).stdout
        self.assertIn("wip.txt", committed)
        self.assertNotIn("generated-source.py", committed)
        # Post-verify worktree is restored to the exact candidate; gate-created
        # non-generated files are removed.
        self.assertFalse(os.path.exists(os.path.join(d, "generated-source.py")))

    def test_gate_modifies_tracked_file_and_is_restored(self):
        d = _repo()
        with open(os.path.join(d, "wip.txt"), "w") as fh:
            fh.write("owner work\n")
        # Stage candidate
        def verify():
            # mutate tracked candidate after staging
            with open(os.path.join(d, "wip.txt"), "w") as fh:
                fh.write("mutated by gate\n")
            return True, "ok"
        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"])
        # Working tree restored to candidate content
        with open(os.path.join(d, "wip.txt")) as fh:
            self.assertEqual(fh.read(), "owner work\n")

    def test_gate_deletes_tracked_file_and_is_restored(self):
        d = _repo()
        with open(os.path.join(d, "wip.txt"), "w") as fh:
            fh.write("owner work\n")
        def verify():
            os.remove(os.path.join(d, "wip.txt"))
            return True, "ok"
        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"])
        self.assertTrue(os.path.exists(os.path.join(d, "wip.txt")))

    def test_gate_renames_tracked_file_and_new_name_is_removed(self):
        d = _repo()
        with open(os.path.join(d, "wip.txt"), "w") as fh:
            fh.write("owner work\n")
        def verify():
            os.rename(os.path.join(d, "wip.txt"), os.path.join(d, "wip2.txt"))
            return True, "ok"
        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"])
        self.assertTrue(os.path.exists(os.path.join(d, "wip.txt")))
        self.assertFalse(os.path.exists(os.path.join(d, "wip2.txt")))

    def test_gate_stages_new_file_and_it_is_not_kept(self):
        d = _repo()
        def verify():
            with open(os.path.join(d, "newfile.txt"), "w") as fh:
                fh.write("gate staged\n")
            _git(["add", "newfile.txt"], d)
            return True, "ok"
        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"])
        committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], d).stdout
        self.assertNotIn("newfile.txt", committed)
        self.assertFalse(os.path.exists(os.path.join(d, "newfile.txt")))

    def test_candidate_intended_delete_survives_and_gate_recreate_removed(self):
        """Baseline has old.txt; candidate deletes it; gate recreates+stages it.
        Commit and worktree must still omit old.txt; unrelated owner WIP survives."""
        d = _repo()
        # Add a baseline tracked file
        with open(os.path.join(d, "old.txt"), "w") as fh:
            fh.write("baseline\n")
        _git(["add", "-A"], d)
        _git(["commit", "-qm", "add old.txt"], d)
        # Owner WIP staged-add
        with open(os.path.join(d, "new_owner.txt"), "w") as fh:
            fh.write("owner wip\n")
        # Pre-existing ignored debris that must be preserved
        os.makedirs(os.path.join(d, "node_modules"), exist_ok=True)
        with open(os.path.join(d, "node_modules", "pre.txt"), "w") as fh:
            fh.write("pre\n")
        # Candidate intends to delete old.txt
        os.remove(os.path.join(d, "old.txt"))
        def verify():
            # Gate recreates + stages old.txt falsely
            with open(os.path.join(d, "old.txt"), "w") as fh:
                fh.write("recreated by gate\n")
            _git(["add", "old.txt"], d)
            # Also create new ignored debris made by the gate
            with open(os.path.join(d, "node_modules", "post.txt"), "w") as fh:
                fh.write("post\n")
            return True, "ok"
        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"])
        # Commit omits old.txt; worktree omits old.txt
        tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], d).stdout
        self.assertNotIn("old.txt", tree)
        self.assertFalse(os.path.exists(os.path.join(d, "old.txt")))
        # Unrelated staged-added owner WIP survives in commit and worktree
        self.assertIn("new_owner.txt", tree)
        self.assertTrue(os.path.exists(os.path.join(d, "new_owner.txt")))
        # Ignored debris: pre-existing kept, newly created removed
        self.assertTrue(os.path.exists(os.path.join(d, "node_modules", "pre.txt")))
        self.assertFalse(os.path.exists(os.path.join(d, "node_modules", "post.txt")))

    def test_gate_created_ephemeral_output_is_cleaned_not_committed(self):
        d = self._dirty()

        def verify():
            cache = os.path.join(d, "__pycache__")
            os.makedirs(cache, exist_ok=True)
            with open(os.path.join(cache, "x.pyc"), "wb") as fh:
                fh.write(b"compiled")
            return True, "tests passed"

        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIs(res["verified"], True)
        committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], d).stdout
        self.assertNotIn("__pycache__", committed)
        self.assertFalse(os.path.exists(os.path.join(d, "__pycache__", "x.pyc")))

    def test_gate_staging_generated_output_cannot_change_the_committed_tree(self):
        d = self._dirty()

        def verify():
            cache = os.path.join(d, "__pycache__")
            os.makedirs(cache, exist_ok=True)
            with open(os.path.join(cache, "staged.pyc"), "wb") as fh:
                fh.write(b"compiled")
            _git(["add", "-f", "__pycache__/staged.pyc"], d)
            return True, "tests passed"

        res = ac.commit_pending_changes(d, run=_test_runner, verify=verify)
        self.assertIsNone(res["verified"], "a gate that mutates the index is never VERIFIED")
        self.assertIn("changed the Git index", res["verify_note"])
        committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], d).stdout
        self.assertIn("wip.txt", committed)
        self.assertNotIn("__pycache__/staged.pyc", committed)
        self.assertFalse(os.path.exists(os.path.join(d, "__pycache__", "staged.pyc")))

    def test_gate_output_is_redacted_and_never_written_to_history(self):
        d = self._dirty()
        secret = "sk-proj-supersecret123456789"
        res = ac.commit_pending_changes(
            d, run=_test_runner,
            verify=lambda: (False, "API_KEY=" + secret + "\nfailed"))
        self.assertNotIn(secret, res["verify_note"])
        self.assertIn("[REDACTED]", res["verify_note"])
        body = _git(["log", "-1", "--format=%B"], d).stdout
        self.assertNotIn(secret, body)
        self.assertNotIn("API_KEY", body)

    def test_build_only_success_says_exactly_what_ran(self):
        d = self._dirty()
        ac.commit_pending_changes(
            d, run=_test_runner,
            verify=lambda: (True, "build ok\n(no project test suite configured)"))
        body = _git(["log", "-1", "--format=%B"], d).stdout
        self.assertIn("VERIFIED-BUILD-ONLY", body)
        self.assertNotIn("suite ran and passed", body)

    def test_the_commit_message_stops_claiming_the_changes_are_not_ours(self):
        d = self._dirty()
        ac.commit_pending_changes(d, run=_test_runner, verify=lambda: (True, ""))
        body = _git(["log", "-1", "--format=%B"], d).stdout
        self.assertIn("cannot tell whether these edits are the owner's work in "
                      "progress or its own output", body)

    def test_clean_repo_threads_the_gate_through_to_the_sweep(self):
        """A gate wired only into the function nobody calls is no gate."""
        calls = []
        d = self._dirty()

        def fake_run(cmd, cwd, timeout=ac.TIMEOUT_S):
            if cmd and cmd[0] == "git":
                return _test_runner(cmd, cwd, timeout)
            return 1, "not reachable in this test"
        ac.clean_repo(d, repo=None, run=fake_run,
                      verify=lambda: calls.append(1) or (True, "ok"))
        self.assertEqual(calls, [1])

    def test_the_summary_names_the_verdict_so_a_red_sweep_is_not_read_as_clean(self):
        step = {"step": "uncommitted-changes", "candidates": 1,
                "acted_on": ["M src/app.js"], "skipped": [], "failed": [],
                "verified": False, "verify_note": "1 failing"}
        text = ac.format_summary(ac.summarise([step]))
        self.assertIn("gate: RED", text)
        self.assertIn("1 failing", text)

    def test_the_summary_labels_an_unverified_sweep_as_such(self):
        step = {"step": "uncommitted-changes", "candidates": 1,
                "acted_on": ["M src/app.js"], "skipped": [], "failed": [],
                "verified": None, "verify_note": "no verify command"}
        self.assertIn("gate: UNVERIFIED",
                      ac.format_summary(ac.summarise([step])))


class PullRequestPolicyTests(unittest.TestCase):
    """Red/draft/conflicting PRs are SKIPPED WITH A REASON, never merged."""

    def _list_stub(self, prs):
        def fake(args, cwd, run=None):
            return prs, ""
        return fake

    def test_red_pr_is_skipped_with_the_failing_check_named(self):
        prs = [{"number": 7, "title": "t", "isDraft": False,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": [{"name": "ci", "conclusion": "FAILURE"}]}]
        orig = ac._gh_json
        ac._gh_json = self._list_stub(prs)
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r", run=_test_runner)
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
            res = ac.land_open_prs("/nonexistent", repo="o/r", run=_test_runner)
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
        orig_json = ac._gh_json
        merged = []

        def fake_run(cmd, cwd, timeout=ac.TIMEOUT_S):
            merged.append(cmd)
            return 0, "merged"

        ac._gh_json = self._list_stub(prs)
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r", run=fake_run)
        finally:
            ac._gh_json = orig_json
        self.assertEqual(res["acted_on"], [9])
        self.assertTrue(any("merge" in c for c in merged[0]))
        ac.summarise([res])

    def test_unreachable_gh_is_a_FAILURE_not_a_silent_clean_repo(self):
        orig = ac._gh_json
        ac._gh_json = lambda args, cwd, run=None: (None, "gh: not found")
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r", run=_test_runner)
        finally:
            ac._gh_json = orig
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(len(res["failed"]), 1)
        ac.summarise([res])


class UnfinishedChecksAreNotGreenTests(unittest.TestCase):
    """2026-08-25. `_OK_CHECK` contained None and "", so a CheckRun that had not
    finished read as a passing check and the PR was MERGED with its CI still
    running. Reproduced against the real `gh pr list --json statusCheckRollup`
    shape: an unfinished CheckRun reports `conclusion: ""` and carries no
    `state` key at all, so `c.get("conclusion") or c.get("state")` was None.

    "Never ran" is not "passed". This is the same class of defect as the build
    gate's tri-state `final_ok is None`."""

    _IN_PROGRESS = {"__typename": "CheckRun", "name": "test", "status": "IN_PROGRESS",
                    "conclusion": "", "startedAt": "2026-08-26T02:40:29Z",
                    "completedAt": "", "workflowName": "CI"}
    _QUEUED = {"__typename": "CheckRun", "name": "build", "status": "QUEUED",
               "conclusion": "", "startedAt": "", "completedAt": "",
               "workflowName": "CI"}

    def test_unfinished_check_runs_block_and_are_named_NOT_RUN(self):
        blocking = ac._pr_blocking_checks(
            {"statusCheckRollup": [self._IN_PROGRESS, self._QUEUED]})
        self.assertEqual(blocking, ["test=NOT RUN (IN_PROGRESS)",
                                    "build=NOT RUN (QUEUED)"])

    def test_a_pr_whose_ci_is_still_running_is_NOT_merged(self):
        prs = [{"number": 42, "isDraft": False, "mergeable": "MERGEABLE",
                "statusCheckRollup": [self._IN_PROGRESS, self._QUEUED]}]
        merged = []

        def fake_run(cmd, cwd, timeout=ac.TIMEOUT_S):
            merged.append(cmd)
            return 0, "merged"

        orig = ac._gh_json
        ac._gh_json = lambda args, cwd, run=None: (prs, "")
        try:
            res = ac.land_open_prs("/nonexistent", repo="o/r", run=fake_run)
        finally:
            ac._gh_json = orig
        self.assertEqual(merged, [], "a merge was issued while CI was unfinished")
        self.assertEqual(res["acted_on"], [])
        self.assertIn("NOT RUN", res["skipped"][0]["reason"])
        ac.summarise([res])

    def test_a_completed_check_that_reported_no_conclusion_is_not_green(self):
        """The status guard and the conclusion guard are SEPARATE defences. A
        check GitHub marks COMPLETED while reporting no conclusion has still
        produced no verdict, and only this second guard catches it - which the
        mutation harness proved by leaving the first one intact."""
        blocking = ac._pr_blocking_checks({"statusCheckRollup": [
            {"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
             "conclusion": ""}]})
        self.assertEqual(blocking, ["test=NOT RUN (no conclusion reported)"])

    def test_a_check_run_with_neither_status_nor_conclusion_is_not_green(self):
        blocking = ac._pr_blocking_checks({"statusCheckRollup": [
            {"__typename": "CheckRun", "name": "ghost", "conclusion": ""}]})
        self.assertEqual(blocking, ["ghost=NOT RUN (no conclusion reported)"])

    def test_a_pending_status_context_blocks(self):
        blocking = ac._pr_blocking_checks({"statusCheckRollup": [
            {"__typename": "StatusContext", "context": "Vercel", "state": "PENDING"}]})
        self.assertEqual(blocking, ["Vercel=PENDING"])

    def test_a_status_context_with_no_state_is_not_silently_green(self):
        blocking = ac._pr_blocking_checks({"statusCheckRollup": [
            {"__typename": "StatusContext", "context": "CodeRabbit"}]})
        self.assertEqual(blocking, ["CodeRabbit=NOT RUN (no state reported)"])


class CiNeverRanIsNotACodeFailureTests(unittest.TestCase):
    """An account-wide GitHub Actions billing halt fails every workflow job in
    about two seconds having executed zero steps. Reported as an ordinary check
    failure it reads as "your code broke CI", which is false and sends whoever
    reads it to debug code that was never compiled.

    Measured shape: buckeye7066/GrantFlow PR #1401, 2026-08-26 - 11 FAILURE
    jobs, every one 2-3 seconds.

    Duration NEVER changes the VERDICT: a red check blocks either way. It only
    changes the reason text."""

    _START = "2026-08-26T02:40:29Z"

    @classmethod
    def _job(cls, name, conclusion, seconds):
        # Carry properly. A naive "%02d" % (29 + seconds) produced "02:40:69Z",
        # which strptime rejects, which made _check_seconds return None, which
        # silently disarmed the very assertion the fixture existed to make. The
        # mutation harness caught it; a passing test did not.
        start = _dt.datetime.strptime(cls._START, "%Y-%m-%dT%H:%M:%SZ")
        end = start + _dt.timedelta(seconds=seconds)
        return {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
                "conclusion": conclusion, "startedAt": cls._START,
                "completedAt": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "workflowName": "CI"}

    def test_the_fixture_really_produces_the_durations_it_claims(self):
        """The fixture is load-bearing: if it emits an unparseable timestamp,
        every duration reads as None and the never-ran tests pass vacuously."""
        self.assertEqual(ac._check_seconds(self._job("x", "FAILURE", 2)), 2.0)
        self.assertEqual(ac._check_seconds(self._job("x", "FAILURE", 40)), 40.0)

    def test_whole_rollup_failing_in_seconds_is_reported_as_never_ran(self):
        rollup = [self._job("test", "FAILURE", 2), self._job("lint", "FAILURE", 2)]
        blocking = ac._pr_blocking_checks({"statusCheckRollup": rollup})
        self.assertTrue(all("CI NEVER RAN" in b for b in blocking), blocking)
        self.assertTrue(all("NOT a code failure" in b for b in blocking), blocking)

    def test_never_ran_still_blocks_the_merge(self):
        prs = [{"number": 5, "isDraft": False, "mergeable": "MERGEABLE",
                "statusCheckRollup": [self._job("test", "FAILURE", 2),
                                      self._job("lint", "FAILURE", 2)]}]
        merged = []
        orig = ac._gh_json
        ac._gh_json = lambda args, cwd, run=None: (prs, "")
        try:
            res = ac.land_open_prs(
                "/nonexistent", repo="o/r",
                run=lambda cmd, cwd, timeout=ac.TIMEOUT_S: (merged.append(cmd), (0, "m"))[1])
        finally:
            ac._gh_json = orig
        self.assertEqual(merged, [], "a never-ran CI must never be merged through")
        self.assertEqual(res["acted_on"], [])

    def test_one_genuinely_fast_failure_is_still_a_real_failure(self):
        """A lint that legitimately fails in two seconds while a real test job
        fails in four minutes is NOT a billing halt. The claim needs the WHOLE
        rollup, or it slanders real red builds as infrastructure."""
        rollup = [self._job("lint", "FAILURE", 2), self._job("test", "FAILURE", 40)]
        blocking = ac._pr_blocking_checks({"statusCheckRollup": rollup})
        self.assertEqual(blocking, ["lint=FAILURE", "test=FAILURE"])

    def test_a_single_fast_failure_alone_is_not_enough_evidence(self):
        blocking = ac._pr_blocking_checks(
            {"statusCheckRollup": [self._job("lint", "FAILURE", 2)]})
        self.assertEqual(blocking, ["lint=FAILURE"])

    def test_failures_without_timestamps_are_never_called_never_ran(self):
        blocking = ac._pr_blocking_checks({"statusCheckRollup": [
            {"name": "ci", "conclusion": "FAILURE"},
            {"name": "cd", "conclusion": "FAILURE"}]})
        self.assertEqual(blocking, ["ci=FAILURE", "cd=FAILURE"])


class ReportingStepsTests(unittest.TestCase):
    def test_open_issues_are_accounted_not_dropped(self):
        orig = ac._gh_json
        ac._gh_json = lambda args, cwd, run=None: ([{"number": 3, "title": "x"},
                                                    {"number": 4, "title": "y"}], "")
        try:
            res = ac.report_open_issues("/nonexistent", repo="o/r", run=_test_runner)
        finally:
            ac._gh_json = orig
        self.assertEqual(res["candidates"], 2)
        self.assertEqual(len(res["skipped"]), 2)
        ac.summarise([res])

    def test_missing_repo_slug_is_reported_not_assumed_clean(self):
        res = ac.report_dependabot("/nonexistent", repo=None, run=_test_runner)
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
