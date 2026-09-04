from __future__ import annotations

from pathlib import Path
import ast
import re


def read(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# flexfactor_tenets.py
p = Path("flexfactor_tenets.py")
s = read(p)
s = replace_once(
    s,
    "from dataclasses import asdict, dataclass\n",
    "from dataclasses import asdict, dataclass, replace\nfrom importlib import metadata as importlib_metadata\n",
    "tenets imports",
)
s = s.replace("import shutil\n", "")

start = s.index("def _find_tenets_executable(")
end = s.index("\ndef _read_bounded_pipe(", start)
resolver = '''def _trusted_tenets_directories(
    *, interpreter: str | os.PathLike[str] | None = None, platform_name: str | None = None
) -> tuple[Path, ...]:
    interpreter_path = Path(interpreter or sys.executable).expanduser().resolve(strict=False)
    windows = (platform_name or os.name) == "nt"
    candidates = [interpreter_path.parent]
    prefix_scripts = Path(sys.prefix).expanduser().resolve(strict=False) / ("Scripts" if windows else "bin")
    if prefix_scripts not in candidates:
        candidates.append(prefix_scripts)
    return tuple(candidates)


def _is_trusted_tenets_executable(
    executable: str | os.PathLike[str],
    *,
    interpreter: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
) -> bool:
    windows = (platform_name or os.name) == "nt"
    expected_names = {"tenets.exe", "tenets"} if windows else {"tenets"}
    candidate = Path(executable).expanduser().resolve(strict=False)
    if candidate.name.lower() not in {name.lower() for name in expected_names}:
        return False
    if candidate.parent not in _trusted_tenets_directories(
        interpreter=interpreter, platform_name=platform_name
    ):
        return False
    return candidate.is_file() and (windows or os.access(candidate, os.X_OK))


def _find_tenets_executable(
    *,
    interpreter: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    path_value: str | None = None,
) -> str | None:
    """Resolve Tenets only from the active Python installation, never PATH."""
    del path_value  # API compatibility only. Ambient PATH is intentionally ignored.
    windows = (platform_name or os.name) == "nt"
    executable_names = ("tenets.exe", "tenets") if windows else ("tenets",)
    for directory in _trusted_tenets_directories(
        interpreter=interpreter, platform_name=platform_name
    ):
        for name in executable_names:
            candidate = directory / name
            if _is_trusted_tenets_executable(
                candidate, interpreter=interpreter, platform_name=platform_name
            ):
                return str(candidate.resolve(strict=False))
    return None


def _tenets_distribution_version() -> str | None:
    try:
        return importlib_metadata.version("tenets")
    except importlib_metadata.PackageNotFoundError:
        return None
'''
s = s[:start] + resolver + s[end:]

s = replace_once(
    s,
    "            chunk = pipe.read(64 * 1024)\n",
    "            read1 = getattr(pipe, \"read1\", None)\n"
    "            if callable(read1):\n"
    "                chunk = read1(64 * 1024)\n"
    "            else:\n"
    "                try:\n"
    "                    chunk = os.read(pipe.fileno(), 64 * 1024)\n"
    "                except (AttributeError, OSError, ValueError):\n"
    "                    chunk = pipe.read(64 * 1024)\n",
    "available-byte pipe read",
)

s = replace_once(
    s,
    "    resolved_executable = executable or _find_tenets_executable()\n    command: tuple[str, ...] = ()\n",
    "    resolved_executable = executable or _find_tenets_executable()\n"
    "    explicit_untrusted = bool(executable and not _is_trusted_tenets_executable(executable))\n"
    "    if explicit_untrusted:\n"
    "        resolved_executable = None\n"
    "    installed_version = _tenets_distribution_version()\n"
    "    if resolved_executable and installed_version != TENETS_VERSION:\n"
    "        resolved_executable = None\n"
    "    command: tuple[str, ...] = ()\n",
    "version/trust gate",
)
s = replace_once(
    s,
    '''    message = (
        f"Tenets {TENETS_VERSION} is not installed; install FlexFactor with "
        "the context extra (or the all extra) to enable local file ranking."
    )
''',
    '''    if installed_version and installed_version != TENETS_VERSION:
        message = (
            f"Tenets version mismatch: expected {TENETS_VERSION}, found {installed_version}; "
            "ranking disabled so an untested tool cannot influence audit order."
        )
    elif explicit_untrusted:
        message = "Tenets executable is outside the active Python installation; ranking disabled."
    else:
        message = (
            f"Tenets {TENETS_VERSION} is not installed; install FlexFactor with "
            "the context extra (or the all extra) to enable local file ranking."
        )
''',
    "unavailable message",
)

s = replace_once(
    s,
    "    _atomic_write_json(output_path, result.to_dict())\n    return result\n",
    '''    try:
        _atomic_write_json(output_path, result.to_dict())
    except OSError as exc:
        # Optional context ranking must not abort an audit because evidence storage failed.
        result = replace(
            result,
            message=_bounded_text(f"{result.message} Evidence write failed: {exc}"),
            output_path="",
        )
    return result
''',
    "evidence fail-open",
)

s = replace_once(
    s,
    '''    for index, item in enumerate(args):
        if item == "--goal" and index + 1 < len(args) and args[index + 1].strip():
            return args[index + 1].strip()
        if item.startswith("--goal=") and item.partition("=")[2].strip():
            return item.partition("=")[2].strip()
''',
    '''    task_flags = ("--session-prompt", "--guiding-prompt", "--goal")
    for flag in task_flags:
        for index, item in enumerate(args):
            if item == flag and index + 1 < len(args) and args[index + 1].strip():
                return args[index + 1].strip()
            prefix = flag + "="
            if item.startswith(prefix) and item.partition("=")[2].strip():
                return item.partition("=")[2].strip()
''',
    "audit task flags",
)

install_start = s.index(
    "def install(module_globals: MutableMapping[str, Any], *, argv: Sequence[str] | None = None) -> None:"
)
install_end = s.index("\ndef build_parser()", install_start)
install_impl = '''def install(module_globals: MutableMapping[str, Any], *, argv: Sequence[str] | None = None) -> None:
    """Idempotently prioritize canonical audit candidates without changing membership."""
    with _INSTALL_LOCK:
        if module_globals.get("_FLEXFACTOR_TENETS_INSTALLED"):
            return
        module_globals["_FLEXFACTOR_TENETS_INSTALLED"] = True
        prior_enum = module_globals.get("_enumerate_source_files")
        prior_manifest = module_globals.get("_repository_review_manifest")
        if not callable(prior_enum) and not callable(prior_manifest):
            return
        task = _argv_task(argv)
        canonicalize = module_globals.get("_canon_rel")

        def context_for(root: Path) -> TenetsContextResult | None:
            if not enabled():
                return None
            try:
                result = cached_tenets_context(root, task)
            except Exception as exc:
                module_globals["_TENETS_CONTEXT_LAST_ERROR"] = _bounded_text(str(exc))
                return None
            module_globals["_TENETS_CONTEXT_LAST"] = result.to_dict()
            return result if result.status == "ok" else None

        if callable(prior_enum):
            def enumerate_source_files(*args: Any, **kwargs: Any) -> Any:
                root = _infer_project_root(args, kwargs)
                if root is None:
                    return prior_enum(*args, **kwargs)
                result = context_for(root)
                if result is None:
                    return prior_enum(*args, **kwargs)
                try:
                    source_files, cap = _call_with_lifted_cap(
                        prior_enum, module_globals, args, kwargs
                    )
                except Exception as exc:
                    message = _bounded_text(
                        f"uncapped enumeration failed; original order preserved: {exc}"
                    )
                    degraded = result.to_dict()
                    degraded["status"] = "degraded"
                    degraded["message"] = message
                    module_globals["_TENETS_CONTEXT_LAST"] = degraded
                    module_globals["_TENETS_CONTEXT_LAST_ERROR"] = message
                    return prior_enum(*args, **kwargs)
                prioritized = _prioritize_paths(source_files, result.files, canonicalize)
                return _limit_paths(prioritized, cap)

            enumerate_source_files._tenets_wrapped = True  # type: ignore[attr-defined]
            module_globals["_enumerate_source_files"] = enumerate_source_files

        if callable(prior_manifest):
            def repository_review_manifest(*args: Any, **kwargs: Any) -> Any:
                manifest = prior_manifest(*args, **kwargs)
                if not isinstance(manifest, Mapping):
                    return manifest
                root = _infer_project_root(args, kwargs)
                if root is None:
                    return manifest
                result = context_for(root)
                if result is None:
                    return manifest
                reviewable = manifest.get("reviewable_files")
                prioritized = _prioritize_paths(reviewable, result.files, canonicalize)
                if prioritized is reviewable:
                    return manifest
                updated = dict(manifest)
                updated["reviewable_files"] = prioritized
                return updated

            repository_review_manifest._tenets_wrapped = True  # type: ignore[attr-defined]
            module_globals["_repository_review_manifest"] = repository_review_manifest
'''
s = s[:install_start] + install_impl + s[install_end:]
write(p, s)


# flexfactor.py shared run_cli entrypoint
p = Path("flexfactor.py")
s = read(p)
tree = ast.parse(s)
fn = next(
    n
    for n in tree.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_cli"
)
lines = s.splitlines(keepends=True)
if not any(
    "_runtime_tenets.install(globals()" in lines[i]
    for i in range(fn.lineno - 1, fn.end_lineno or fn.lineno)
):
    anchor = next(
        (
            i
            for i in range(fn.lineno - 1, fn.end_lineno or fn.lineno)
            if "_ff_directed.install(globals())" in lines[i]
        ),
        None,
    )
    if anchor is None:
        raise AssertionError("directed run_cli install anchor not found")
    indent = lines[anchor][: len(lines[anchor]) - len(lines[anchor].lstrip())]
    lines[anchor + 1 : anchor + 1] = [
        indent + "import sys as _runtime_sys\n",
        indent + "import flexfactor_tenets as _runtime_tenets\n",
        indent
        + "_runtime_tenets.install(globals(), argv=(argv if argv is not None else _runtime_sys.argv[1:]))\n",
    ]
    s = "".join(lines)
ast.parse(s)
write(p, s)


# Invariant process launch registry
p = Path("flexfactor_invariant_sweep_tests.py")
s = read(p)
s = replace_once(
    s,
    "_PROCESS_LAUNCH_SITES = {\n",
    '''_PROCESS_LAUNCH_SITES = {
    "flexfactor_tenets.py::_run_bounded_process": (
        "Optional local Tenets 0.13.3 context ranker launches only the exact "
        "console script resolved from the active Python installation after exact "
        "distribution-version validation; ambient PATH and target-controlled "
        "executables are rejected, shell=False, and time/output are bounded."
    ),
''',
    "process launch registry",
)
write(p, s)


# Production readiness must run the Tenets test module.
p = Path(".github/workflows/production-readiness.yml")
s = read(p)
s = replace_once(
    s,
    "                   flexfactor_runtime_hardening_tests.py \\\n",
    "                   flexfactor_runtime_hardening_tests.py \\\n                   flexfactor_tenets_tests.py \\\n",
    "production test list",
)
write(p, s)


# Windows setup-python console scripts live under Scripts.
p = Path(".github/workflows/tenets-context.yml")
s = read(p)
s = replace_once(
    s,
    '''          assert Path(executable).resolve().parent == interpreter_dir, (
              executable,
              interpreter_dir,
          )
''',
    '''          trusted_dirs = {
              interpreter_dir,
              Path(sys.prefix).resolve() / ("Scripts" if os.name == "nt" else "bin"),
          }
          assert Path(executable).resolve().parent in trusted_dirs, (
              executable,
              sorted(str(path) for path in trusted_dirs),
          )
''',
    "windows scripts assertion",
)
write(p, s)


# Tenets regressions
p = Path("flexfactor_tenets_tests.py")
s = read(p)
s = replace_once(
    s,
    "        self.state_patch.start()\n        with ft._RESULT_CACHE_LOCK:\n",
    "        self.state_patch.start()\n"
    "        self.version_patch = mock.patch.object(ft, \"_tenets_distribution_version\", return_value=ft.TENETS_VERSION)\n"
    "        self.version_patch.start()\n"
    "        with ft._RESULT_CACHE_LOCK:\n",
    "test version setup",
)
s = replace_once(
    s,
    "        self.state_patch.stop()\n        self.temp.cleanup()\n",
    "        self.version_patch.stop()\n        self.state_patch.stop()\n        self.temp.cleanup()\n",
    "test version teardown",
)

pattern = re.compile(
    r"    def test_virtualenv_sibling_executable_precedes_ambient_path\(self\) -> None:\n.*?(?=    def test_success_keeps_only_safe_unique_repository_paths)",
    re.S,
)
replacement = '''    def test_trusted_interpreter_install_resolution_ignores_ambient_path(self) -> None:
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
        fake.write_bytes(b"MZ" if os.name == "nt" else b"#!/bin/sh\\nexit 0\\n")
        if os.name != "nt":
            fake.chmod(0o755)
        with mock.patch.object(ft.sys, "prefix", str(Path(self.temp.name) / "empty-prefix")):
            found = ft._find_tenets_executable(
                interpreter=Path(self.temp.name) / "python", path_value=str(self.root)
            )
        self.assertIsNone(found)

'''
s, count = pattern.subn(replacement, s, count=1)
if count != 1:
    raise AssertionError(f"resolver test replacement count={count}")

marker = "\n\nclass TenetsInstallTests(unittest.TestCase):\n"
extra_context = r'''
    def test_distribution_version_mismatch_disables_execution(self) -> None:
        with mock.patch.object(ft, "_find_tenets_executable", return_value="/trusted/tenets"), \
             mock.patch.object(ft, "_tenets_distribution_version", return_value="0.13.2"), \
             mock.patch.object(ft, "_run_bounded_process") as runner:
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("version mismatch", result.message)
        runner.assert_not_called()

    def test_evidence_write_failure_preserves_safe_ranking(self) -> None:
        payload = {"files": [{"path": "src/app.py", "score": 1.0}]}
        completed = self._process(stdout=json.dumps(payload).encode())
        with mock.patch.object(ft, "_find_tenets_executable", return_value="/trusted/tenets"), \
             mock.patch.object(ft, "_run_bounded_process", return_value=completed), \
             mock.patch.object(ft, "_atomic_write_json", side_effect=OSError("read only")):
            result = ft.generate_tenets_context(self.root, "audit")
        self.assertEqual(result.status, "ok")
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
'''
if marker not in s:
    raise AssertionError("TenetsInstallTests marker missing")
s = s.replace(marker, "\n" + extra_context + marker, 1)

end_marker = '\n\nif __name__ == "__main__":\n'
extra_install = r'''

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
        self.assertIn("_runtime_tenets.install(globals()", source)
'''
if end_marker not in s:
    raise AssertionError("test module end marker missing")
s = s.replace(end_marker, extra_install + end_marker, 1)
write(p, s)


for path in (
    "flexfactor.py",
    "flexfactor_tenets.py",
    "flexfactor_tenets_tests.py",
    "flexfactor_invariant_sweep_tests.py",
):
    ast.parse(read(path), filename=path)

print("Tenets hardening patch applied")
