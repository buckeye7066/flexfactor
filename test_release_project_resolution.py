"""Known targets must not trigger a second home-directory discovery."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import flexfactor_tests as fixtures
import flexfactor_directed as directed
import flexfactor_tenets as tenets

ff = fixtures.ff


class KnownProjectResolutionTests(unittest.TestCase):
    def test_installed_lookup_never_uses_typo_after_canonical_discovery_exhausts(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            (Path(tmp) / "grantfloo").mkdir()
            runtime = {"_find_local_project": ff._find_local_project,
                       "_PROJECT_ROOTS": [tmp], "_slugify": ff._slugify}
            if hasattr(ff, "_find_local_project_result"):
                runtime["_find_local_project_result"] = ff._find_local_project_result
            clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0.0, 2.0]))
            directed.install(runtime)
            with mock.patch.object(ff, "_PROJECT_ROOTS", [tmp]), \
                 mock.patch.object(ff, "time", clock):
                self.assertIsNone(runtime["_find_local_project"]("grantflow"))

    def test_canonical_name_lookup_exhaustion_refuses_a_partial_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0.0, 2.0]))
            with mock.patch.object(ff, "_PROJECT_ROOTS", [tmp]), \
                 mock.patch.object(ff, "time", clock):
                self.assertIsNone(ff._find_local_project("grantflow"))

    def test_canonical_lookup_entry_limit_refuses_a_partial_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            (Path(tmp) / "unrelated").mkdir()
            with mock.patch.object(ff, "_PROJECT_ROOTS", [tmp]), \
                 mock.patch.object(directed, "_PROJECT_LOOKUP_MAX_ENTRIES", 1):
                self.assertIsNone(ff._find_local_project("grantflow"))

    def test_canonical_lookup_preserves_exact_and_hidden_project_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "one", Path(tmp) / "two"
            first.mkdir()
            second.mkdir()
            (first / "grantflow-prefix").mkdir()
            (first / ".grantflow").mkdir()
            (second / "GrantFlow").mkdir()
            with mock.patch.object(ff, "_PROJECT_ROOTS", [str(first), str(second)]):
                self.assertEqual(str(second / "GrantFlow"),
                                 ff._find_local_project("GrantFlow Repo"))
                (second / "GrantFlow").rmdir()
                self.assertEqual(str(first / ".grantflow"),
                                 ff._find_local_project("GrantFlow Repo"))

    def test_typo_discovery_exhaustion_refuses_a_partial_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0.0, 2.0, 3.0]))
            with mock.patch.object(directed, "time", clock, create=True):
                self.assertIsNone(directed.typo_resolve_local_project(
                    ["grnatflow"], [tmp], ff._slugify))

    def test_existing_directory_never_enters_name_resolver(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ff, "resolve_project_dir", side_effect=AssertionError("home discovery")
        ):
            self.assertEqual(Path(tmp).resolve(), tenets._resolved_cli_program_dir(tmp))

    def test_installed_context_reuses_the_running_alias_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runtime = {
                "_enumerate_source_files": lambda *_a, **_k: ["app.py"],
                "_FLEXFACTOR_TENETS_PROGRAM": "Friendly Alias",
            }
            seen = []

            def context(project, task, *, protected_roots):
                seen.append((project, task, protected_roots))
                return mock.Mock(status="degraded", to_dict=lambda: {})

            with mock.patch.object(tenets, "enabled", return_value=True), \
                 mock.patch.object(tenets, "cached_tenets_context", side_effect=context), \
                 mock.patch.object(ff, "resolve_project_dir",
                                   side_effect=AssertionError("home discovery")), \
                 mock.patch.dict(os.environ, {"FLEXFACTOR_TENETS_TASK": ""}):
                tenets.install(runtime, argv=[
                    "audit", "--program", "Friendly Alias", "--guiding-prompt", "fix login",
                ])
                self.assertEqual(["app.py"], runtime["_enumerate_source_files"](str(root)))
            self.assertEqual([(root, "fix login", (os.path.normcase(str(root)),))], seen)

    def test_typo_lookup_does_not_stat_unrelated_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            (Path(tmp) / "unrelated-mount").mkdir()
            original = os.path.isdir

            def checked(path):
                if os.path.basename(path) == "unrelated-mount":
                    raise AssertionError("unrelated mount was probed")
                return original(path)

            with mock.patch.object(directed.os.path, "isdir", side_effect=checked):
                self.assertEqual(str(Path(tmp) / "grantflow"),
                                 directed.typo_resolve_local_project(
                                     ["grnatflow"], [tmp], ff._slugify))


if __name__ == "__main__":
    unittest.main()
