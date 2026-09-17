"""Known targets must not trigger a second home-directory discovery."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import flexfactor_tests as fixtures
import flexfactor_directed as directed
import flexfactor_tenets as tenets

ff = fixtures.ff


def _ordered_scandir(names):
    """An os.scandir replacement with a FIXED entry order.

    Real scandir order is the filesystem's business, and a budget/deadline test
    that depends on it is testing the OS, not the lookup.
    """
    class _Entries:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def __iter__(self):
            return iter([SimpleNamespace(name=n) for n in names])

    return lambda _root: _Entries()


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
        """Exhaustion BEFORE any winner is established still refuses.

        This used to create `grantflow` + `unrelated` with a budget of 1 and
        assert None, which only ever held because the OS happened to enumerate
        `grantflow` FIRST - i.e. the winner WAS found inside the budget and was
        then thrown away by the next entry. That was the delayed-return defect,
        not this contract. The contract is about a partial inventory that never
        established a winner at all, so the budget is now exhausted BEFORE the
        match is reachable, deterministically rather than by scandir order.
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            with mock.patch.object(ff, "_PROJECT_ROOTS", [tmp]), \
                 mock.patch.object(ff.os, "scandir",
                                   _ordered_scandir(["unrelated-mount",
                                                     "grantflow"])), \
                 mock.patch.object(directed, "_PROJECT_LOOKUP_MAX_ENTRIES", 1):
                self.assertIsNone(ff._find_local_project("grantflow"))

    def test_canonical_lookup_keeps_a_match_found_inside_the_entry_budget(self):
        """The other side of the same boundary: a winner confirmed WITHIN the
        budget is authoritative, and entries that would have blown the budget
        cannot retract it."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "grantflow").mkdir()
            with mock.patch.object(ff, "_PROJECT_ROOTS", [tmp]), \
                 mock.patch.object(ff.os, "scandir",
                                   _ordered_scandir(["grantflow",
                                                     "unrelated-mount"])), \
                 mock.patch.object(directed, "_PROJECT_LOOKUP_MAX_ENTRIES", 1):
                self.assertEqual(str(Path(tmp) / "grantflow"),
                                 ff._find_local_project("grantflow"))

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

    # ------------------------------------------------------------------ #
    # STARTUP RESPONSIVENESS. Stated precisely, because the distinction is
    # the whole point: `_PROJECT_LOOKUP_SECONDS` is a COOPERATIVE budget
    # checked BETWEEN filesystem operations. It is NOT a hard timeout and
    # cannot interrupt a blocked `os.scandir` on, say, a disconnected
    # network root. Moving the return earlier removes the unnecessary work
    # AFTER a match is found - which is the case that actually bit startup -
    # and these two tests pin both halves of the real contract.
    # ------------------------------------------------------------------ #
    def test_startup_never_reaches_a_blocking_root_once_a_match_is_found(self):
        """A match in an earlier root makes a later blocking root irrelevant."""
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "one", Path(tmp) / "two"
            first.mkdir()
            second.mkdir()
            (first / "GrantFlow").mkdir()
            real_scandir = os.scandir
            blocked = threading.Event()

            def scandir(root):
                if os.path.normcase(str(root)) == os.path.normcase(str(second)):
                    # If the fix regresses, this makes the cost VISIBLE as
                    # latency instead of hanging the suite forever.
                    blocked.set()
                    threading.Event().wait(5.0)
                    raise AssertionError("a later blocking root was opened")
                return real_scandir(root)

            threads_before = threading.active_count()
            with mock.patch.object(ff, "_PROJECT_ROOTS", [str(first), str(second)]), \
                 mock.patch.object(ff.os, "scandir", scandir):
                started = time.perf_counter()
                resolved = ff._find_local_project("GrantFlow")
            elapsed = time.perf_counter() - started
            self.assertEqual(str(first / "GrantFlow"), resolved)
            self.assertFalse(blocked.is_set(),
                             "the blocking root must never be opened at all")
            self.assertLess(elapsed, 2.0, "startup must stay responsive")
            self.assertEqual(threads_before, threading.active_count(),
                             "the lookup is synchronous - it must never leave "
                             "an abandoned worker behind")

    def test_a_root_that_blocks_before_any_match_reports_an_honest_unresolved(self):
        """The HONEST limit, asserted rather than claimed.

        A root that blocks BEFORE any match is established is not interrupted -
        the budget is cooperative. What must hold is that the run then reports
        an unresolved lookup rather than silently presenting a weaker candidate
        as a proven winner: the later root holding the real `GrantFlow` is never
        reached, the tuple says the inventory is INCOMPLETE, and the installed
        wrapper refuses to fuzzy-resolve it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "one", Path(tmp) / "two"
            first.mkdir()
            second.mkdir()
            (second / "GrantFlow").mkdir()
            (first / "grantfloo").mkdir()  # a near-name the typo stage could grab
            real_scandir = os.scandir

            def scandir(root):
                if os.path.normcase(str(root)) == os.path.normcase(str(first)):
                    threading.Event().wait(0.20)
                return real_scandir(root)

            runtime = {"_find_local_project": ff._find_local_project,
                       "_find_local_project_result": ff._find_local_project_result,
                       "_PROJECT_ROOTS": [str(first), str(second)],
                       "_slugify": ff._slugify}
            directed.install(runtime)
            with mock.patch.object(ff, "_PROJECT_ROOTS", [str(first), str(second)]), \
                 mock.patch.object(ff.os, "scandir", scandir), \
                 mock.patch.object(directed, "_PROJECT_LOOKUP_SECONDS", 0.05):
                started = time.perf_counter()
                result = ff._find_local_project_result("GrantFlow")
                elapsed = time.perf_counter() - started
                self.assertEqual((None, False), result,
                                 "an interrupted lookup must not read as a "
                                 "completed search")
                self.assertIsNone(runtime["_find_local_project"]("GrantFlow"),
                                  "an incomplete inventory must not be fuzzy-"
                                  "resolved to a near-name")
            self.assertGreaterEqual(
                elapsed, 0.20,
                "this pins the honest limit: the cooperative budget does NOT "
                "abort a blocked filesystem call")

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
