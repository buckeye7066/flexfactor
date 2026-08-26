#!/usr/bin/env python3
"""`_remove_captured_untracked` must never be able to delete a repository.

THE INCIDENT. On 2026-08-24 every FILE inside C:/Users/firer/flexfactor/.git was
removed twice during live audit runs (01:57 and 05:12), while the .git DIRECTORY
itself survived both times. That is exactly the shape of the removal walk in
flexfactor_wip: for a directory target it unlinks every file, rmdir's every
subdirectory bottom-up, and then rmdir's the directory itself - and that final
rmdir fails if any process still holds a handle, which leaves precisely an empty
.git behind.

The CAUSE was never proven, and these tests do not claim it. What they pin is
the invariant that makes the mechanism impossible either way:

    a tool whose entire job is to PRESERVE and RESTORE the owner's work must
    never be capable of deleting the repository it is operating on.

The paths reaching that function are sliced out of `git status --porcelain`
TEXT (`line[3:]`), so a malformed line, an unexpected porcelain shape, or git
having run against the wrong repository all arrive looking like ordinary
relative paths. The guard costs nothing in a correct run - git never reports
`.git` as untracked - so it only ever fires when something has already gone
wrong, which is what a safety invariant is for.

REGRESSION PROOF: `PreFixBehaviourTests` loads the committed PRE-FIX fixture and
shows it deletes `.git/HEAD` outright. The fixture is part of the repository so
the proof runs identically on developer machines and CI.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest

import flexfactor_wip as wip

_PREFIX_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "eval_fixtures", "wip_pre_fix.py")


def _make_repo(root: str) -> None:
    """A project dir with a real-looking .git and one genuine untracked file."""
    os.makedirs(os.path.join(root, ".git", "refs", "heads"), exist_ok=True)
    os.makedirs(os.path.join(root, ".git", "objects"), exist_ok=True)
    for rel, body in ((os.path.join(".git", "HEAD"), "ref: refs/heads/main\n"),
                      (os.path.join(".git", "config"), "[core]\n"),
                      (os.path.join(".git", "index"), "x"),
                      (os.path.join(".git", "refs", "heads", "main"), "abc123\n"),
                      ("scratch.txt", "genuinely untracked\n")):
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write(body)


class RefusalReasonTests(unittest.TestCase):
    """The predicate, in isolation."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wipguard-")
        _make_repo(self.root)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_git_directory_is_refused(self):
        self.assertIn("git directory", wip.refuse_removal_reason(self.root, ".git"))

    def test_anything_inside_a_git_directory_is_refused(self):
        for rel in (".git/HEAD", ".git/config", ".git/refs/heads/main",
                    ".git\\HEAD", "sub/.git/HEAD"):
            self.assertTrue(wip.refuse_removal_reason(self.root, rel),
                            f"{rel!r} must be refused")

    def test_the_check_is_case_insensitive(self):
        """NTFS is case-insensitive; '.GIT' is the same directory."""
        self.assertTrue(wip.refuse_removal_reason(self.root, ".GIT/HEAD"))

    def test_paths_escaping_the_project_are_refused(self):
        for rel in ("../outside.txt", "a/../../outside.txt"):
            self.assertTrue(wip.refuse_removal_reason(self.root, rel),
                            f"{rel!r} must be refused")

    def test_absolute_paths_are_refused_on_EVERY_platform(self):
        r"""Rooted on either platform's rules, refused on both.

        This test asserted `C:\Windows\System32` unconditionally while running
        only on Windows. Wired into CI on 2026-08-25 it failed on ubuntu, and
        the failure was real: `os.path.isabs` / `os.path.splitdrive` answer for
        the HOST, so on Linux a drive-letter path was an ordinary relative
        filename and sailed through a containment guard. A rule whose answer
        depends on the host is not one rule.
        """
        for rel in (r"C:\Windows\System32", "D:/x", "/etc/passwd",
                    "\\\\server\\share", "\\rooted"):
            self.assertTrue(wip.refuse_removal_reason(self.root, rel),
                            f"{rel!r} must be refused on every platform")

    def test_the_rooted_check_does_not_consult_the_host(self):
        """Prove the OTHER platform's answer from this one.

        The test above exercises whichever branch this host happens to take, so
        on Windows it would stay green even if the cross-platform branch were
        deleted. `_is_rooted_anywhere` is pure string logic with no os.path in
        it, which is exactly why it can be checked here and hold there.
        """
        for rel in (r"C:\Windows\System32", "D:/x", "/etc/passwd",
                    "\\\\server\\share", "\\rooted", "/"):
            self.assertTrue(wip._is_rooted_anywhere(rel), repr(rel))
        for rel in ("scratch.txt", "sub/dir/file.txt", "a:b.txt", "C:relative",
                    "..", "x\\y"):
            self.assertFalse(wip._is_rooted_anywhere(rel),
                             f"{rel!r} is a legitimate relative path")

    def test_the_project_root_itself_is_refused(self):
        for rel in (".", "./", ""):
            self.assertTrue(wip.refuse_removal_reason(self.root, rel),
                            f"{rel!r} must be refused")

    def test_a_genuine_untracked_file_is_ALLOWED(self):
        """The guard must not break the function's actual job."""
        self.assertEqual(wip.refuse_removal_reason(self.root, "scratch.txt"), "")
        self.assertEqual(wip.refuse_removal_reason(self.root, "sub/dir/file.txt"), "")


