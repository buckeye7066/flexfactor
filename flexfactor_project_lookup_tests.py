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


if __name__ == "__main__":
    unittest.main(verbosity=2)
