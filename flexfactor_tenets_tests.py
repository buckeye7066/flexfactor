"""Hermetic unit tests for FlexFactor's optional Tenets adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
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
        self.state_patch = mock.patch.dict(os.environ, {"FLEXFACTOR_STATE_DIR": str(self.state)}, clear=False)
        self.state_patch.start()
        with ft._RESULT_CACHE_LOCK:
            ft._RESULT_CACHE.clear()

    def tearDown(self) -> None:
        self.state_patch.stop()
        self.temp.cleanup()

    def test_missing_tenets_is_fail_open_and_writes_external_evidence(self) -> None:
        with mock.patch.object(ft.shutil, "which", return_value=None):
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
        completed = subprocess.CompletedProcess(
            args=["tenets"], returncode=0, stdout=json.dumps(payload).encode(), stderr=b""
        )
        with mock.patch.object(ft.shutil, "which", return_value="/usr/bin/tenets"), mock.patch.object(
            ft.subprocess, "run", return_value=completed
        ) as runner:
            result = ft.generate_tenets_context(self.root, "fix auth", top=10)
        self.assertEqual(result.status, "ok")
        self.assertEqual([item.path for item in result.files], ["src/app.py", "README.md"])
        self.assertEqual(result.files[0].score, 0.91)
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ("/usr/bin/tenets", "rank", "fix auth"))
        self.assertNotIn("shell", runner.call_args.kwargs)
        self.assertIs(runner.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_empty_valid_result_is_degraded_not_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["tenets"], returncode=0, stdout=b'{"files": []}', stderr=b""
        )
        with mock.patch.object(ft.shutil, "which", return_value="tenets"), mock.patch.object(
            ft.subprocess, "run", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")

    def test_nonzero_exit_is_fail_open_and_stderr_is_bounded(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["tenets"], returncode=3, stdout=b"", stderr=(b"failure " * 100_000)
        )
        with mock.patch.object(ft.shutil, "which", return_value="tenets"), mock.patch.object(
            ft.subprocess, "run", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")
        self.assertLessEqual(len(result.message), 2000)

    def test_timeout_is_fail_open(self) -> None:
        with mock.patch.object(ft.shutil, "which", return_value="tenets"), mock.patch.object(
            ft.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["tenets"], timeout=1),
        ):
            result = ft.generate_tenets_context(self.root, "audit", timeout_seconds=1)
        self.assertEqual(result.status, "degraded")
        self.assertIn("timeout", result.message)

    def test_malformed_json_is_fail_open(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["tenets"], returncode=0, stdout=b"not-json", stderr=b""
        )
        with mock.patch.object(ft.shutil, "which", return_value="tenets"), mock.patch.object(
            ft.subprocess, "run", return_value=completed
        ):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "degraded")

    def test_invalid_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root / "missing", "audit")
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root, "  ")
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root, "audit", top=0)
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root, "audit", top=1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root, "audit", timeout_seconds=0)
        with self.assertRaises(ValueError):
            ft.generate_tenets_context(self.root, "audit", timeout_seconds=True)

    def test_strict_cli_returns_nonzero_when_tool_is_missing(self) -> None:
        with mock.patch.object(ft.shutil, "which", return_value=None), mock.patch(
            "builtins.print"
        ):
            code = ft.run_cli([str(self.root), "audit", "--strict"])
        self.assertEqual(code, 1)

    def test_cache_runs_ranker_once_per_project_task(self) -> None:
        unavailable = ft.TenetsContextResult(
            schema_version=1,
            tool="tenets",
            expected_version=ft.TENETS_VERSION,
            adapter_version=1,
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

    def _ok_result(self) -> ft.TenetsContextResult:
        return ft.TenetsContextResult(
            schema_version=1,
            tool="tenets",
            expected_version=ft.TENETS_VERSION,
            adapter_version=1,
            status="ok",
            project_root=str(self.root),
            task="audit",
            files=(
                ft.RankedFile("tests/test_app.py", 0.9),
                ft.RankedFile("src/app.py", 0.8),
            ),
            message="ok",
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
        with mock.patch.object(ft, "cached_tenets_context", return_value=self._ok_result()):
            ft.install(runtime, argv=["audit", "--program", str(self.root)])
            result = runtime["_enumerate_source_files"](str(self.root))
        self.assertEqual(result[:2], ["tests/test_app.py", "src/app.py"])
        self.assertCountEqual(result, original)
        self.assertEqual(len(result), len(original))

    def test_degraded_context_preserves_original_order(self) -> None:
        original = ["b.py", "a.py"]
        runtime = {"_enumerate_source_files": lambda project_dir: list(original)}
        degraded = self._ok_result().__class__(
            **{**self._ok_result().__dict__, "status": "degraded", "files": ()}
        )
        with mock.patch.object(ft, "cached_tenets_context", return_value=degraded):
            ft.install(runtime)
            result = runtime["_enumerate_source_files"](str(self.root))
        self.assertEqual(result, original)

    def test_install_is_idempotent(self) -> None:
        calls = 0

        def enumerate_source_files(project_dir: str):
            nonlocal calls
            calls += 1
            return ["a.py"]

        runtime = {"_enumerate_source_files": enumerate_source_files}
        with mock.patch.object(ft, "cached_tenets_context", return_value=self._ok_result()):
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
