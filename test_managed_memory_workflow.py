"""Offline checks for managed-runner memory delegation; no host provisioning."""
import importlib.util
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / ".github/scripts/managed_memory.py"


class ManagedMemoryWorkflowTests(unittest.TestCase):
    def helper(self):
        spec = importlib.util.spec_from_file_location("managed_memory_test", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_job_path_rejects_untrusted_or_ambiguous_components(self):
        helper = self.helper()
        self.assertEqual(helper.group_path("123", "2", "tests"),
                         Path("/sys/fs/cgroup/flexfactor-memory-123-2-tests"))
        for args in (("../1", "2", "tests"), ("1", "2", "../tests"),
                     ("1", "", "tests"), ("1", "2", "test/x")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                helper.group_path(*args)

    def test_setup_refuses_disabled_parent_without_creating_group(self):
        helper = self.helper()
        with mock.patch.object(helper, "verify_cgroup_mount"), \
             mock.patch.object(Path, "read_text", return_value="pids cpu"), \
             mock.patch.object(Path, "mkdir") as mkdir:
            with self.assertRaisesRegex(RuntimeError, "already enabled"):
                helper.setup(Path("/sys/fs/cgroup/flexfactor-memory-1-1-tests"),
                             Path("unused.json"), 1000, 1000)
        mkdir.assert_not_called()

    def test_receipt_validates_and_reads_one_nofollow_descriptor(self):
        helper = self.helper()
        import io
        with mock.patch.object(helper.os, "O_NOFOLLOW", 0x20000, create=True), \
             mock.patch.object(helper.os, "open", return_value=71) as opened, \
             mock.patch.object(helper.os, "fstat", return_value=SimpleNamespace(st_mode=0o100644, st_uid=0)) as checked, \
             mock.patch.object(helper.os, "fdopen", return_value=io.StringIO('{"path":"verified"}')) as stream, \
             mock.patch.object(helper.os, "close") as closed:
            self.assertEqual(helper.read_receipt(Path("receipt")), {"path": "verified"})
        self.assertTrue(opened.call_args.args[1] & 0x20000)
        checked.assert_called_once_with(71)
        self.assertEqual(stream.call_args.args[0], 71)
        closed.assert_called_once_with(71)

    def test_receipt_rejects_unprivileged_owner_before_reading_json(self):
        helper = self.helper()
        with mock.patch.object(helper.os, "O_NOFOLLOW", 0, create=True), \
             mock.patch.object(helper.os, "open", return_value=71), \
             mock.patch.object(helper.os, "fstat", return_value=SimpleNamespace(st_mode=0o100644, st_uid=1000)), \
             mock.patch.object(helper.os, "fdopen") as stream, \
             mock.patch.object(helper.os, "close") as closed:
            with self.assertRaisesRegex(RuntimeError, "root-owned"):
                helper.read_receipt(Path("receipt"))
        stream.assert_not_called()
        closed.assert_called_once_with(71)

    def test_initial_receipt_failure_removes_only_new_empty_root(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "own-root"
            receipt = Path(directory) / "missing-parent" / "receipt.json"
            with mock.patch.object(helper, "verify_cgroup_mount"), \
                 mock.patch.object(Path, "read_text", return_value="memory"):
                with self.assertRaises(FileNotFoundError):
                    helper.setup(root, receipt, 1000, 1000)
            self.assertFalse(root.exists(), "failed receipt creation must not strand an unrecorded cgroup")
            self.assertTrue(Path(directory).is_dir())

    def test_cleanup_refuses_receipt_for_another_group(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            receipt.write_text('{"path":"/sys/fs/cgroup"}', encoding="utf-8")
            with mock.patch.object(helper, "verify_cgroup_mount"), \
                 mock.patch.object(helper, "read_receipt", return_value={"path": "/sys/fs/cgroup"}), \
                 mock.patch.object(helper.os, "open") as opened:
                with self.assertRaisesRegex(RuntimeError, "receipt"):
                    helper.cleanup(Path("/sys/fs/cgroup/flexfactor-memory-1-1-tests"), receipt)
                opened.assert_not_called()

    def test_cleanup_refuses_replaced_subtree_before_killing_anything(self):
        helper = self.helper()
        root = Path("/sys/fs/cgroup/flexfactor-memory-1-1-tests")
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            import json
            receipt.write_text(json.dumps({"path": str(root), "device": 2, "inode": 3}), encoding="utf-8")
            with mock.patch.object(helper, "verify_cgroup_mount"), \
                 mock.patch.object(helper, "read_receipt", return_value={"path": str(root), "device": 2, "inode": 3}), \
                 mock.patch.multiple(helper.os, O_DIRECTORY=0, O_NOFOLLOW=0, create=True), \
                 mock.patch.object(helper.os, "open", side_effect=[10, 11]) as opened, \
                 mock.patch.object(helper.os, "fstat", return_value=SimpleNamespace(st_dev=2, st_ino=4)), \
                 mock.patch.object(helper.os, "close") as closed:
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    helper.cleanup(root, receipt)
                self.assertEqual(opened.call_count, 2, "must not open cgroup.kill after identity mismatch")
                self.assertEqual(closed.call_args_list, [mock.call(11), mock.call(10)])
            self.assertTrue(receipt.exists())

    def test_probe_refuses_rlimit_fallback_even_if_target_exits_zero(self):
        helper = self.helper()
        sandbox = mock.Mock()
        sandbox.capability_report.return_value = {"strongest": "bwrap"}
        sandbox.run_contained.return_value = SimpleNamespace(
            returncode=0, stdout="MEMORY_CONTAINED", stderr="",
            flexfactor_containment={"level": {"memory_mechanism": "rlimit-as"}})
        with mock.patch.dict(sys.modules, {"flexfactor_sandbox": sandbox}), \
             mock.patch.object(sys, "path", sys.path.copy()):
            with self.assertRaisesRegex(RuntimeError, "physical-memory probe failed"):
                helper.probe()

    def test_probe_refuses_missing_bubblewrap_before_running_target(self):
        helper = self.helper()
        sandbox = mock.Mock()
        sandbox.capability_report.return_value = {"strongest": "process-group"}
        with mock.patch.dict(sys.modules, {"flexfactor_sandbox": sandbox}), \
             mock.patch.object(sys, "path", sys.path.copy()):
            with self.assertRaisesRegex(RuntimeError, "bubblewrap"):
                helper.probe()
        sandbox.run_contained.assert_not_called()

    def test_mobile_uses_existing_owner_billing_policy(self):
        source = (ROOT / ".github/workflows/mobile-run.yml").read_text(encoding="utf-8")
        self.assertIn("FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY: ${{ vars.FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY || '0' }}", source)

    def test_both_managed_workflows_provision_enter_and_always_clean_up(self):
        for file in ("mobile-run.yml", "production-readiness.yml"):
            source = (ROOT / ".github/workflows" / file).read_text(encoding="utf-8")
            with self.subTest(file=file):
                self.assertIn("apt-get install --yes bubblewrap", source)
                self.assertIn("managed_bwrap_profile.py ensure", source)
                self.assertLess(source.index("managed_bwrap_profile.py ensure"), source.index("managed_memory.py setup"))
                self.assertIn("managed_memory.py setup", source)
                self.assertIn("managed_memory.py probe", source)
                self.assertIn("FLEXFACTOR_MEMORY_CGROUP_ROOT", source)
                self.assertIn('echo "$$" | sudo tee', source)
                self.assertIn(".supervisor/cgroup.procs", source)
                self.assertIn("managed_memory.py cleanup", source)
                self.assertNotIn("unprivileged_userns_clone", source)
                self.assertNotIn("apparmor_restrict_unprivileged_userns", source)
                cleanup = source.split("- name: Clean up managed memory delegation", 1)[1]
                self.assertIn("if: always()", cleanup.split("run:", 1)[0])
        release = (ROOT / ".github/workflows/production-readiness.yml").read_text(encoding="utf-8")
        self.assertIn("FLEXFACTOR_TEST_PWSH", release)
        self.assertIn("test_managed_memory_workflow.py", release)


class ManagedBubblewrapProfileTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("managed_bwrap_test", ROOT / ".github/scripts/managed_bwrap_profile.py")
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        self.bad = SimpleNamespace(returncode=1, stdout="", stderr="operation not permitted")

    def test_successful_probe_never_invokes_sudo_or_installs_policy(self):
        h = self.helper
        with mock.patch.object(h.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(h, "run", return_value=self.ok) as run, \
             mock.patch.object(h, "apparmor_active") as active:
            h.ensure()
        run.assert_called_once_with(h.PROBE)
        active.assert_not_called()

    def test_failed_probe_without_apparmor_refuses_changes(self):
        h = self.helper
        with mock.patch.object(h.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(h, "run", return_value=self.bad) as run, \
             mock.patch.object(h, "apparmor_active", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "without active AppArmor"):
                h.ensure()
        run.assert_called_once_with(h.PROBE)

    def test_existing_loaded_profile_is_not_replaced(self):
        h = self.helper
        with mock.patch.object(h, "loaded_profiles", return_value={"bwrap": "enforce)"}), \
             mock.patch.object(h, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                h.reject_conflicts()
        run.assert_not_called()

    def test_existing_vendor_definition_is_not_replaced(self):
        h = self.helper
        names = SimpleNamespace(returncode=0, stdout="unpriv_bwrap\n", stderr="")
        with mock.patch.object(h, "loaded_profiles", return_value={}), \
             mock.patch.object(Path, "iterdir", return_value=[Path("/etc/apparmor.d/vendor")]), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(h, "run", return_value=names) as run:
            with self.assertRaisesRegex(RuntimeError, "existing bubblewrap policy"):
                h.reject_conflicts()
        self.assertEqual(run.call_args.args[0][1], "--names")

    def test_install_adds_only_packaged_profile_and_checks_enforcement(self):
        h = self.helper
        names = SimpleNamespace(returncode=0, stdout="bwrap\nunpriv_bwrap\n", stderr="")
        with mock.patch.object(h.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(h, "apparmor_active", return_value=True), \
             mock.patch.object(h, "reject_conflicts") as conflicts, \
             mock.patch.object(h, "run", side_effect=[self.bad, self.ok, names, self.ok]) as run, \
             mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(h, "loaded_profiles", return_value={"bwrap": "enforce)", "unpriv_bwrap": "enforce)"}):
            h.install(1000, 1000)
        self.assertEqual(conflicts.call_count, 2)
        self.assertEqual(run.call_args_list[0], mock.call(h.PROBE, user=1000, group=1000, extra_groups=[]))
        self.assertEqual(run.call_args_list[-1], mock.call([h.PARSER, "--add", str(h.PROFILE)]))

    def test_loaded_profiles_must_both_be_enforced(self):
        h = self.helper
        names = SimpleNamespace(returncode=0, stdout="bwrap\nunpriv_bwrap\n", stderr="")
        with mock.patch.object(h.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(h, "apparmor_active", return_value=True), \
             mock.patch.object(h, "reject_conflicts"), \
             mock.patch.object(h, "run", side_effect=[self.bad, self.ok, names, self.ok]), \
             mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(h, "loaded_profiles", return_value={"bwrap": "enforce)", "unpriv_bwrap": "complain)"}):
            with self.assertRaisesRegex(RuntimeError, "both load in enforce"):
                h.install(1000, 1000)

    def test_ordinary_user_probe_must_pass_after_installation(self):
        h = self.helper
        with mock.patch.object(h.os, "geteuid", return_value=1000, create=True), \
             mock.patch.object(h.os, "getuid", return_value=1000, create=True), \
             mock.patch.object(h.os, "getgid", return_value=1000, create=True), \
             mock.patch.object(h, "apparmor_active", return_value=True), \
             mock.patch.object(h, "run", side_effect=[self.bad, self.ok, self.bad]) as run:
            with self.assertRaisesRegex(RuntimeError, "still fails"):
                h.ensure()
        self.assertEqual(run.call_args_list[-1], mock.call(h.PROBE))
        self.assertEqual(run.call_args_list[1].args[0][0], "sudo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
