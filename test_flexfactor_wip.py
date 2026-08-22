"""Adversarial tests for flexfactor_wip (orphan WIP snapshot).

Run:  python test_flexfactor_wip.py

Every test builds a REAL throwaway git repo under tempfile. Nothing here
touches the FlexFactor repo itself or ~/.flexfactor.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import flexfactor_wip as wip  # noqa: E402


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _rmtree(path):
    def _onexc(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=_onexc)


def _write(root, rel, data):
    full = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kw = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": "\n"}
    with open(full, mode, **kw) as fh:
        fh.write(data)
    return full


def tree_hashes(root) -> dict:
    """rel path -> sha256 of bytes (or symlink target) for EVERY file except .git."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                out[rel] = "symlink:" + os.readlink(full)
            else:
                with open(full, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def make_repo() -> str:
    d = tempfile.mkdtemp(prefix="ffwip-")
    r = git(["init", "-b", "main"], d)
    assert r.returncode == 0, r.stderr
    git(["config", "user.name", "FlexFactor Test"], d)
    git(["config", "user.email", "test@example.invalid"], d)
    git(["config", "core.autocrlf", "false"], d)
    _write(d, "a.txt", "alpha original\n")
    _write(d, "b/c.txt", "charlie original\n")
    _write(d, "gone.txt", "to be deleted by the WIP\n")
    _write(d, ".gitignore", "ignored.log\n")
    git(["add", "-A"], d)
    r = git(["commit", "-q", "-m", "base"], d)
    assert r.returncode == 0, r.stderr
    return d


def status(d) -> str:
    return git(["-c", "core.quotePath=false", "status", "--porcelain", "-uall"], d).stdout


def head(d) -> str:
    return git(["rev-parse", "HEAD"], d).stdout.strip()


def show_refs(d) -> str:
    return git(["show-ref"], d).stdout


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self.d = make_repo()

    def tearDown(self):
        _rmtree(self.d)

    def _capture_and_assert_clean(self):
        before = tree_hashes(self.d)
        ok, ref, secrets = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertTrue(ref.startswith(wip.WIP_REF_PREFIX), ref)
        self.assertIn(ref, show_refs(self.d))
        # Worktree returned to HEAD: nothing dirty.
        self.assertEqual(status(self.d).strip(), "")
        return before, ref, secrets

    def _restore_and_assert_identical(self, before, ref):
        self.assertTrue(wip.restore_orphan_wip_snapshot(git, self.d, ref))
        after = tree_hashes(self.d)
        self.assertEqual(before, after)


class ModifiedTrackedFilesTests(_RepoCase):
    def test_1_modified_tracked_files_round_trip_byte_for_byte(self):
        _write(self.d, "a.txt", "alpha MODIFIED by owner\r\n")  # CRLF stays bytes
        _write(self.d, "b/c.txt", b"\x00\x01binary-ish\xff")
        fp_before = wip.porcelain_fingerprint(git, self.d)
        self.assertIn(" M a.txt", status(self.d))
        head_before = head(self.d)
        before, ref, secrets = self._capture_and_assert_clean()
        self.assertEqual(secrets, [])
        self.assertEqual(head(self.d), head_before, "capture must not move HEAD")
        with open(os.path.join(self.d, "a.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"alpha original\n", "worktree must be at HEAD")
        self._restore_and_assert_identical(before, ref)
        self.assertIn(" M a.txt", status(self.d))
        self.assertEqual(wip.porcelain_fingerprint(git, self.d), fp_before)


class UntrackedFilesTests(_RepoCase):
    def test_2_untracked_nested_files_round_trip(self):
        _write(self.d, "new/deep/er/file.txt", "untracked nested\n")
        _write(self.d, "new/top.txt", "untracked top\n")
        _write(self.d, "ignored.log", "ignored - NOT captured, survives in place\n")
        before = tree_hashes(self.d)
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertEqual(status(self.d).strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.d, "new")))
        # Documented contract: .gitignore'd files are neither captured nor removed.
        self.assertTrue(os.path.exists(os.path.join(self.d, "ignored.log")))
        ls = git(["ls-tree", "-r", "--name-only", ref], self.d).stdout
        self.assertNotIn("ignored.log", ls)
        self.assertIn("new/deep/er/file.txt", ls)
        self._restore_and_assert_identical(before, ref)
        self.assertIn("?? new/deep/er/file.txt", status(self.d))


class DeletedTrackedFileTests(_RepoCase):
    def test_3_tracked_file_deleted_in_wip_is_deleted_again_after_restore(self):
        os.unlink(os.path.join(self.d, "gone.txt"))
        self.assertIn(" D gone.txt", status(self.d))
        before = tree_hashes(self.d)
        self.assertNotIn("gone.txt", before)
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(self.d, "gone.txt")),
                        "after capture the tree is at HEAD, so the file exists again")
        self.assertTrue(wip.restore_orphan_wip_snapshot(git, self.d, ref))
        self.assertFalse(os.path.exists(os.path.join(self.d, "gone.txt")),
                         "restore must re-apply the WIP deletion")
        self.assertIn(" D gone.txt", status(self.d))
        self.assertEqual(before, tree_hashes(self.d))


