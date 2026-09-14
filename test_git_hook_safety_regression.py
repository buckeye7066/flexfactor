"""Real-repository regressions for Git hooks on the installed runtime paths.

Hooks only write a constant marker; no hook reads or exports credentials.
All Git configuration and repositories belong to disposable fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from flexfactor_tests import ff
import flexfactor_autoclean as autoclean
import flexfactor_directed as directed


@unittest.skipUnless(shutil.which("git"), "Git is required for the real hook regression")
class GitHookSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ff-hook-safety-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "target"
        self.repo.mkdir()
        # Never expose real credentials to even these harmless fixture hooks.
        env, _ = ff._ff_sandbox.scrub_env(dict(os.environ))
        env = {k: v for k, v in env.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_CEILING_DIRECTORIES=str(self.root))
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.raw("init", "-q", "-b", "main")
        self.raw("config", "user.name", "Hook Test")
        self.raw("config", "user.email", "hook@example.invalid")
        self.raw("config", "core.autocrlf", "false")
        (self.repo / "source.txt").write_text("baseline\n", encoding="utf-8")
        self.raw("add", "source.txt")
        self.raw("commit", "-qm", "baseline")
        directed.install(vars(ff))

    def raw(self, *args, cwd=None, check=True):
        cp = subprocess.run(["git", *args], cwd=cwd or self.repo,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        if check:
            self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp

    def hook(self, name, *, directory=None, reject=False):
        directory = directory or self.repo / ".git" / "hooks"
        directory.mkdir(parents=True, exist_ok=True)
        hook = directory / name
        marker = self.root / (name + ".marker")
        quoted = "'" + marker.as_posix().replace("'", "'\\''") + "'"
        hook.write_text("#!/bin/sh\nprintf 'hook ran\\n' > " + quoted
                        + ("\nexit 1\n" if reject else "\nexit 0\n"),
                        encoding="utf-8", newline="\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return marker

    def test_fixture_hook_really_runs_without_the_protected_runner(self):
        marker = self.hook("pre-commit")
        self.raw("commit", "--allow-empty", "-qm", "fixture control")
        self.assertTrue(marker.exists(), "the hook fixture did not execute")

    def test_git_commit_disables_repository_hooks(self):
        marker = self.hook("pre-commit")
        cp = ff._git(["commit", "--allow-empty", "-qm", "candidate"], str(self.repo))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(marker.exists(), "target hook escaped the broker via _git")

    def test_full_argv_checkout_disables_configured_target_hook_directory(self):
        hooks = self.repo / "target-hooks"
        marker = self.hook("post-checkout", directory=hooks)
        self.raw("config", "core.hooksPath", str(hooks))
        cp = ff._git_argv(["git", "checkout", "-b", "candidate"], str(self.repo))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(marker.exists(), "configured post-checkout hook executed")
        self.assertEqual(self.raw("config", "core.hooksPath").stdout.strip(), str(hooks))

    def test_autoclean_uses_the_protected_runner_for_its_real_commit(self):
        marker = self.hook("post-commit")
        (self.repo / "source.txt").write_text("candidate\n", encoding="utf-8")
        result = autoclean.commit_pending_changes(
            str(self.repo), run=ff._brokered_tuple_runner, verify=lambda: True)
        self.assertTrue(result["acted_on"], result)
        self.assertFalse(marker.exists(), "autoclean executed a target commit hook")

    def test_inherited_and_explicit_hook_config_cannot_reenable_hooks(self):
        hooks = self.repo / "target-hooks"
        marker = self.hook("pre-commit", directory=hooks)
        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks),
        }):
            cp = ff._run(["git", "-C", str(self.repo), "-c",
                          "core.hooksPath=" + str(hooks), "commit", "--allow-empty",
                          "-qm", "candidate"], str(self.root))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(marker.exists(), "per-command hook policy was overridden")

    def test_fsmonitor_hook_cannot_execute_during_status(self):
        hooks = self.repo / ".git" / "hooks"
        marker = self.hook("fsmonitor")
        self.raw("config", "core.fsmonitor", (hooks / "fsmonitor").as_posix())
        self.raw("status", "--porcelain")
        self.assertTrue(marker.exists(), "the fsmonitor fixture did not execute")
        marker.unlink()
        cp = ff._git(["status", "--porcelain"], str(self.repo))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(marker.exists(), "status executed a target fsmonitor hook")

    def test_push_preserves_remote_rejection_and_does_not_run_local_pre_push(self):
        remote = self.root / "remote.git"
        self.raw("init", "--bare", "-q", "-b", "main", str(remote))
        self.raw("remote", "add", "origin", str(remote))
        cp = ff._git(["push", "origin", "main"], str(self.repo))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        baseline = self.raw("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
        local_marker = self.hook("pre-push")
        remote_marker = self.hook("pre-receive", directory=remote / "hooks", reject=True)
        self.raw("commit", "--allow-empty", "-qm", "candidate")
        cp = ff._git(["push", "origin", "main"], str(self.repo))
        self.assertNotEqual(cp.returncode, 0, "remote rejection was bypassed")
        self.assertTrue(remote_marker.exists(), "the remote protection did not run")
        self.assertEqual(self.raw("rev-parse", "refs/heads/main", cwd=remote).stdout.strip(),
                         baseline, "the rejected candidate reached the remote branch")
        self.assertFalse(local_marker.exists(), "push executed a local target hook")

    def test_existing_transport_restriction_still_blocks_local_clone(self):
        cp = ff._run(["git", "-c", "protocol.allow=never", "-c",
                      "protocol.https.allow=always", "clone", "--no-checkout",
                      str(self.repo), str(self.root / "forbidden-clone")], str(self.root))
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("transport 'file' not allowed", cp.stderr)

    def test_transport_auth_config_is_preserved_without_contacting_a_remote(self):
        # This invented header checks config forwarding, never a host credential.
        setting = "http.https://example.invalid/.extraHeader"
        value = "Authorization: Bearer fixture-only-not-a-credential"
        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": setting,
            "GIT_CONFIG_VALUE_0": value,
        }):
            cp = ff._git(["config", "--get", setting], str(self.repo))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), value)

    def test_force_push_is_still_refused_before_launch(self):
        cp = ff._git(["push", "--force", "origin", "main"], str(self.repo))
        self.assertEqual(cp.returncode, 126)
        self.assertTrue(getattr(cp, "flexfactor_policy_blocked", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
