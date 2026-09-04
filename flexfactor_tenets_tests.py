"""Hermetic unit tests for FlexFactor's optional Tenets adapter."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import flexfactor_tenets as ft


class TenetsContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "README.md").write_text("demo\n", encoding="utf-8")
        self.state = Path(self.temp.name) / "state"
        self.state_patch = mock.patch.dict(
            os.environ,
            {"FLEXFACTOR_STATE_DIR": str(self.state)},
            clear=False,
        )
        self.state_patch.start()
        self.version_patch = mock.patch.object(ft, "_tenets_distribution_version", return_value=ft.TENETS_VERSION)
        self.version_patch.start()
        with ft._RESULT_CACHE_LOCK:
            ft._RESULT_CACHE.clear()

    def tearDown(self) -> None:
        self.version_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _process(
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        timed_out: bool = False,
        overflow_stream: str | None = None,
        read_error: str | None = None,
    ) -> ft._BoundedProcessResult:
        return ft._BoundedProcessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            overflow_stream=overflow_stream,
            read_error=read_error,
        )

    def test_missing_tenets_is_fail_open_and_writes_external_evidence(self) -> None:
        with mock.patch.object(ft, "_find_tenets_executable", return_value=None):
            result = ft.generate_tenets_context(self.root, "review launch path")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.files, ())
        output = Path(result.output_path)
        self.assertTrue(output.is_relative_to(self.state.resolve()))
        self.assertFalse(output.is_relative_to(self.root))
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["tool"], "tenets")
        self.assertEqual(payload["expected_version"], "0.13.3")
        self.assertEqual(payload["adapter_version"], 2)

    def test_unreadable_distribution_metadata_is_treated_as_unavailable(self) -> None:
        failures = (
            PermissionError("metadata is unreadable"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid metadata"),
            ValueError("malformed metadata"),
            RuntimeError("unexpected metadata backend failure"),
        )
        self.version_patch.stop()
        try:
            for failure in failures:
                with self.subTest(failure=type(failure).__name__), mock.patch.object(
                    ft.importlib_metadata, "version", side_effect=failure
                ):
                    self.assertIsNone(ft._tenets_distribution_version())
        finally:
            self.version_patch.start()

    def test_trusted_interpreter_install_resolution_ignores_ambient_path(self) -> None:
        scripts = Path(self.temp.name) / "venv" / "Scripts"
        scripts.mkdir(parents=True)
        interpreter = scripts / "python.exe"
        interpreter.write_bytes(b"")
        tenets = scripts / "tenets.exe"
        tenets.write_bytes(b"")
        with mock.patch.object(ft.sys, "prefix", str(Path(self.temp.name) / "other-prefix")):
            found = ft._find_tenets_executable(
                interpreter=interpreter, platform_name="nt", path_value=str(self.root)
            )
        self.assertEqual(Path(found or ""), tenets.resolve())

    def test_ambient_path_tenets_is_never_executed(self) -> None:
        fake = self.root / ("tenets.exe" if os.name == "nt" else "tenets")
        fake.write_bytes(b"MZ" if os.name == "nt" else b"x")
        if os.name != "nt":
            fake.chmod(0o755)
        with mock.patch.object(ft.sys, "prefix", str(Path(self.temp.name) / "empty-prefix")):
            found = ft._find_tenets_executable(
                interpreter=Path(self.temp.name) / "python", path_value=str(self.root)
            )
        self.assertIsNone(found)

    def test_success_keeps_only_safe_unique_repository_paths(self) -> None:
        outside = self.root.parent / "secret.txt"
        payload = {
            "files": [
                {"path": "src/app.py", "score": 0.91},
                {"path": str(self.root / "README.md"), "relevance_score": "0.8"},
                {"path": "src/app.py", "score": 0.7},
                {"path": "../secret.txt", "score": 1.0},
                {"path": str(outside), "score": 1.0},
            ]
        }
        completed = self._process(stdout=json.dumps(payload).encode())
        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/usr/bin/tenets"
        ), mock.patch.object(ft, "_run_bounded_process", return_value=completed) as runner:
            result = ft.generate_tenets_context(self.root, "fix auth", top=10)
        self.assertEqual(result.status, "ok")
        self.assertEqual([item.path for item in result.files], ["src/app.py", "README.md"])
        self.assertEqual(result.files[0].score, 0.91)
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ("/usr/bin/tenets", "rank", "fix auth"))
        self.assertFalse(runner.call_args.kwargs["cwd"].is_relative_to(self.root.resolve()))
        self.assertIn("env", runner.call_args.kwargs)
        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 120.0)

    def test_ranked_entries_must_resolve_to_existing_regular_files(self) -> None:
        payload = {
            "files": [
                {"path": "src"},
                {"path": "src/missing.py"},
                {"path": "src/app.py"},
            ]
        }
        completed = self._process(stdout=json.dumps(payload).encode())
        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/usr/bin/tenets"
        ), mock.patch.object(ft, "_run_bounded_process", return_value=completed):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "ok")
        self.assertEqual([item.path for item in result.files], ["src/app.py"])

    def test_selected_console_script_must_be_owned_by_validated_distribution(self) -> None:
        executable = Path(self.temp.name) / "trusted" / "tenets"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        entry_point = types.SimpleNamespace(group="console_scripts", name="tenets")

        class Distribution:
            metadata = {"Name": "tenets"}
            entry_points = (entry_point,)
            files = ("../../../bin/tenets",)
            version = ft.TENETS_VERSION

            def __init__(self, owned_path: Path):
                self.owned_path = owned_path

            def locate_file(self, _record):
                return self.owned_path

        self.version_patch.stop()
        try:
            with mock.patch.object(
                ft, "_is_trusted_tenets_executable", return_value=True
            ), mock.patch.object(
                ft, "_trusted_tenets_metadata_directories",
                return_value=(Path(self.temp.name) / "site-packages",),
            ), mock.patch.object(
                ft.importlib_metadata,
                "distributions",
                return_value=[Distribution(executable)],
            ):
                self.assertEqual(
                    ft._tenets_distribution_version(executable), ft.TENETS_VERSION
                )

            forged_alias = executable.parent / "other-tenets"
            with mock.patch.object(
                ft, "_is_trusted_tenets_executable", return_value=True
            ), mock.patch.object(
                ft, "_trusted_tenets_metadata_directories",
                return_value=(Path(self.temp.name) / "site-packages",),
            ), mock.patch.object(
                ft.importlib_metadata,
                "distributions",
                return_value=[Distribution(forged_alias)],
            ):
                self.assertIsNone(ft._tenets_distribution_version(executable))
        finally:
            self.version_patch.start()

    def test_real_cli_json_uses_bounded_stdout_without_a_temp_output_file(self) -> None:
        payload = {"files": [{"path": "src/app.py", "score": 0.95}]}

        def runner(command, **_kwargs):
            self.assertNotIn("--output", command)
            return self._process(stdout=json.dumps(payload).encode())

        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/usr/bin/tenets"
        ), mock.patch.object(ft, "_run_bounded_process", side_effect=runner):
            result = ft.generate_tenets_context(self.root, "audit")

        self.assertEqual(result.status, "ok")
        self.assertEqual([item.path for item in result.files], ["src/app.py"])

    def test_non_utf8_bounded_json_stdout_is_degraded(self) -> None:
        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/usr/bin/tenets"
        ), mock.patch.object(
            ft, "_run_bounded_process", return_value=self._process(stdout=b"\xff\xfe")
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertIn("unusable output", result.message)

    def test_empty_valid_result_is_degraded_not_success(self) -> None:
        completed = self._process(stdout=b'{"files": []}')
        with mock.patch.object(ft, "_find_tenets_executable", return_value="tenets"), mock.patch.object(
            ft, "_run_bounded_process", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")

    def test_nonzero_exit_is_fail_open_and_message_is_bounded(self) -> None:
        completed = self._process(returncode=3, stderr=b"failure " * 50_000)
        with mock.patch.object(ft, "_find_tenets_executable", return_value="tenets"), mock.patch.object(
            ft, "_run_bounded_process", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertLessEqual(len(result.message), 2000)

    def test_timeout_is_fail_open(self) -> None:
        completed = self._process(returncode=-9, timed_out=True)
        with mock.patch.object(ft, "_find_tenets_executable", return_value="tenets"), mock.patch.object(
            ft, "_run_bounded_process", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit", timeout_seconds=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("timeout", result.message)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux supervisor lifecycle contract")
    def test_interrupt_cleans_ranker_before_propagating(self) -> None:
        survivor_file = Path(self.temp.name) / "interrupted-ranker-survived"
        script = (
            "import pathlib,sys,time; time.sleep(0.8); "
            "pathlib.Path(sys.argv[1]).write_text('alive'); time.sleep(60)"
        )
        with mock.patch.object(
            ft.threading.Event, "wait", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                ft._run_bounded_process(
                    (sys.executable, "-S", "-c", script, str(survivor_file)),
                    cwd=self.root,
                    timeout_seconds=10,
                )
        time.sleep(1)
        self.assertFalse(survivor_file.exists())

    def test_oversized_output_is_terminated_and_bounded_for_both_streams(self) -> None:
        scripts = {
            "stdout": "import sys; sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush()",
            "stderr": "import sys; sys.stderr.buffer.write(b'x' * 4096); sys.stderr.flush()",
        }
        for stream, script in scripts.items():
            with self.subTest(stream=stream):
                result = ft._run_bounded_process(
                    (sys.executable, "-S", "-c", script),
                    cwd=self.root,
                    timeout_seconds=10,
                    stdout_limit=128,
                    stderr_limit=128,
                )
                self.assertEqual(result.overflow_stream, stream)
                self.assertFalse(result.timed_out)
                self.assertLessEqual(len(result.stdout), 128)
                self.assertLessEqual(len(result.stderr), 128)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux supervisor lifecycle contract")
    def test_termination_signals_the_unreaped_supervisor_not_its_numeric_group(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 424242
        process.wait.side_effect = subprocess.TimeoutExpired("tenets", 2)

        with mock.patch.object(ft.os, "killpg") as kill_group, \
             mock.patch.object(ft, "_posix_process_group_alive", return_value=True), \
             mock.patch.object(ft.time, "monotonic", side_effect=[0.0, 4.0]):
            ft._terminate_process_tree(process)

        kill_group.assert_not_called()
        process.send_signal.assert_called_once_with(ft.signal.SIGTERM)
        process.terminate.assert_not_called()
        process.kill.assert_called_once_with()

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux supervisor lifecycle contract")
    def test_reaped_supervisor_is_never_signalled_by_pid_or_process_group(self) -> None:
        process = mock.Mock(pid=424242)
        process.poll.return_value = 0
        with mock.patch.object(ft.os, "killpg") as kill_group:
            ft._terminate_process_tree(process)
        kill_group.assert_not_called()
        process.send_signal.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux descendant lifecycle contract")
    def test_descendant_is_killed_when_the_direct_ranker_exits_zero(self) -> None:
        survivor_file = Path(self.temp.name) / "descendant-survived"
        script = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(1); pathlib.Path(sys.argv[1]).write_text(\"alive\"); time.sleep(60)',"
            "sys.argv[1]])"
        )
        started = time.monotonic()

        result = ft._run_bounded_process(
            (sys.executable, "-S", "-c", script, str(survivor_file)),
            cwd=self.root,
            timeout_seconds=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.read_error)
        self.assertLess(time.monotonic() - started, 4)
        time.sleep(1.2)
        self.assertFalse(survivor_file.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux child-subreaper containment contract")
    def test_setsid_descendant_cannot_escape_after_ranker_exits_zero(self) -> None:
        survivor_file = Path(self.temp.name) / "setsid-descendant-survived"
        script = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(1); pathlib.Path(sys.argv[1]).write_text(\"alive\"); time.sleep(60)',"
            "sys.argv[1]],start_new_session=True)"
        )
        started = time.monotonic()

        result = ft._run_bounded_process(
            (sys.executable, "-S", "-c", script, str(survivor_file)),
            cwd=self.root,
            timeout_seconds=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.read_error)
        self.assertLess(time.monotonic() - started, 4)
        time.sleep(1.2)
        self.assertFalse(survivor_file.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux child-subreaper containment contract")
    def test_timeout_cleans_ranker_and_helper_that_both_escape_session(self) -> None:
        survivor_file = Path(self.temp.name) / "timeout-setsid-survived"
        script = (
            "import os,signal,subprocess,sys,time; "
            "os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text(\"alive\"); time.sleep(60)',"
            "sys.argv[1]],start_new_session=True); time.sleep(60)"
        )
        started = time.monotonic()

        result = ft._run_bounded_process(
            (sys.executable, "-S", "-c", script, str(survivor_file)),
            cwd=self.root,
            timeout_seconds=0.2,
        )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.read_error)
        self.assertLess(time.monotonic() - started, 4)
        time.sleep(1.2)
        self.assertFalse(survivor_file.exists())

    def test_other_posix_platform_fails_before_launch_without_containment(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows has Job Object containment")
        with mock.patch.object(ft.sys, "platform", "darwin"), mock.patch.object(
            ft.subprocess, "Popen"
        ) as popen:
            with self.assertRaisesRegex(OSError, "containment is unavailable"):
                ft._run_bounded_process(
                    (sys.executable, "-c", "print('must not run')"),
                    cwd=self.root,
                    timeout_seconds=10,
                )
        popen.assert_not_called()

    def test_windows_job_cleanup_survives_an_exited_direct_parent(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        with mock.patch.object(ft.os, "name", "nt"), mock.patch.object(
            ft, "_terminate_windows_kill_job"
        ) as terminate_job:
            ft._terminate_process_tree(process, windows_job=9123)
        terminate_job.assert_called_once_with(9123)

    def test_windows_process_is_contained_before_it_is_resumed(self) -> None:
        process = mock.Mock(pid=417)
        order: list[str] = []
        with mock.patch.object(
            ft, "_create_windows_kill_job",
            side_effect=lambda _process: order.append("contain") or 9123,
        ), mock.patch.object(
            ft, "_resume_windows_process",
            side_effect=lambda _pid: order.append("resume"),
        ):
            job = ft._contain_and_resume_windows_process(process)
        self.assertEqual(job, 9123)
        self.assertEqual(order, ["contain", "resume"])

    def test_windows_resume_failure_closes_the_containment_job(self) -> None:
        process = mock.Mock(pid=417)
        with mock.patch.object(
            ft, "_create_windows_kill_job", return_value=9123
        ), mock.patch.object(
            ft, "_resume_windows_process", side_effect=OSError("resume failed")
        ), mock.patch.object(ft, "_terminate_windows_kill_job") as terminate_job:
            with self.assertRaisesRegex(OSError, "resume failed"):
                ft._contain_and_resume_windows_process(process)
        terminate_job.assert_called_once_with(9123)

    def test_windows_job_policy_caps_aggregate_memory_and_process_count(self) -> None:
        flags, process_limit, memory_limit = ft._windows_job_limit_policy()
        self.assertEqual(flags & 0x00000008, 0x00000008)
        self.assertEqual(flags & 0x00000200, 0x00000200)
        self.assertEqual(flags & 0x00002000, 0x00002000)
        self.assertEqual(process_limit, ft.MAX_RANKER_PROCESSES)
        self.assertEqual(memory_limit, ft.MAX_RANKER_MEMORY_BYTES)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux resource-limit contract")
    def test_linux_supervisor_applies_memory_process_and_file_limits(self) -> None:
        fake_resource = types.SimpleNamespace(
            RLIMIT_AS=1,
            RLIMIT_NPROC=2,
            RLIMIT_FSIZE=3,
            RLIM_INFINITY=-1,
            getrlimit=mock.Mock(return_value=(-1, -1)),
            setrlimit=mock.Mock(),
        )
        with mock.patch.dict(sys.modules, {"resource": fake_resource}):
            ft._apply_linux_ranker_resource_limits()
        self.assertEqual(fake_resource.setrlimit.call_args_list, [
            mock.call(1, (ft.MAX_RANKER_MEMORY_BYTES, ft.MAX_RANKER_MEMORY_BYTES)),
            mock.call(2, (ft.MAX_RANKER_PROCESSES, ft.MAX_RANKER_PROCESSES)),
            mock.call(3, (ft.MAX_STDOUT_BYTES, ft.MAX_STDOUT_BYTES)),
        ])

    def test_ranker_never_runs_from_or_resolves_helpers_through_target(self) -> None:
        target_bin = self.root / "tools"
        target_bin.mkdir()
        completed = self._process(stdout=b'{"files":["src/app.py"]}')
        with mock.patch.object(ft, "_find_tenets_executable", return_value="/trusted/tenets"), \
             mock.patch.object(ft, "_run_bounded_process", return_value=completed) as runner, \
             mock.patch.dict(os.environ, {
                 "PATH": os.pathsep.join((str(target_bin), "/safe/bin")),
                 "PYTHONPATH": str(self.root),
                 "PYTHONHOME": str(self.root / "python-home"),
                 "OPENAI_API_KEY": "must-not-reach-ranker",
                 "GITHUB_TOKEN": "must-not-reach-ranker",
             }):
            result = ft.generate_tenets_context(self.root, "audit")

        self.assertEqual(result.status, "ok")
        call = runner.call_args
        self.assertFalse(Path(call.kwargs["cwd"]).is_relative_to(self.root))
        child_env = call.kwargs["env"]
        self.assertEqual(child_env["PATH"], "")
        self.assertFalse(Path(child_env["HOME"]).is_relative_to(self.root))
        self.assertNotIn("PYTHONPATH", child_env)
        self.assertNotIn("PYTHONHOME", child_env)
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("GITHUB_TOKEN", child_env)
        self.assertEqual(child_env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(child_env["PYTHONSAFEPATH"], "1")
        self.assertEqual(child_env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(child_env["GIT_TERMINAL_PROMPT"], "0")
        disabled_git = Path(child_env["GIT_PYTHON_GIT_EXECUTABLE"])
        self.assertTrue(disabled_git.is_relative_to(call.kwargs["cwd"]))
        self.assertFalse(disabled_git.exists())

    def test_repo_named_symlink_or_junction_never_survives_helper_lookup(self) -> None:
        outside = Path(self.temp.name) / "outside-bin"
        outside.mkdir()
        target_link = self.root / "linked-bin"
        try:
            target_link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable")
        isolation = Path(self.temp.name) / "isolation"
        isolation.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(target_link)}, clear=False):
            environment = ft._isolated_tenets_environment(self.root.resolve(), isolation)
        self.assertEqual(environment["PATH"], "")

    def test_generator_reports_stream_limit_without_parsing_partial_json(self) -> None:
        completed = self._process(
            returncode=-15,
            stdout=b"{" * 10,
            overflow_stream="stdout",
        )
        with mock.patch.object(ft, "_find_tenets_executable", return_value="tenets"), mock.patch.object(
            ft, "_run_bounded_process", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertIn("safety limit", result.message)

    def test_temporary_output_allocation_failure_is_fail_open_for_cli(self) -> None:
        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/trusted/tenets"
        ), mock.patch.object(
            ft.tempfile, "TemporaryDirectory", side_effect=OSError("temporary storage unavailable")
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertIn("temporary storage unavailable", result.message)

        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/trusted/tenets"
        ), mock.patch.object(
            ft.tempfile, "TemporaryDirectory", side_effect=OSError("temporary storage unavailable")
        ), mock.patch("builtins.print"):
            code = ft.run_cli([str(self.root), "audit", "--strict"])
        self.assertEqual(code, 1)

    def test_temporary_isolation_cleanup_failure_is_degraded(self) -> None:
        isolation = Path(self.temp.name) / "isolation"
        isolation.mkdir()

        class BrokenTemporaryDirectory:
            name = str(isolation)

            def cleanup(self):
                raise RuntimeError("cleanup denied")

        completed = self._process(stdout=b'{"files":["src/app.py"]}')
        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/trusted/tenets"
        ), mock.patch.object(
            ft.tempfile, "TemporaryDirectory", return_value=BrokenTemporaryDirectory()
        ), mock.patch.object(ft, "_run_bounded_process", return_value=completed):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertIn("cleanup failed", result.message)

    def test_malformed_json_is_fail_open(self) -> None:
        completed = self._process(stdout=b"not-json")
        with mock.patch.object(ft, "_find_tenets_executable", return_value="tenets"), mock.patch.object(
            ft, "_run_bounded_process", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")

    def test_invalid_inputs_fail_closed_with_value_error(self) -> None:
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root / "missing", "audit")
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root, "  ")
        for top in (0, 201, True, 1.5):
            with self.subTest(top=top), self.assertRaises(ValueError):
                ft.generate_tenets_context(self.root, "audit", top=top)  # type: ignore[arg-type]
        for timeout in (0, -1, True, None, "1", float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ft.generate_tenets_context(
                    self.root,
                    "audit",
                    timeout_seconds=timeout,  # type: ignore[arg-type]
                )

    def test_strict_cli_returns_nonzero_when_tool_is_missing(self) -> None:
        with mock.patch.object(ft, "_find_tenets_executable", return_value=None), mock.patch(
            "builtins.print"
        ):
            code = ft.run_cli([str(self.root), "audit", "--strict"])
        self.assertEqual(code, 1)

    def test_cache_runs_ranker_once_per_project_task(self) -> None:
        unavailable = ft.TenetsContextResult(
            schema_version=1,
            tool="tenets",
            expected_version=ft.TENETS_VERSION,
            adapter_version=2,
            status="unavailable",
            project_root=str(self.root),
            task="audit",
            files=(),
            message="missing",
            duration_seconds=0.0,
            output_path=str(self.state / "context.json"),
            command=(),
        )
        with mock.patch.object(ft, "generate_tenets_context", return_value=unavailable) as generate:
            first = ft.cached_tenets_context(self.root, "audit")
            second = ft.cached_tenets_context(self.root, "audit")
        self.assertIs(first, second)
        generate.assert_called_once()

    def test_repository_mutation_invalidates_cached_ranking(self) -> None:
        result = ft.TenetsContextResult(
            schema_version=1,
            tool="tenets",
            expected_version=ft.TENETS_VERSION,
            adapter_version=2,
            status="ok",
            project_root=str(self.root),
            task="audit",
            files=(ft.RankedFile("src/app.py", 1.0),),
            message="ok",
            duration_seconds=0.0,
            output_path=str(self.state / "context.json"),
            command=("tenets",),
        )
        with mock.patch.object(
            ft, "generate_tenets_context", return_value=result
        ) as generate:
            ft.cached_tenets_context(self.root, "audit")
            (self.root / "src" / "app.py").write_text(
                "print('mutated')\n", encoding="utf-8"
            )
            ft.cached_tenets_context(self.root, "audit")
        self.assertEqual(generate.call_count, 2)


    def test_distribution_version_mismatch_disables_execution(self) -> None:
        with mock.patch.object(ft, "_find_tenets_executable", return_value="/trusted/tenets"), \
             mock.patch.object(ft, "_tenets_distribution_version", return_value="0.13.2"), \
             mock.patch.object(ft, "_run_bounded_process") as runner:
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("version mismatch", result.message)
        runner.assert_not_called()

    def test_evidence_write_failure_degrades_safe_ranking(self) -> None:
        payload = {"files": [{"path": "src/app.py", "score": 1.0}]}
        completed = self._process(stdout=json.dumps(payload).encode())
        with mock.patch.object(ft, "_find_tenets_executable", return_value="/trusted/tenets"), \
             mock.patch.object(ft, "_run_bounded_process", return_value=completed), \
             mock.patch.object(ft, "_atomic_write_json", side_effect=OSError("read only")):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertEqual([item.path for item in result.files], ["src/app.py"])
        self.assertEqual(result.output_path, "")
        self.assertIn("Evidence write failed", result.message)

    def test_reader_prefers_available_byte_read1(self) -> None:
        class Pipe:
            def __init__(self):
                self.calls = 0
            def read1(self, _size):
                self.calls += 1
                return b"abcdef" if self.calls == 1 else b""
            def read(self, _size):
                raise AssertionError("blocking read must not be used when read1 exists")
            def close(self):
                pass
        chunks = []
        overflow = __import__("threading").Event()
        state = {}
        lock = __import__("threading").Lock()
        ft._read_bounded_pipe(
            Pipe(), limit=3, stream_name="stdout", chunks=chunks,
            overflow_event=overflow, state=state, state_lock=lock,
        )
        self.assertTrue(overflow.is_set())
        self.assertEqual(b"".join(chunks), b"abc")
        self.assertEqual(state.get("overflow_stream"), "stdout")

    def test_audit_prompt_spellings_drive_context_task(self) -> None:
        for argv, expected in (
            (["audit", "--session-prompt", "repair billing"], "repair billing"),
            (["prodready", "--session-prompt=repair auth"], "repair auth"),
            (["audit", "--guiding-prompt", "repair queue"], "repair queue"),
            (["audit", "--guiding-prompt=repair launch"], "repair launch"),
        ):
            with self.subTest(argv=argv), mock.patch.dict(
                os.environ, {"FLEXFACTOR_TENETS_TASK": ""}, clear=False
            ):
                self.assertEqual(ft._argv_task(argv), expected)

    def test_environment_guiding_prompt_drives_context_task(self) -> None:
        with mock.patch.dict(os.environ, {
            "FLEXFACTOR_TENETS_TASK": "",
            "FLEXFACTOR_SESSION_PROMPT": "",
            "FLEXFACTOR_GUIDING_PROMPT": "repair the mobile launch path",
        }, clear=False):
            self.assertEqual(
                ft._argv_task(["prodready"], project=self.root),
                "repair the mobile launch path",
            )

    def test_environment_session_prompt_routes_per_target_before_ranking(self) -> None:
        first = self.root.parent / "GrantFlow"
        second = self.root.parent / "purpose-foundry"
        first.mkdir(exist_ok=True)
        second.mkdir(exist_ok=True)
        argv = ["audit", "--program", str(first), "--program", str(second)]
        with mock.patch.dict(os.environ, {
            "FLEXFACTOR_TENETS_TASK": "",
            "FLEXFACTOR_SESSION_PROMPT": (
                "GrantFlow: repair billing. Purpose Foundry: fix deck exports."
            ),
            "FLEXFACTOR_GUIDING_PROMPT": "",
        }, clear=False):
            first_task = ft._argv_task(argv, project=first)
            second_task = ft._argv_task(argv, project=second)
        self.assertIn("repair billing", first_task)
        self.assertNotIn("deck exports", first_task)
        self.assertIn("deck exports", second_task)
        self.assertNotIn("repair billing", second_task)

    def test_multi_program_guiding_prompts_route_to_matching_project(self) -> None:
        first = self.root.parent / "GrantFlow"
        second = self.root.parent / "family-stewardship"
        first.mkdir(exist_ok=True)
        second.mkdir(exist_ok=True)
        argv = [
            "audit",
            "--program", str(first),
            "--guiding-prompt", "repair grant matching",
            "--program", "family stewardship",
            "--guiding-prompt", "repair family workflow",
        ]
        with mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS_TASK": ""}, clear=False):
            self.assertEqual(ft._argv_task(argv, project=first), "repair grant matching")
            self.assertEqual(ft._argv_task(argv, project=second), "repair family workflow")

    def test_multi_program_session_prompt_is_routed_before_ranking(self) -> None:
        first = self.root.parent / "GrantFlow"
        second = self.root.parent / "purpose-foundry"
        first.mkdir(exist_ok=True)
        second.mkdir(exist_ok=True)
        argv = [
            "audit",
            "--program", str(first),
            "--program", str(second),
            "--session-prompt",
            "GrantFlow: repair billing. Purpose Foundry: fix deck exports. "
            "All programs must verify the release.",
        ]
        with mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS_TASK": ""}, clear=False):
            first_task = ft._argv_task(argv, project=first)
            second_task = ft._argv_task(argv, project=second)
        self.assertIn("repair billing", first_task)
        self.assertNotIn("deck exports", first_task)
        self.assertIn("deck exports", second_task)
        self.assertNotIn("repair billing", second_task)
        self.assertIn("verify the release", first_task)
        self.assertIn("verify the release", second_task)

    def test_unassigned_session_work_never_leaks_to_another_program(self) -> None:
        first = self.root.parent / "GrantFlow"
        second = self.root.parent / "purpose-foundry"
        first.mkdir(exist_ok=True)
        second.mkdir(exist_ok=True)
        argv = [
            "audit", "--program", str(first), "--program", str(second),
            "--session-prompt", "GrantFlow: repair billing.",
        ]
        with mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS_TASK": ""}, clear=False):
            second_task = ft._argv_task(argv, project=second)
        self.assertNotIn("repair billing", second_task)
        self.assertIn("production readiness", second_task)

    def test_same_named_programs_prefer_the_exact_resolved_path(self) -> None:
        first = self.root.parent / "one" / "app"
        second = self.root.parent / "two" / "app"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        argv = [
            "audit",
            "--program", str(first),
            "--guiding-prompt", "repair first checkout",
            "--program", str(second),
            "--guiding-prompt", "repair second checkout",
        ]
        with mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS_TASK": ""}, clear=False):
            self.assertEqual(
                ft._argv_task(argv, project=second),
                "repair second checkout",
            )

    def test_ambiguous_program_basename_does_not_guess_a_prompt(self) -> None:
        first = self.root.parent / "one" / "app"
        second = self.root.parent / "two" / "app"
        unmatched = self.root.parent / "three" / "app"
        for path in (first, second, unmatched):
            path.mkdir(parents=True)
        argv = [
            "audit",
            "--program", str(first),
            "--guiding-prompt", "repair first checkout",
            "--program", str(second),
            "--guiding-prompt", "repair second checkout",
        ]
        with mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS_TASK": ""}, clear=False):
            task = ft._argv_task(argv, project=unmatched)
        self.assertNotIn(task, {"repair first checkout", "repair second checkout"})
        self.assertIn("production readiness", task)

class TenetsInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _result(self, *paths: str, status: str = "ok") -> ft.TenetsContextResult:
        return ft.TenetsContextResult(
            schema_version=1,
            tool="tenets",
            expected_version=ft.TENETS_VERSION,
            adapter_version=2,
            status=status,
            project_root=str(self.root),
            task="audit",
            files=tuple(ft.RankedFile(path, 1.0 - index / 10) for index, path in enumerate(paths)),
            message=status,
            duration_seconds=0.0,
            output_path=str(self.root / "context.json"),
            command=("tenets",),
        )

    def test_install_prioritizes_without_dropping_or_duplicating_files(self) -> None:
        original = ["README.md", "src/app.py", "src/other.py", "tests/test_app.py"]

        def enumerate_source_files(project_dir: str, skip_clean=None):
            return list(original)

        runtime = {
            "_enumerate_source_files": enumerate_source_files,
            "_canon_rel": lambda value: value.replace("\\", "/").removeprefix("./"),
        }
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("tests/test_app.py", "src/app.py"),
        ):
            ft.install(runtime, argv=["audit", "--program", str(self.root)])
            result = runtime["_enumerate_source_files"](str(self.root))
        self.assertEqual(result[:2], ["tests/test_app.py", "src/app.py"])
        self.assertCountEqual(result, original)
        self.assertEqual(len(result), len(original))

    def test_ranked_file_beyond_parameter_cap_enters_same_sized_budget(self) -> None:
        original = ["src/first.py", "src/second.py", "src/target.py", "src/fourth.py"]
        observed_caps: list[int] = []

        def enumerate_source_files(project_dir: str, max_files: int = 2):
            observed_caps.append(max_files)
            return list(original[:max_files])

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("src/target.py"),
        ):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root), max_files=2)
        self.assertEqual(observed_caps, [sys.maxsize])
        self.assertEqual(result, ["src/target.py", "src/first.py"])
        self.assertEqual(len(result), 2)

    def test_lifted_parameter_cap_has_no_fixed_repository_ceiling(self) -> None:
        observed_caps: list[int] = []

        def enumerate_source_files(project_dir: str, max_files: int = 2):
            observed_caps.append(max_files)
            files = ["src/first.py", "src/second.py"]
            if max_files > 100_000:
                files.append("src/target.py")
            return files[:max_files]

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("src/target.py"),
        ):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root), max_files=2)
        self.assertEqual(observed_caps, [sys.maxsize])
        self.assertEqual(result, ["src/target.py", "src/first.py"])

    def test_uncapped_enumeration_failure_preserves_original_and_reports_degraded(self) -> None:
        original = ["src/first.py", "src/second.py", "src/target.py"]

        def enumerate_source_files(project_dir: str, max_files: int = 2):
            if max_files == sys.maxsize:
                raise MemoryError("candidate set exceeds available memory")
            return list(original[:max_files])

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("src/target.py"),
        ):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root), max_files=2)
        self.assertEqual(result, ["src/first.py", "src/second.py"])
        evidence = runtime["_TENETS_CONTEXT_LAST"]
        self.assertIsInstance(evidence, dict)
        assert isinstance(evidence, dict)
        self.assertEqual(evidence["status"], "degraded")
        self.assertIn("original order preserved", evidence["message"])

    def test_ranked_file_beyond_positional_cap_enters_budget(self) -> None:
        original = ["src/first.py", "src/second.py", "src/target.py"]

        def enumerate_source_files(project_dir: str, max_files: int):
            return list(original[:max_files])

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("src/target.py"),
        ):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root), 2)
        self.assertEqual(result, ["src/target.py", "src/first.py"])

    def test_ranked_file_beyond_global_cap_enters_budget_and_cap_is_restored(self) -> None:
        runtime: dict[str, object] = {"MAX_FILES_PER_RUN": 2}

        def enumerate_source_files(project_dir: str):
            files = ["src/first.py", "src/second.py", "src/target.py"]
            limit = runtime["MAX_FILES_PER_RUN"]
            assert isinstance(limit, int)
            return files[:limit]

        runtime["_enumerate_source_files"] = enumerate_source_files
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("src/target.py"),
        ):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root))  # type: ignore[operator]
        self.assertEqual(result, ["src/target.py", "src/first.py"])
        self.assertEqual(runtime["MAX_FILES_PER_RUN"], 2)

    def test_degraded_context_preserves_original_cap_and_order(self) -> None:
        original = ["b.py", "a.py", "target.py"]
        observed_caps: list[int] = []

        def enumerate_source_files(project_dir: str, max_files: int = 2):
            observed_caps.append(max_files)
            return list(original[:max_files])

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result(status="degraded"),
        ):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root))
        self.assertEqual(result, ["b.py", "a.py"])
        self.assertEqual(observed_caps, [2])

    def test_install_is_idempotent(self) -> None:
        calls = 0

        def enumerate_source_files(project_dir: str):
            nonlocal calls
            calls += 1
            return ["a.py"]

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(
            ft,
            "cached_tenets_context",
            return_value=self._result("a.py"),
        ):
            ft.install(runtime)
            wrapped = runtime["_enumerate_source_files"]
            ft.install(runtime)
            self.assertIs(runtime["_enumerate_source_files"], wrapped)
            runtime["_enumerate_source_files"](str(self.root))
        self.assertEqual(calls, 1)

    def test_disable_switch_preserves_original_order_without_calling_tenets(self) -> None:
        original = ["b.py", "a.py"]
        runtime = {"_enumerate_source_files": lambda project_dir: list(original)}
        with mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS": "0"}), mock.patch.object(
            ft, "cached_tenets_context"
        ) as context:
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root))
        self.assertEqual(result, original)
        context.assert_not_called()


class TenetsManifestAndEntryTests(unittest.TestCase):
    def test_manifest_reviewable_files_are_ranked_without_dropping_members(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = ["src/first.py", "src/second.py", "src/target.py"]
            def manifest(project_dir):
                return {
                    "reviewable_files": list(original),
                    "binary_files": ["asset.bin"],
                    "count": 4,
                }
            runtime = {
                "_repository_review_manifest": manifest,
                "_canon_rel": lambda value: value.replace("\\", "/").removeprefix("./"),
            }
            ranked = ft.TenetsContextResult(
                schema_version=1,
                tool="tenets",
                expected_version=ft.TENETS_VERSION,
                adapter_version=2,
                status="ok",
                project_root=str(root),
                task="audit",
                files=(ft.RankedFile("src/target.py", 1.0),),
                message="ok",
                duration_seconds=0.0,
                output_path="",
                command=("tenets",),
            )
            with mock.patch.object(ft, "cached_tenets_context", return_value=ranked):
                ft.install(runtime, argv=["audit", "--session-prompt", "target task"])
                first = runtime["_repository_review_manifest"](str(root))
                second = runtime["_repository_review_manifest"](str(root))
            self.assertEqual(
                first["reviewable_files"],
                ["src/target.py", "src/first.py", "src/second.py"],
            )
            self.assertCountEqual(first["reviewable_files"], original)
            self.assertEqual(second["reviewable_files"], first["reviewable_files"])
            self.assertEqual(first["binary_files"], ["asset.bin"])

    def test_shared_run_cli_arms_tenets_for_direct_entrypoints(self) -> None:
        import inspect
        import flexfactor as ff
        source = inspect.getsource(ff.run_cli)
        self.assertIn("_runtime_tenets.install(", source)
        self.assertIn("globals(), argv=", source)

    def test_launcher_import_defers_install_and_forwards_explicit_argv(self) -> None:
        fake_flexfactor = types.ModuleType("flexfactor")
        fake_flexfactor.run_cli = mock.Mock(return_value=7)
        fake_memory = types.ModuleType("obsidian_memory")
        fake_memory.recall = mock.Mock()
        fake_memory.remember = mock.Mock()
        fake_directed = types.ModuleType("flexfactor_directed")
        fake_directed.install = mock.Mock()
        fake_tenets = types.ModuleType("flexfactor_tenets")
        fake_tenets.install = mock.Mock()
        modules = {
            "flexfactor": fake_flexfactor,
            "obsidian_memory": fake_memory,
            "flexfactor_directed": fake_directed,
            "flexfactor_tenets": fake_tenets,
        }
        path = Path(__file__).with_name("flexfactor_run.py")
        spec = importlib.util.spec_from_file_location("_flexfactor_run_test", path)
        assert spec is not None and spec.loader is not None
        shim = importlib.util.module_from_spec(spec)

        with mock.patch.dict(sys.modules, modules, clear=False):
            spec.loader.exec_module(shim)
        fake_tenets.install.assert_not_called()

        argv = ["audit", "--session-prompt", "repair the actual run"]
        self.assertEqual(shim.run_cli(argv), 7)
        fake_tenets.install.assert_called_once_with(
            vars(fake_flexfactor), argv=argv
        )
        fake_flexfactor.run_cli.assert_called_once_with(argv)

    def test_reinstall_refreshes_arguments_for_long_lived_embedders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = {"_enumerate_source_files": lambda project: ["app.py"]}
            observed_tasks: list[str] = []

            def context(project, task, **_kwargs):
                observed_tasks.append(task)
                return ft.TenetsContextResult(
                    schema_version=1, tool="tenets", expected_version=ft.TENETS_VERSION,
                    adapter_version=2, status="degraded", project_root=str(project),
                    task=task, files=(), message="test", duration_seconds=0,
                    output_path="", command=(),
                )

            with mock.patch.object(ft, "cached_tenets_context", side_effect=context):
                ft.install(runtime, argv=["audit", "--session-prompt", "first task"])
                runtime["_enumerate_source_files"](str(root))
                ft.install(runtime, argv=["audit", "--session-prompt", "second task"])
                runtime["_enumerate_source_files"](str(root))
        self.assertEqual(observed_tasks, ["first task", "second task"])


if __name__ == "__main__":
    unittest.main()