class RenamedFileTests(_RepoCase):
    def test_4_git_mv_rename_round_trips(self):
        r = git(["mv", "a.txt", "renamed.txt"], self.d)
        self.assertEqual(r.returncode, 0, r.stderr)
        before = tree_hashes(self.d)
        self.assertIn("renamed.txt", before)
        self.assertNotIn("a.txt", before)
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(self.d, "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "renamed.txt")))
        self._restore_and_assert_identical(before, ref)
        st = status(self.d)
        # Restore yields an UNSTAGED dirty state (documented): old name deleted,
        # new name untracked. Bytes are identical; only staging differs.
        self.assertIn(" D a.txt", st)
        self.assertIn("?? renamed.txt", st)


class SymlinkTests(_RepoCase):
    def test_5_symlink_round_trips(self):
        link = os.path.join(self.d, "link.txt")
        try:
            os.symlink("a.txt", link)
        except (OSError, NotImplementedError) as ex:
            self.skipTest(f"BLOCKED: os.symlink not permitted on this host "
                          f"(no SeCreateSymbolicLinkPrivilege / dev mode): {ex}")
        git(["config", "core.symlinks", "true"], self.d)
        before = tree_hashes(self.d)
        self.assertEqual(before["link.txt"], "symlink:a.txt")
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertFalse(os.path.lexists(link))
        mode = git(["ls-tree", ref, "link.txt"], self.d).stdout
        self.assertTrue(mode.startswith("120000"), f"stored as symlink: {mode!r}")
        self._restore_and_assert_identical(before, ref)
        self.assertTrue(os.path.islink(link))


class SecretScanTests(_RepoCase):
    SECRET = ("config = {\n"
              "  'aws': 'AKIAABCDEFGHIJKLMNOP',\n"
              "}\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
              "-----END RSA PRIVATE KEY-----\n")

    def test_6_secret_bearing_dirty_file_blocks_publish(self):
        _write(self.d, "a.txt", self.SECRET)
        before = tree_hashes(self.d)
        ok, ref, secrets = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertTrue(secrets, "secret findings must be non-empty")
        self.assertEqual({f["path"] for f in secrets}, {"a.txt"})
        self.assertIn(secrets[0]["category"], ("private_key", "aws_key"))
        allowed, reason = wip.publish_allowed(
            git, self.d, snapshot_id=ref, branch="main", secret_findings=secrets)
        self.assertFalse(allowed)
        self.assertIn("secret", reason.lower())
        # Still restorable, bytes intact.
        self._restore_and_assert_identical(before, ref)

    def test_6b_secret_in_unicode_named_file_is_not_silently_skipped(self):
        # Regression: ls-tree without -z octal-quotes this name, `git show`
        # then fails, and the scan returned [] for a file holding a key.
        _write(self.d, "sécrets 日本.py", self.SECRET)
        ok, ref, secrets = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertEqual([f["path"] for f in secrets], ["sécrets 日本.py"])
        self.assertTrue(wip.restore_orphan_wip_snapshot(git, self.d, ref))


class UnicodeAndSpacesTests(_RepoCase):
    NAME = "my file ünïcode 日本.txt"

    def test_7_filenames_with_spaces_and_unicode_round_trip(self):
        _write(self.d, self.NAME, "ünïcode content 日本\n")
        _write(self.d, "dir with space/nested ü.txt", "nested\n")
        _write(self.d, "a.txt", "modified ü\n")
        fp_before = wip.porcelain_fingerprint(git, self.d)
        before = tree_hashes(self.d)
        self.assertIn(self.NAME, before)
        ok, ref, secrets = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertEqual(secrets, [])
        self.assertFalse(os.path.exists(os.path.join(self.d, self.NAME)))
        self._restore_and_assert_identical(before, ref)
        self.assertEqual(wip.porcelain_fingerprint(git, self.d), fp_before)

    def test_7b_unicode_deletion_is_reapplied(self):
        _write(self.d, self.NAME, "tracked unicode\n")
        git(["add", "-A"], self.d)
        git(["commit", "-q", "-m", "add unicode"], self.d)
        os.unlink(os.path.join(self.d, self.NAME))
        before = tree_hashes(self.d)
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(self.d, self.NAME)))
        self.assertTrue(wip.restore_orphan_wip_snapshot(git, self.d, ref))
        self.assertFalse(os.path.exists(os.path.join(self.d, self.NAME)))
        self.assertEqual(before, tree_hashes(self.d))


