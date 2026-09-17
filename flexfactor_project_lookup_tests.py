#!/usr/bin/env python3
"""Tests for `_find_local_project` - the URL/name -> local checkout resolver.

Why these exist: on 2026-08-24 a live 10-program audit pointed two programs at
the WRONG directory. `--program https://github.com/buckeye7066/Ellie` resolved
to the app's config folder `~/.ellie` instead of the checkout `~/Ellie`, and
ForgePress did the same. Both ran to completion with files_total=0 and
analyzed_source_files=0 - a full audit of nothing, reported as a finished
program.

Root cause: `_slugify` maps a leading dot to nothing, so '.ellie' and 'Ellie'
both slugify to 'ellie'; `os.listdir` returns the dot-entry first; and the
"exact match" pass returns the FIRST hit. The dot-directory therefore always
won. Deterministic, not a race.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import flexfactor as ff

# TEST HYGIENE (house rule, see CLAUDE.md): a test must NEVER touch the real
# ~/.flexfactor. These tests only call a pure lookup, but the redirect is
# unconditional so a future test added here cannot quietly evict the owner's
# real brain.json (which has happened before) or stomp a live dashboard.
_ISOLATED = tempfile.mkdtemp(prefix="ffproj-state-")
ff.BRAIN_PATH = os.path.join(_ISOLATED, "brain.json")
ff.STATUS_PATH = os.path.join(_ISOLATED, "status.json")
ff.RUNS_PATH = os.path.join(_ISOLATED, "runs")


class StateIsolationTests(unittest.TestCase):
    def test_this_module_never_points_at_the_real_flexfactor_state(self):
        real = os.path.join(os.path.expanduser("~"), ".flexfactor")
        for p in (ff.BRAIN_PATH, ff.STATUS_PATH, ff.RUNS_PATH):
            self.assertFalse(os.path.normcase(str(p)).startswith(os.path.normcase(real)),
                             f"{p} points at the owner's real state directory")


class HiddenSiblingTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ffproj-")
        self._saved = ff._PROJECT_ROOTS
        ff._PROJECT_ROOTS = [self.root]

    def tearDown(self):
        ff._PROJECT_ROOTS = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, *names):
        for n in names:
            os.makedirs(os.path.join(self.root, n), exist_ok=True)

    # --- the live defect -------------------------------------------------
    def test_real_checkout_wins_over_hidden_config_sibling(self):
        """'.ellie' (config) must never beat 'Ellie' (the checkout)."""
        self._mk(".ellie", "Ellie")
        self.assertEqual(ff._find_local_project("Ellie"),
                         os.path.join(self.root, "Ellie"))

    def test_forgepress_case(self):
        self._mk(".forgepress", "ForgePress")
        self.assertEqual(ff._find_local_project("ForgePress"),
                         os.path.join(self.root, "ForgePress"))

    def test_holds_regardless_of_listdir_order(self):
        """Create the visible one FIRST too - creation order must not decide it."""
        self._mk("Ellie", ".ellie")
        self.assertEqual(ff._find_local_project("Ellie"),
                         os.path.join(self.root, "Ellie"))

    # --- no regression ---------------------------------------------------
    def test_hidden_dir_still_resolves_when_it_is_the_only_candidate(self):
        """Don't trade a wrong answer for no answer."""
        self._mk(".ellie")
        self.assertEqual(ff._find_local_project("Ellie"),
                         os.path.join(self.root, ".ellie"))

    def test_exact_visible_beats_prefix_visible(self):
        self._mk("repo-rewards", "repo-rewards-archive")
        self.assertEqual(ff._find_local_project("repo-rewards"),
                         os.path.join(self.root, "repo-rewards"))

    def test_exact_hidden_still_beats_prefix_visible(self):
        """Precision tiers must stay ordered: exact > prefix, hidden or not."""
        self._mk(".grantflow", "grantflow-old-backup")
        self.assertEqual(ff._find_local_project("GrantFlow"),
                         os.path.join(self.root, ".grantflow"))

    def test_prefix_match_still_works(self):
        self._mk("genemap-discovery")
        self.assertEqual(ff._find_local_project("genemap"),
                         os.path.join(self.root, "genemap-discovery"))

    def test_no_candidate_returns_none(self):
        self._mk("something-else")
        self.assertIsNone(ff._find_local_project("Ellie"))

    def test_unrelated_entries_need_no_metadata_probe(self):
        self._mk("GrantFlow", "unrelated-mounted-folder")
        real_isdir = os.path.isdir

        def checked_isdir(path):
            if os.path.basename(path) == "unrelated-mounted-folder":
                self.fail("startup probed an unrelated directory")
            return real_isdir(path)

        with patch.object(ff.os.path, "isdir", side_effect=checked_isdir):
            self.assertEqual(ff._find_local_project("GrantFlow"),
                             os.path.join(self.root, "GrantFlow"))

    def test_later_root_exact_match_beats_earlier_prefix(self):
        self._mk("GrantFlow-backup", "second-root/GrantFlow")
        ff._PROJECT_ROOTS = [self.root, os.path.join(self.root, "second-root")]
        self.assertEqual(ff._find_local_project("GrantFlow"),
                         os.path.join(self.root, "second-root", "GrantFlow"))

    # --- precedence across roots, both directions ------------------------
    def test_later_visible_exact_beats_earlier_hidden_exact(self):
        """The early return is VISIBLE-exact only, so a hidden exact in an
        EARLIER root must not pre-empt the real checkout in a later one."""
        self._mk(".ellie", "second-root/Ellie")
        ff._PROJECT_ROOTS = [self.root, os.path.join(self.root, "second-root")]
        self.assertEqual(ff._find_local_project("Ellie"),
                         os.path.join(self.root, "second-root", "Ellie"))

    def test_earlier_visible_exact_beats_later_hidden_exact(self):
        """And the winner in the FIRST root still wins without enumerating on."""
        self._mk("Ellie", "second-root/.ellie")
        ff._PROJECT_ROOTS = [self.root, os.path.join(self.root, "second-root")]
        self.assertEqual(ff._find_local_project("Ellie"),
                         os.path.join(self.root, "Ellie"))

    def test_hidden_only_fallback_survives_across_roots(self):
        """Hidden-only projects must still resolve - last resort, never dropped."""
        self._mk("unrelated", "second-root/.ellie")
        ff._PROJECT_ROOTS = [self.root, os.path.join(self.root, "second-root")]
        self.assertEqual(ff._find_local_project("Ellie"),
                         os.path.join(self.root, "second-root", ".ellie"))

    # --- an exactly-named FILE is not a checkout -------------------------
    def test_exact_name_that_is_a_file_is_not_selected(self):
        """os.path.isdir is the gate: a FILE named 'Ellie' must not win, and must
        not take the early-return path either."""
        with open(os.path.join(self.root, "Ellie"), "w", encoding="utf-8") as fh:
            fh.write("not a checkout")
        self.assertIsNone(ff._find_local_project("Ellie"))
        self.assertEqual((None, True), ff._find_local_project_result("Ellie"))

    def test_exact_file_does_not_block_a_prefix_directory(self):
        with open(os.path.join(self.root, "Iplay"), "w", encoding="utf-8") as fh:
            fh.write("not a checkout")
        self._mk("Iplay-backup")
        self.assertEqual(ff._find_local_project("Iplay"),
                         os.path.join(self.root, "Iplay-backup"))

    # --- the tuple contract, not only the path --------------------------
    def test_a_found_project_reports_an_authoritative_result(self):
        self._mk("Iplay")
        self.assertEqual((os.path.join(self.root, "Iplay"), True),
                         ff._find_local_project_result("Iplay"))

    def test_no_candidate_reports_an_authoritative_absence(self):
        self._mk("something-else")
        self.assertEqual((None, True), ff._find_local_project_result("Ellie"))


