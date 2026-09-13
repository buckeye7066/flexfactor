"""Real isolated Git regressions for owner WIP preparation and restoration."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from flexfactor_tests import ff, _unit_purpose_understanding
import flexfactor_autoclean as autoclean
import flexfactor_wip as wip
from test_flexfactor_wip import git, make_repo, _rmtree, tree_hashes


class OwnerWipPreservationTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(_rmtree, self.root)
        self.base = git(["rev-parse", "HEAD"], self.root).stdout.strip()
        self.key = os.path.normcase(os.path.abspath(self.root))
        self.addCleanup(ff._WIP_ACTIVE.pop, self.key, None)

    def dirty(self):
        root = Path(self.root)
        (root / "a.txt").write_bytes(b"owner STAGED content\n")
        git(["add", "a.txt"], self.root)
        (root / "a.txt").write_bytes(b"owner UNSTAGED content\r\n")
        (root / "owner-new.txt").write_bytes(b"owner untracked bytes\x00\xff")
        return self.state()

    def state(self):
        return (tree_hashes(self.root),
                git(["status", "--porcelain=v1", "-z", "-uall"], self.root).stdout,
                git(["ls-files", "--stage", "-z"], self.root).stdout)

    def test_partially_staged_and_untracked_bytes_round_trip(self):
        before = self.dirty()
        fingerprint = wip.porcelain_fingerprint(ff._git, self.root)
        ok, ref, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertTrue(ok)
        self.assertEqual(git(["status", "--porcelain"], self.root).stdout, "")
        self.assertEqual(git(["rev-list", "--parents", "-1", ref], self.root).stdout.split(),
                         [git(["rev-parse", ref], self.root).stdout.strip()])
        self.assertTrue(wip.restore_orphan_wip_snapshot(ff._git, self.root, ref))
        self.assertEqual(self.state(), before)
        self.assertEqual(wip.porcelain_fingerprint(ff._git, self.root), fingerprint)

    def test_intent_to_add_index_entry_is_preserved_or_refused(self):
        (Path(self.root) / "intent.txt").write_bytes(b"owner intent-to-add bytes\n")
        git(["add", "-N", "intent.txt"], self.root)
        before = self.state()
        self.assertIn(" A intent.txt", before[1])
        ok, ref, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        if ok:
            self.assertTrue(wip.restore_orphan_wip_snapshot(ff._git, self.root, ref))
        self.assertEqual(self.state(), before)

    def test_capture_failure_preserves_original_index_and_worktree(self):
        before = self.dirty()

        def fail_write_tree(args, cwd):
            if args == ["write-tree"] and "owner-new.txt" in git(["ls-files"], cwd).stdout:
                return subprocess.CompletedProcess(args, 1, "", "injected write-tree failure")
            return ff._git(args, cwd)

        ok, _, _ = wip.capture_orphan_wip_snapshot(fail_write_tree, self.root)
        self.assertFalse(ok)
        self.assertEqual(self.state(), before)

    def test_publication_refuses_when_only_staged_snapshot_enters_ancestry(self):
        (Path(self.root) / "owner-staged.txt").write_text("owner staged bytes\n", encoding="utf-8")
        git(["add", "owner-staged.txt"], self.root)
        ok, ref, secrets = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertTrue(ok)
        merged = git(["merge", "--allow-unrelated-histories", "-m", "unsafe index ancestry",
                      ref + "-index"], self.root)
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertEqual(git(["show", "HEAD:owner-staged.txt"], self.root).stdout, "owner staged bytes\n")
        allowed, reason = wip.publish_allowed(ff._git, self.root, snapshot_id=ref,
                                             branch="HEAD", secret_findings=secrets)
        self.assertFalse(allowed, reason)

    def test_restore_does_not_reverse_unrelated_verified_commit(self):
        self.dirty()
        ok, ref, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertTrue(ok)
        (Path(self.root) / "b/c.txt").write_text("verified tool fix\n", encoding="utf-8")
        git(["commit", "-am", "verified tool fix"], self.root)
        self.assertTrue(wip.restore_orphan_wip_snapshot(ff._git, self.root, ref))
        self.assertEqual((Path(self.root) / "b/c.txt").read_text(), "verified tool fix\n")
        self.assertNotIn("b/c.txt", git(["status", "--porcelain"], self.root).stdout)
        self.assertEqual(git(["show", ":a.txt"], self.root).stdout, "owner STAGED content\n")

    def test_fingerprint_detects_staged_only_byte_changes(self):
        self.dirty()
        before = wip.porcelain_fingerprint(ff._git, self.root)
        path = Path(self.root) / "a.txt"
        working = path.read_bytes()
        path.write_bytes(b"different staged bytes\n")
        git(["add", "a.txt"], self.root)
        path.write_bytes(working)
        self.assertNotEqual(wip.porcelain_fingerprint(ff._git, self.root), before)

    def _normalizing_round_trip(self, payload):
        self.dirty()
        git(["config", "core.autocrlf", "true"], self.root)
        git(["config", "core.safecrlf", "false"], self.root)
        (Path(self.root) / "a.txt").write_bytes(payload)
        before = self.state()
        ok, ref, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        if ok:
            self.assertTrue(wip.restore_orphan_wip_snapshot(ff._git, self.root, ref))
        self.assertEqual(self.state(), before, "normalization must round-trip exactly or refuse before mutation")

    def test_autocrlf_preserves_owner_lf_bytes(self):
        self._normalizing_round_trip(b"owner first\nowner second\n")

    def test_autocrlf_preserves_owner_mixed_line_endings(self):
        self._normalizing_round_trip(b"owner first\nowner second\r\n")

    def test_transforming_attributes_preserve_bytes_or_refuse_capture(self):
        (Path(self.root) / ".gitattributes").write_text("a.txt text eol=lf\n", encoding="utf-8")
        git(["add", ".gitattributes"], self.root)
        git(["commit", "-m", "attributes"], self.root)
        before = self.dirty()
        ok, ref, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        if ok:
            self.assertTrue(wip.restore_orphan_wip_snapshot(ff._git, self.root, ref))
        self.assertEqual(self.state(), before)

    def _custom_filter_fixture(self):
        root = Path(self.root)
        (root / ".gitattributes").write_text("a.txt filter=owner\n", encoding="utf-8")
        git(["add", ".gitattributes"], self.root)
        git(["commit", "-m", "filter attribute"], self.root)
        marker = root / ".git/filter-ran"
        script = root / ".git/owner-filter.py"
        script.write_text("import pathlib, sys\n"
                          "pathlib.Path(__file__).with_name('filter-ran').write_text('ran')\n"
                          "sys.stdout.buffer.write(sys.stdin.buffer.read().upper())\n", encoding="utf-8")
        command = f'"{sys.executable.replace(chr(92), "/")}" "{str(script).replace(chr(92), "/")}"'
        git(["config", "filter.owner.clean", command], self.root)
        git(["config", "filter.owner.smudge", command], self.root)
        # Same length as HEAD forces status to compare content instead of
        # recognizing a size-only dirty path without invoking the clean filter.
        (root / "a.txt").write_bytes(b"ALPHA ORIGINAL\n")
        # Status may itself use a configured clean filter. Record the fixture
        # before arming the marker observation for the capture call.
        before = self.state()
        marker.unlink(missing_ok=True)
        return marker, before

    def test_custom_filters_are_refused_before_any_filter_executes(self):
        marker, before = self._custom_filter_fixture()
        ok, _, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertFalse(marker.exists(), "snapshot preparation executed target filter code")
        self.assertFalse(ok)
        self.assertEqual(self.state(), before)

    def test_clean_tree_admission_refuses_filters_before_status_executes_them(self):
        marker, before = self._custom_filter_fixture()
        clean = ff._git_tree_clean(self.root)
        self.assertFalse(marker.exists(), "clean-tree admission executed target filter code")
        self.assertFalse(clean)
        self.assertEqual(self.state(), before)

    def test_disabling_committed_filter_cannot_hide_reset_filter_execution(self):
        marker, _ = self._custom_filter_fixture()
        (Path(self.root) / ".gitattributes").write_text("a.txt -filter\n", encoding="utf-8")
        before = self.state()
        marker.unlink(missing_ok=True)
        ok, _, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertFalse(marker.exists(), "reset executed the filter restored from HEAD attributes")
        self.assertFalse(ok)
        self.assertEqual(self.state(), before)

    def test_staged_deletion_cannot_hide_filtered_head_paths(self):
        marker, _ = self._custom_filter_fixture()
        (Path(self.root) / ".gitattributes").write_text("a.txt -filter\n", encoding="utf-8")
        git(["rm", "-f", "a.txt"], self.root)
        before = self.state()
        marker.unlink(missing_ok=True)
        ok, _, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertFalse(marker.exists(), "reset executed a filter on a path missing from the index")
        self.assertFalse(ok)
        self.assertEqual(self.state(), before)

    def test_restore_checks_snapshot_attributes_before_checkout(self):
        marker, _ = self._custom_filter_fixture()
        tree = git(["write-tree"], self.root).stdout.strip()
        snapshot = git(["commit-tree", tree, "-m", "legacy fixture snapshot"], self.root).stdout.strip()
        ref = "refs/flexfactor-wip/legacy-filter"
        git(["update-ref", ref, snapshot], self.root)
        (Path(self.root) / ".gitattributes").write_text("a.txt -filter\n", encoding="utf-8")
        git(["add", ".gitattributes"], self.root)
        git(["commit", "-m", "disable filter"], self.root)
        (Path(self.root) / "a.txt").write_bytes(b"alpha original\n")
        before = self.state()
        marker.unlink(missing_ok=True)
        restored = wip.restore_orphan_wip_snapshot(ff._git, self.root, ref)
        self.assertFalse(marker.exists(), "restore executed a filter from the snapshot tree")
        self.assertFalse(restored)
        self.assertEqual(self.state(), before)
        self.assertEqual(git(["rev-parse", ref], self.root).stdout.strip(), snapshot)

    def test_legacy_crlf_attribute_preserves_mixed_bytes_or_refuses(self):
        (Path(self.root) / ".gitattributes").write_text("a.txt crlf\n", encoding="utf-8")
        git(["add", ".gitattributes"], self.root)
        git(["commit", "-m", "legacy newline attribute"], self.root)
        self.dirty()
        (Path(self.root) / "a.txt").write_bytes(b"owner first\nowner second\r\n")
        before = self.state()
        ok, ref, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        if ok:
            self.assertTrue(wip.restore_orphan_wip_snapshot(ff._git, self.root, ref))
        self.assertEqual(self.state(), before)

    def test_uncaptured_nested_repository_is_never_recursively_removed(self):
        nested = Path(self.root) / "nested"
        nested.mkdir()
        git(["init", "-b", "main"], str(nested))
        git(["config", "user.email", "nested@example.invalid"], str(nested))
        git(["config", "user.name", "Nested Fixture"], str(nested))
        (nested / "owner.txt").write_bytes(b"committed inner work\n")
        git(["add", "owner.txt"], str(nested))
        git(["commit", "-m", "inner base"], str(nested))
        (nested / "owner.txt").write_bytes(b"inner owner WIP\n")
        before = self.state()
        nested_before = {str(p.relative_to(nested)): p.read_bytes()
                         for p in nested.rglob("*") if p.is_file()}
        ok, _, _ = wip.capture_orphan_wip_snapshot(ff._git, self.root)
        self.assertFalse(ok, "a gitlink cannot preserve the nested owner's dirty bytes")
        self.assertEqual(self.state(), before)
        self.assertEqual({str(p.relative_to(nested)): p.read_bytes()
                          for p in nested.rglob("*") if p.is_file()}, nested_before)

    def _audit_preparation(self, outcome):
        if outcome in ("conflict", "resolved-conflict"):
            git(["checkout", "-b", "other"], self.root)
            (Path(self.root) / "a.txt").write_text("other branch\n", encoding="utf-8")
            git(["commit", "-am", "other"], self.root)
            git(["checkout", "main"], self.root)
            (Path(self.root) / "a.txt").write_text("main branch\n", encoding="utf-8")
            git(["commit", "-am", "main"], self.root)
            self.assertNotEqual(git(["merge", "other"], self.root).returncode, 0)
            if outcome == "resolved-conflict":
                (Path(self.root) / "a.txt").write_text("owner merge resolution\n", encoding="utf-8")
                git(["add", "a.txt"], self.root)
            self.base = git(["rev-parse", "HEAD"], self.root).stdout.strip()
            before = self.state()
        else:
            before = self.dirty()
        remote = tempfile.mkdtemp(prefix="ffwip-origin-")
        self.addCleanup(_rmtree, remote)
        git(["init", "--bare", "-b", "main"], remote)
        git(["remote", "add", "origin", remote], self.root)
        git(["push", "origin", "main"], self.root)
        stack = dict(is_node=False, is_python=False, framework=None, scripts={},
                     verify_cmds=[], fast_verify=None, test_cmd=None, full_suite_cmd=None,
                     dev_script=None, is_web=False, esbuild=None, config_refused=False)
        args = SimpleNamespace(max_cost=1, max_files=0, include=[], exclude=[],
                               push=True, merge=True, auto_clean=True, bootstrap=True,
                               cycles=1, apply=True, allow_dirty=False, trust_repo=False)
        during = {}

        def prepare(*args, **kwargs):
            during["state"] = self.state()
            during["refs"] = git(["for-each-ref", "--format=%(refname)", wip.WIP_REF_PREFIX], self.root).stdout
            # Keep the real autoclean candidate commit decision live, while
            # replacing only hosted PR/API operations with an isolated local push.
            def local_runner(cmd, cwd, timeout=None):
                cp = ff._git(cmd[1:], cwd)
                return cp.returncode, cp.stdout + cp.stderr
            step = autoclean.commit_pending_changes(self.root, run=local_runner,
                                                   verify=lambda: (True, "fixture gate"))
            git(["push", "origin", "main"], self.root)
            if outcome == "exception":
                raise RuntimeError("injected cleanup exception")
            return autoclean.summarise([step])

        def stop_after_preparation(*args, **kwargs):
            raise RuntimeError("injected post-preparation failure" if outcome == "failure"
                               else "isolated preparation boundary")

        stubs = {
            "resolve_program_input": lambda arg: ("fixture", ""),
            "resolve_project_dir": lambda *a: self.root,
            "_acquire_audit_lock": lambda *a: "fixture-lock",
            "_release_audit_lock": lambda *a: None,
            "_load_brain": lambda: {}, "_clean_map": lambda *a: {},
            "_runstate_module": lambda: None, "_evidence_module": lambda: None,
            "_detect_stack": lambda *a: stack,
            "build_audit_providers": lambda *a: [("fixture", SimpleNamespace(model="offline"))],
            "_model_args_for_repository": lambda *a: SimpleNamespace(_source_classification={}),
            "_ensure_program_understanding": lambda *a, **kw: _unit_purpose_understanding(),
            "_gather_from_folder": lambda *a: ("fixture", ""),
            "load_purpose_contract": lambda *a: None,
            "_run_bootstrap_phase": stop_after_preparation,
            "_attach_ledger_suggester": lambda *a: None,
            "_PROGRESS": SimpleNamespace(update=lambda *a, **kw: None),
            "ConsoleMeter": mock.MagicMock,
        }
        with mock.patch.multiple(ff, **stubs), mock.patch.object(autoclean, "clean_repo", prepare), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = ff.audit_one_program(self.root, args, 1, 1, 0)
        if outcome in ("conflict", "resolved-conflict"):
            self.assertEqual(self.state(), before, "a failed capture must not abort owner conflict resolution")
            self.assertNotIn("state", during, "autoclean must not run on uncaptured owner WIP")
            self.assertIn("snapshot", result.get("error", ""))
            return
        self.assertIn("state", during, result)
        self.assertEqual(during["state"][1], "", "autoclean must see a HEAD-clean worktree")
        self.assertIn(wip.WIP_REF_PREFIX, during["refs"])
        self.assertEqual(git(["rev-parse", "refs/heads/main"], remote).stdout.strip(), self.base)
        self.assertNotIn("owner ", git(["log", "-p", "origin/main"], self.root).stdout)
        self.assertEqual(self.state(), before)
        self.assertIn("restored", result.get("wip_restore", ""))

    def test_autoclean_sees_orphan_snapshot_before_clean_preparation(self):
        self._audit_preparation("clean")

    def test_failed_run_cannot_publish_owner_wip(self):
        self._audit_preparation("failure")

    def test_autoclean_exception_cannot_publish_owner_wip(self):
        self._audit_preparation("exception")

    def test_conflicted_index_refuses_preparation_without_changing_owner_state(self):
        self._audit_preparation("conflict")

    def test_staged_merge_resolution_is_not_discarded_by_preparation(self):
        self._audit_preparation("resolved-conflict")


if __name__ == "__main__":
    unittest.main(verbosity=2)