class OrphanProofTests(_RepoCase):
    def test_8_snapshot_is_orphan_until_someone_merges_it(self):
        # Only an ADDED file so the later merge is conflict-free by construction.
        _write(self.d, "wip.txt", "owner wip\n")
        ok, ref, secrets = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        sha = wip.resolve_snapshot_sha(git, self.d, ref)
        self.assertTrue(sha)
        parents = git(["rev-list", "--parents", "-n", "1", sha], self.d).stdout.split()
        self.assertEqual(parents, [sha], "snapshot commit must have NO parent")
        self.assertIs(wip.snapshot_is_ancestor_of(git, self.d, ref, "HEAD"), False)
        self.assertIs(wip.snapshot_is_ancestor_of(git, self.d, sha, "main"), False)

        # FlexFactor makes a new commit on main.
        _write(self.d, "tool.txt", "flexfactor fix\n")
        git(["add", "-A"], self.d)
        self.assertEqual(git(["commit", "-q", "-m", "flexfactor: fix"], self.d).returncode, 0)
        self.assertIs(wip.snapshot_is_ancestor_of(git, self.d, ref, "HEAD"), False)
        allowed, reason = wip.publish_allowed(
            git, self.d, snapshot_id=ref, branch="main", secret_findings=secrets)
        self.assertTrue(allowed, reason)

        # Someone merges the snapshot into main.
        m = git(["merge", "--allow-unrelated-histories", "--no-edit", sha], self.d)
        self.assertEqual(m.returncode, 0, m.stderr + m.stdout)
        self.assertIs(wip.snapshot_is_ancestor_of(git, self.d, ref, "main"), True)
        allowed, reason = wip.publish_allowed(
            git, self.d, snapshot_id=ref, branch="main", secret_findings=[])
        self.assertFalse(allowed)
        self.assertIn("ancestor", reason)

    def test_8b_no_snapshot_attached_is_allowed(self):
        allowed, _ = wip.publish_allowed(git, self.d, snapshot_id=None, branch="main")
        self.assertTrue(allowed)

    def test_8c_no_branch_fails_closed(self):
        _write(self.d, "a.txt", "x\n")
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        allowed, reason = wip.publish_allowed(git, self.d, snapshot_id=ref, branch=None)
        self.assertFalse(allowed)
        self.assertTrue(reason)


