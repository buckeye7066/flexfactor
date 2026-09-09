"""Hermetic source update tests using local Git repositories, never live GitHub."""
import os
import io
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import tempfile
import unittest

from source_app_update import SourceUpdater, UpdateError, prompt


class FixtureUpdater(SourceUpdater):
    def trusted_source(self):
        return self.git("remote", "get-url", "origin")


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.remote = self.base / "remote"
        self.client = self.base / "client"
        self.remote.mkdir()
        self.run_git(self.remote, "init", "-b", "main")
        self.run_git(self.remote, "config", "user.email", "fixture@example.invalid")
        self.run_git(self.remote, "config", "user.name", "Update fixture")
        (self.remote / "app.py").write_text("VERSION = 1\n")
        (self.remote / ".gitignore").write_text(".env\ndata/\n")
        self.commit("installed")
        self.run_git(self.base, "clone", str(self.remote), str(self.client))
        self.updater = FixtureUpdater(self.client, "buckeye7066/example")
        self.original = self.run_git(self.client, "rev-parse", "HEAD")

    def tearDown(self):
        # Windows Git marks object files read-only.
        import shutil
        def writable(action, path, exc):
            os.chmod(path, 0o700)
            action(path)
        shutil.rmtree(self.base, onerror=writable)
        self.tmp.cleanup()

    @staticmethod
    def run_git(root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args],
                                       stderr=subprocess.STDOUT, text=True).strip()

    def commit(self, message):
        self.run_git(self.remote, "add", ".")
        self.run_git(self.remote, "commit", "-m", message)
        return self.run_git(self.remote, "rev-parse", "HEAD")

    def newer(self):
        (self.remote / "app.py").write_text("VERSION = 2\n")
        return self.commit("new version")

    def test_check_downloads_metadata_without_changing_installed_code(self):
        latest = self.newer()
        self.assertEqual(self.updater.check()["latest"], latest)
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), self.original)
        self.assertEqual((self.client / "app.py").read_text(), "VERSION = 1\n")

    def test_explicit_update_installs_pinned_revision_and_preserves_data(self):
        latest = self.newer()
        (self.client / ".env").write_text("personal secret")
        (self.client / "notes.txt").write_text("personal notes")
        state = self.updater.apply(latest)
        self.assertEqual(state["status"], "updated")
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), latest)
        self.assertEqual((self.client / "app.py").read_text(), "VERSION = 2\n")
        self.assertEqual((self.client / ".env").read_text(), "personal secret")
        self.assertEqual((self.client / "notes.txt").read_text(), "personal notes")

    def test_dirty_source_is_preserved(self):
        latest = self.newer()
        (self.client / "app.py").write_text("personal changes")
        with self.assertRaises(UpdateError):
            self.updater.apply(latest)
        self.assertEqual((self.client / "app.py").read_text(), "personal changes")
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), self.original)

    def test_ignored_file_collision_fails_without_losing_data(self):
        (self.client / ".env").write_text("personal secret")
        (self.remote / ".env").write_text("upstream replacement")
        self.run_git(self.remote, "add", "-f", ".env")
        latest = self.commit("colliding file")
        with self.assertRaises(UpdateError):
            self.updater.apply(latest)
        self.assertEqual((self.client / ".env").read_text(), "personal secret")
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), self.original)

    def test_newer_release_after_notice_requires_fresh_choice(self):
        first = self.newer()
        (self.remote / "app.py").write_text("VERSION = 3\n")
        self.commit("another version")
        with self.assertRaises(UpdateError):
            self.updater.apply(first)
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), self.original)

    def test_untrusted_origin_is_rejected(self):
        with self.assertRaises(UpdateError):
            SourceUpdater(self.client, "buckeye7066/example").check()

    def test_url_rewrites_are_rejected(self):
        canonical = "https://github.com/buckeye7066/example.git"
        self.run_git(self.client, "remote", "set-url", "origin", canonical)
        self.run_git(self.client, "config", "url.https://example.invalid/.insteadOf", "https://github.com/")
        with self.assertRaises(UpdateError):
            SourceUpdater(self.client, "buckeye7066/example").check()

    def test_development_branch_is_preserved(self):
        latest = self.newer()
        self.run_git(self.client, "checkout", "-b", "my-work")
        with self.assertRaises(UpdateError):
            self.updater.apply(latest)
        self.assertEqual(self.run_git(self.client, "branch", "--show-current"), "my-work")

    def test_update_lock_prevents_concurrent_install(self):
        latest = self.newer()
        lock = self.client / ".git" / "source-app-update.lock"
        lock.write_text("another updater")
        with self.assertRaises(UpdateError):
            self.updater.apply(latest)
        self.assertTrue(lock.exists())
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), self.original)

    def test_unavailable_check_keeps_installed_app_usable(self):
        updater = SourceUpdater(self.client, "buckeye7066/example")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(prompt(updater, "Fixture"), 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_suffixless_trusted_ssh_origins_remain_supported(self):
        for url in ("git@github.com:buckeye7066/example", "ssh://git@github.com/buckeye7066/example"):
            self.run_git(self.client, "remote", "set-url", "origin", url)
            self.assertEqual(SourceUpdater(self.client, "buckeye7066/example").trusted_source(), url)

    def test_manual_flexfactor_apply_records_dependency_work(self):
        latest = self.newer()
        self.updater.repo = "buckeye7066/flexfactor"
        self.updater.apply(latest)
        marker = self.client / ".git" / "flexfactor-refresh-needs-install"
        self.assertEqual(marker.read_text(), latest)

    def test_dependency_marker_failure_preserves_installed_source(self):
        latest = self.newer()
        self.updater.repo = "buckeye7066/flexfactor"
        (self.client / ".git" / "flexfactor-refresh-needs-install").mkdir()
        with self.assertRaises(UpdateError):
            self.updater.apply(latest)
        self.assertEqual(self.run_git(self.client, "rev-parse", "HEAD"), self.original)


if __name__ == "__main__":
    unittest.main()
