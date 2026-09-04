"""Hermetic unit tests for FlexFactor's optional Tenets adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
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
        with ft._RESULT_CACHE_LOCK:
            ft._RESULT_CACHE.clear()

    def tearDown(self) -> None:
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
        self.assertTrue(output.is_relative_to(self.state))
        self.assertFalse(output.is_relative_to(self.root))
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["tool"], "tenets")
        self.assertEqual(payload["expected_version"], "0.13.3")
        self.assertEqual(payload["adapter_version"], 2)

    def test_virtualenv_sibling_executable_precedes_ambient_path(self) -> None:
        scripts = Path(self.temp.name) / "venv" / "Scripts"
        scripts.mkdir(parents=True)
        interpreter = scripts / "python.exe"
        interpreter.write_bytes(b"")
        tenets = scripts / "tenets.exe"
        tenets.write_bytes(b"")
        with mock.patch.object(ft.shutil, "which", return_value="C:/global/tenets.exe") as which:
            found = ft._find_tenets_executable(
                interpreter=interpreter,
                platform_name="nt",
                path_value="",
            )
        self.assertEqual(Path(found or ""), tenets.resolve())
        which.assert_not_called()

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
        self.assertEqual(runner.call_args.kwargs["cwd"], self.root.resolve())
        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 120.0)

    def test_real_cli_output_file_is_used_instead_of_console_stdout(self) -> None:
        payload = {"files": [{"path": "src/app.py", "score": 0.95}]}
        written_path: list[Path] = []

        def runner(command, **_kwargs):
            output_index = command.index("--output") + 1
            output_path = Path(command[output_index])
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            written_path.append(output_path)
            return self._process(stdout=b"OK Saved ranking to temporary file\n")

        with mock.patch.object(
            ft, "_find_tenets_executable", return_value="/usr/bin/tenets"
        ), mock.patch.object(ft, "_run_bounded_process", side_effect=runner):
            result = ft.generate_tenets_context(self.root, "audit")

        self.assertEqual(result.status, "ok")
        self.assertEqual([item.path for item in result.files], ["src/app.py"])
        self.assertTrue(written_path)
        self.assertFalse(written_path[0].exists())

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


if __name__ == "__main__":
    unittest.main()