class RemovalBehaviourTests(unittest.TestCase):
    """The real function, against a real directory tree."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wipguard-")
        _make_repo(self.root)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_the_2026_08_24_wipe_cannot_happen(self):
        """Hand it exactly what would empty a .git and assert nothing is lost."""
        targets = [".git", ".git/HEAD", ".git/config", ".git/index",
                   ".git/refs/heads/main"]
        failed = wip._remove_captured_untracked(self.root, targets)
        for rel in (os.path.join(".git", "HEAD"), os.path.join(".git", "config"),
                    os.path.join(".git", "index"),
                    os.path.join(".git", "refs", "heads", "main")):
            self.assertTrue(os.path.exists(os.path.join(self.root, rel)),
                            f"{rel} was deleted - the repository is destroyed")
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".git")))
        self.assertEqual(sorted(failed), sorted(targets),
                         "every refusal must be REPORTED, never silently skipped")

    def test_a_refusal_makes_the_caller_fail_closed(self):
        """Non-empty `failed` is what keeps the WIP ref and stops the run from
        treating the worktree as clean - the refusal must reach that path."""
        failed = wip._remove_captured_untracked(self.root, [".git/HEAD"])
        self.assertTrue(failed)

    def test_a_genuine_untracked_file_is_still_removed(self):
        """Guarding must not turn the function into a no-op."""
        failed = wip._remove_captured_untracked(self.root, ["scratch.txt"])
        self.assertEqual(failed, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "scratch.txt")))

    def test_a_mixed_batch_removes_the_safe_one_and_refuses_the_rest(self):
        failed = wip._remove_captured_untracked(self.root, ["scratch.txt", ".git/HEAD"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "scratch.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, ".git", "HEAD")))
        self.assertEqual(failed, [".git/HEAD"])


class PreFixBehaviourTests(unittest.TestCase):
    """Proof the guard is load-bearing: the PRE-FIX module really did delete."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(_PREFIX_COPY):
            raise AssertionError(f"required pre-fix fixture missing: {_PREFIX_COPY}")
        spec = importlib.util.spec_from_file_location("wip_prefix", _PREFIX_COPY)
        cls.old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.old)

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wipprefix-")
        _make_repo(self.root)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_the_old_code_deletes_git_HEAD(self):
        self.assertFalse(hasattr(self.old, "refuse_removal_reason"),
                         "pre-fix copy unexpectedly already has the guard")
        self.old._remove_captured_untracked(self.root, [".git/HEAD"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git", "HEAD")),
                         "expected the PRE-FIX code to delete it; if this fails "
                         "the guard may not be load-bearing after all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