# --------------------------------------------------------------------------- #
# The DELAYED-RETURN defect.
#
# The implementation these tests pin down used to append every confirmed
# candidate to `root_dirs`, finish enumerating the current root, and only THEN
# run a post-enumeration exact pass. So an answer the scan had ALREADY
# established could still be destroyed by whatever came after it in the same
# root: one more entry could exhaust the entry budget or the cooperative
# deadline (both of which `return None, False`), the iterator could raise
# OSError, or a plausible-but-wrong neighbour could block in a metadata probe.
#
# `None, False` then propagates as "this lookup is not authoritative", which
# `flexfactor_directed.install` deliberately refuses to resolve any further -
# so a visible exact checkout that WAS found on disk reported "not found".
#
# These tests drive the real production function with a fully controlled
# scandir, a controlled clock and a recording metadata probe. Nothing here
# depends on real OS enumeration order, a real disconnected drive, or sleeps.
# --------------------------------------------------------------------------- #
class _FakeDirEntry:
    """Only `.name` is consulted by the lookup - metadata goes through isdir."""

    def __init__(self, name: str):
        self.name = name

    def is_dir(self, *_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("the lookup must keep using os.path.isdir; a switch "
                             "to entry.is_dir() changes symlink/junction and "
                             "error behaviour and needs its own tests")


class _RaiseOSError:
    """Marker: reaching this entry raises OSError out of the iterator."""


class _NeverRequested:
    """Marker: asking for this entry means scanning continued past the winner."""


class _ControlledEntries:
    def __init__(self, owner, root, names):
        self.owner, self.root, self.names, self.index = owner, root, list(names), 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.owner.closed.append(self.root)
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= len(self.names):
            raise StopIteration
        name = self.names[self.index]
        self.index += 1
        self.owner.requested.append((self.root, name))
        if name is _RaiseOSError:
            raise OSError("directory enumeration failed mid-root")
        if name is _NeverRequested:
            raise AssertionError(
                f"scanning continued past the confirmed winner in {self.root}")
        return _FakeDirEntry(name)


class _ControlledScandir:
    """An os.scandir replacement that records every root opened/closed and every
    entry actually requested. A root listed in `unavailable_fails` fails the test
    outright if it is ever opened."""

    def __init__(self, plan, unavailable_fails=()):
        self.plan = {os.path.normcase(k): v for k, v in plan.items()}
        self.unavailable_fails = {os.path.normcase(p) for p in unavailable_fails}
        self.opened, self.closed, self.requested = [], [], []

    def __call__(self, root):
        key = os.path.normcase(str(root))
        self.opened.append(key)
        if key in self.unavailable_fails:
            raise AssertionError(f"an unavailable later root was opened: {root}")
        if key not in self.plan:
            raise FileNotFoundError(root)
        return _ControlledEntries(self, key, self.plan[key])


class _Clock:
    """monotonic() walks `values`, then repeats the last one forever."""

    def __init__(self, values):
        self.values, self.calls = list(values), 0

    def monotonic(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class _Probe:
    """An os.path.isdir replacement: records probes, refuses forbidden names."""

    def __init__(self, dirs, forbidden=()):
        self.dirs = {os.path.normcase(d) for d in dirs}
        self.forbidden = set(forbidden)
        self.probed = []

    def __call__(self, path):
        name = os.path.basename(str(path))
        if name in self.forbidden:
            raise AssertionError(f"metadata was probed for {name} after the "
                                 "winner was already confirmed")
        self.probed.append(name)
        return os.path.normcase(str(path)) in self.dirs


class VisibleExactReturnsImmediatelyTests(unittest.TestCase):
    """A confirmed visible exact match must survive everything after it."""

    ROOT_A = os.path.join(os.sep + "roots", "a")
    ROOT_B = os.path.join(os.sep + "roots", "b")

    def setUp(self):
        saved = ff._PROJECT_ROOTS
        self.addCleanup(setattr, ff, "_PROJECT_ROOTS", saved)
        ff._PROJECT_ROOTS = [self.ROOT_A, self.ROOT_B]
        self.winner = os.path.join(self.ROOT_A, "Iplay")

    def _install(self, scan, probe, clock=None):
        patches = [patch.object(ff.os, "scandir", new=scan),
                   patch.object(ff.os.path, "isdir", new=probe)]
        if clock is not None:
            patches.append(patch.object(ff, "time", clock))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, scan, probe, clock=None):
        self._install(scan, probe, clock)
        return ff._find_local_project_result("Iplay")

    # --- A. a later root is unavailable ---------------------------------
    def test_match_in_the_first_root_never_opens_a_later_unavailable_root(self):
        scan = _ControlledScandir({self.ROOT_A: ["Iplay"]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        self.assertEqual((self.winner, True), self._run(scan, probe))
        self.assertEqual([os.path.normcase(self.ROOT_A)], scan.opened)
        self.assertEqual([os.path.normcase(self.ROOT_A)], scan.closed,
                         "the directory iterator must close normally on return")

    # --- B. the entry budget would be exhausted by continuing ------------
    def test_match_inside_the_entry_budget_does_not_request_another_entry(self):
        scan = _ControlledScandir(
            {self.ROOT_A: ["Iplay", _NeverRequested, _NeverRequested]},
            unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        with patch.object(ff._ff_directed, "_PROJECT_LOOKUP_MAX_ENTRIES", 1):
            self.assertEqual((self.winner, True), self._run(scan, probe))
        self.assertEqual([(os.path.normcase(self.ROOT_A), "Iplay")], scan.requested)

    # --- C. the deadline would be exhausted by continuing ----------------
    def test_match_before_the_deadline_does_not_consult_the_clock_again(self):
        # deadline set, root checked, winning entry checked -> 3 calls. A 4th
        # call (the NEXT entry's check) would read 5.0 and return None, False.
        clock = _Clock([0.0, 0.0, 0.0, 5.0])
        scan = _ControlledScandir({self.ROOT_A: ["Iplay", _NeverRequested]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        self.assertEqual((self.winner, True), self._run(scan, probe, clock))
        self.assertEqual(3, clock.calls)
        self.assertEqual([(os.path.normcase(self.ROOT_A), "Iplay")], scan.requested)

    # --- D. enumeration of the SAME root fails after the winner ----------
    def test_match_survives_an_enumeration_error_later_in_the_same_root(self):
        scan = _ControlledScandir({self.ROOT_A: ["Iplay", _RaiseOSError]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        self.assertEqual((self.winner, True), self._run(scan, probe))
        # No further iteration, and no fall-through into the unavailable root
        # (which `except OSError: continue` would otherwise have reached).
        self.assertEqual([(os.path.normcase(self.ROOT_A), "Iplay")], scan.requested)
        self.assertEqual([os.path.normcase(self.ROOT_A)], scan.opened)

    # --- E. a slow/blocking plausible candidate follows the winner -------
    def test_a_plausible_neighbour_after_the_winner_is_never_probed(self):
        scan = _ControlledScandir({self.ROOT_A: ["Iplay", "Iplay-backup"]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner, os.path.join(self.ROOT_A, "Iplay-backup")],
                       forbidden={"Iplay-backup"})
        self.assertEqual((self.winner, True), self._run(scan, probe))
        self.assertEqual(["Iplay"], probe.probed)

    # --- G. exhaustion BEFORE a winner is established stays honest -------
    def test_entry_budget_exhausted_before_any_match_refuses_the_inventory(self):
        """Unchanged contract: a partial inventory cannot prove absence."""
        scan = _ControlledScandir({self.ROOT_A: ["unrelated-mount", "Iplay"]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        with patch.object(ff._ff_directed, "_PROJECT_LOOKUP_MAX_ENTRIES", 1):
            self.assertEqual((None, False), self._run(scan, probe))
        self.assertEqual([], probe.probed,
                         "an unrelated name must never cost a metadata probe")

    def test_deadline_exhausted_before_any_match_refuses_the_inventory(self):
        clock = _Clock([0.0, 0.0, 5.0])
        scan = _ControlledScandir({self.ROOT_A: ["unrelated-mount", "Iplay"]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        self.assertEqual((None, False), self._run(scan, probe, clock))

    # --- the caller-facing wrapper, not only the tuple -------------------
    def test_the_public_wrapper_returns_the_same_early_match(self):
        scan = _ControlledScandir({self.ROOT_A: ["Iplay", _NeverRequested]},
                                  unavailable_fails=[self.ROOT_B])
        probe = _Probe([self.winner])
        with patch.object(ff._ff_directed, "_PROJECT_LOOKUP_MAX_ENTRIES", 1):
            self._install(scan, probe)
            self.assertEqual(self.winner, ff._find_local_project("Iplay"))

    # --- precedence is NOT weakened by the early return ------------------
    def test_a_prefix_candidate_never_takes_the_early_return_path(self):
        scan = _ControlledScandir({self.ROOT_A: ["Iplay-backup"],
                                   self.ROOT_B: ["Iplay"]})
        probe = _Probe([os.path.join(self.ROOT_A, "Iplay-backup"),
                        os.path.join(self.ROOT_B, "Iplay")])
        self.assertEqual((os.path.join(self.ROOT_B, "Iplay"), True),
                         self._run(scan, probe))

    def test_a_hidden_exact_candidate_never_takes_the_early_return_path(self):
        scan = _ControlledScandir({self.ROOT_A: [".iplay"],
                                   self.ROOT_B: ["Iplay"]})
        probe = _Probe([os.path.join(self.ROOT_A, ".iplay"),
                        os.path.join(self.ROOT_B, "Iplay")])
        self.assertEqual((os.path.join(self.ROOT_B, "Iplay"), True),
                         self._run(scan, probe))

    def test_a_hidden_exact_candidate_still_wins_when_it_is_all_there_is(self):
        scan = _ControlledScandir({self.ROOT_A: [".iplay", "iplay-backup"],
                                   self.ROOT_B: []})
        probe = _Probe([os.path.join(self.ROOT_A, ".iplay"),
                        os.path.join(self.ROOT_A, "iplay-backup")])
        self.assertEqual((os.path.join(self.ROOT_A, ".iplay"), True),
                         self._run(scan, probe))


if __name__ == "__main__":
    unittest.main(verbosity=2)
