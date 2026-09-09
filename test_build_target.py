"""Offline dispatcher behavior checks: no app/model/network execution."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("build_target", ROOT / "scripts/build-target.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)
CONFIG = json.loads((ROOT / "scripts/build-targets.json").read_text())


class BuildTargetTests(unittest.TestCase):
    def test_real_manifest_all_hosts_and_targets(self):
        for host, native in BUILD.HOSTS.items():
            for requested in BUILD.TARGETS:
                target = native if requested == "auto" else requested
                with self.subTest(host=host, requested=requested):
                    entry = CONFIG["targets"].get(target)
                    if entry and host in entry["hosts"]:
                        result = BUILD.plan(CONFIG, host, requested)
                        self.assertEqual(result["target"], target)
                        self.assertEqual(result["commands"], entry["commands"])
                    else:
                        with self.assertRaises(ValueError):
                            BUILD.plan(CONFIG, host, requested)

    def test_host_injection_never_allowed_for_real_execution(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                BUILD.options(["--host", sys.platform])
        self.assertEqual(failure.exception.code, 2)

    def test_bad_arguments_fail(self):
        for argv in (["--target"], ["--target", "made-up"], ["--unknown"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    BUILD.options(argv)
        with self.assertRaises(ValueError):
            BUILD.plan(CONFIG, "other-os", "auto")

    def test_dry_run_never_executes_even_supported_command(self):
        fixture = {"name": "fixture", "guidance": "", "targets": {
            "windows": {"hosts": ["win32"], "commands": [["never-run"]],
                        "format": "fixture", "output": "fixture"}}}
        with patch.object(BUILD.Path, "read_text", return_value=json.dumps(fixture)):
            with patch.object(BUILD, "execute") as execute:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    result = BUILD.main(["--host", "win32", "--dry-run"])
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output.getvalue())["target"], "windows")
                execute.assert_not_called()

    def test_command_failure_stops_before_next_step(self):
        runner = Mock(side_effect=subprocess.CalledProcessError(7, "builder"))
        with self.assertRaises(subprocess.CalledProcessError):
            BUILD.execute({"commands": [["python", "first"], ["python", "second"]]},
                          root=ROOT, runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], [sys.executable, "first"])
        self.assertEqual(runner.call_args.kwargs["cwd"], ROOT)

    def test_unsupported_cli_fails_before_executor(self):
        with patch.object(BUILD, "execute") as execute:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(BUILD.main(["--target", "ios"]), 1)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