class CrashDuringCaptureTests(_RepoCase):
    def test_9_commit_tree_failure_loses_nothing(self):
        _write(self.d, "a.txt", "owner edit\n")
        _write(self.d, "untracked.txt", "owner new\n")
        before = tree_hashes(self.d)
        st_before = status(self.d)

        def failing(args, cwd):
            if args and args[0] == "commit-tree":
                return subprocess.CompletedProcess(args, 1, "", "simulated crash")
            return git(args, cwd)

        self.assertEqual(wip.capture_orphan_wip_snapshot(failing, self.d),
                         (False, None, []))
        self.assertEqual(tree_hashes(self.d), before, "original bytes must be intact")
        self.assertEqual(status(self.d), st_before, "worktree must still be dirty")
        self.assertNotIn(wip.WIP_REF_PREFIX, show_refs(self.d))

    def test_9b_reset_failure_after_ref_written_reports_not_ok_but_keeps_ref(self):
        _write(self.d, "a.txt", "owner edit\n")

        def failing(args, cwd):
            if args[:2] == ["reset", "--hard"]:
                return subprocess.CompletedProcess(args, 1, "", "simulated crash")
            return git(args, cwd)

        ok, ref, _ = wip.capture_orphan_wip_snapshot(failing, self.d)
        self.assertFalse(ok)
        self.assertIsNotNone(ref, "ref must be returned so the work is recoverable")
        self.assertIn(ref, show_refs(self.d))


class CrashDuringRestoreTests(_RepoCase):
    def test_10_restore_failure_keeps_the_ref(self):
        _write(self.d, "a.txt", "owner edit\n")
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)

        def failing(args, cwd):
            if args and args[0] in ("checkout", "read-tree"):
                return subprocess.CompletedProcess(args, 128, "", "simulated crash")
            return git(args, cwd)

        self.assertFalse(wip.restore_orphan_wip_snapshot(failing, self.d, ref))
        self.assertIn(ref, show_refs(self.d), "ref must survive a failed restore")
        # And a later, healthy restore still recovers the bytes.
        self.assertTrue(wip.restore_orphan_wip_snapshot(git, self.d, ref))
        with open(os.path.join(self.d, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "owner edit\n")

    def test_10b_unresolvable_snapshot_restores_nothing(self):
        self.assertFalse(wip.restore_orphan_wip_snapshot(
            git, self.d, wip.WIP_REF_PREFIX + "doesnotexist"))


class SeparationUnknownTests(_RepoCase):
    def test_11_publish_fails_closed_when_ancestry_cannot_be_determined(self):
        _write(self.d, "a.txt", "owner edit\n")
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)

        def broken(args, cwd):
            if args and args[0] == "merge-base":
                return subprocess.CompletedProcess(args, 128, "", "fatal: simulated")
            return git(args, cwd)

        self.assertIsNone(wip.snapshot_is_ancestor_of(broken, self.d, ref, "main"))
        allowed, reason = wip.publish_allowed(broken, self.d, snapshot_id=ref, branch="main")
        self.assertFalse(allowed)
        self.assertIn("could not prove", reason)


class DropRefTests(_RepoCase):
    def test_12_drop_only_deletes_refs_under_the_prefix(self):
        _write(self.d, "a.txt", "owner edit\n")
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        for bad in ("refs/heads/main", "main", "HEAD", "", None,
                    "refs/flexfactor-wipx/abc", "refs/tags/v1"):
            self.assertFalse(wip.drop_wip_ref(git, self.d, bad), bad)
        self.assertIn("refs/heads/main", show_refs(self.d))
        self.assertEqual(git(["branch", "--list"], self.d).stdout.strip(), "* main")
        self.assertTrue(wip.drop_wip_ref(git, self.d, ref))
        self.assertNotIn(ref, show_refs(self.d))


class NeverABranchTests(_RepoCase):
    def test_13_snapshot_ref_is_never_a_branch(self):
        branches_before = git(["branch", "--list"], self.d).stdout
        _write(self.d, "a.txt", "owner edit\n")
        ok, ref, _ = wip.capture_orphan_wip_snapshot(git, self.d)
        self.assertTrue(ok)
        self.assertEqual(git(["branch", "--list"], self.d).stdout, branches_before)
        self.assertEqual(git(["branch", "--list", "--all"], self.d).stdout, branches_before)
        self.assertNotIn("refs/heads/", ref)
        self.assertIn(ref, show_refs(self.d))
        self.assertTrue(wip.is_wip_snapshot_ref(ref))
        self.assertFalse(wip.is_wip_snapshot_ref("refs/heads/flexfactor-wip/x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
