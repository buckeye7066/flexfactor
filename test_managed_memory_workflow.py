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


if __name__ == "__main__":
    unittest.main(verbosity=2)
