#!/usr/bin/env python3
"""The two rules that let 47 regressions reach four repositories (2026-09-01).

Line-by-line review of every `chore(autoclean)` commit in sermonsmith,
genemap-discovery, GrantFlow and Ellie found 47 real regressions that no gate
caught. Lint caught 4 of them. The test suites caught 2. The other 41 required
reading the diff, and the diffs were never read because nothing asked anyone to.

Two of FlexFactor's own rules made that possible, and both are fixed here:

  A  `verification_is_real`'s SENTENCE named only the component that failed to
     bootstrap, so a repo whose node suite was fully runnable reported "Build
     verification: NOT AVAILABLE ... Fixes in this run were NOT build-verified"
     with no hint that anything had been verified at all. The VERDICT is
     correct and is deliberately unchanged - an unbootstrapped component really
     is unverified, and `False` is what makes the scorecard record
     `final_build = None`. A critical line that is unactionable and present on
     every run is one an operator learns to scroll past.

  B  The fix prompts stated exactly one correctness bar - "the project MUST
     still build" - and EVERY one of the 47 regressions satisfies it. The
     anti-weakening rule existed in this codebase but only ever reached Phase 0
     red-baseline repair, while test files were merely sorted last in the
     ordinary sweep, never excluded from it.

Offline: no provider, no network, no repository.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flexfactor_prodready_engine as eng  # noqa: E402


def _tc(ecosystem, root, *, build=True, needs_deps=True, installed=True,
        install=True):
    """A Toolchain shaped like the ones `detect_toolchains` really emits."""
    return eng.Toolchain(
        ecosystem=ecosystem, root=root, manager="m", marker="f",
        install=[["i"]] if install else [],
        build=[["b"]] if build else [],
        deps_installed=installed, build_needs_deps=needs_deps,
    )


class VerificationNoteNamesBothHalvesTests(unittest.TestCase):
    """The refusal is kept; the sentence stops implying nothing ran.

    Deliberately NOT changed: the boolean. Returning True on the strength of a
    sibling component's green suite would let a fix land in the unverified
    component, which is the overclaim this function exists to prevent, and
    `flexfactor_prodready_persistence_tests.test_swift_with_a_REAL_toolchain_is_not_foreign`
    pins that policy on purpose.
    """

    # `_host_can_build` is pinned in the tests below that exercise the NEW
    # branch. Left unpinned they would be host-dependent and, on this Windows
    # machine, would silently exercise the PRE-EXISTING `foreign` branch
    # instead: swift is not on PATH here, so `_host_can_build` returns False
    # and swift never reaches the missing-deps branch at all. That branch was
    # already correct; the defect is the one below it.
    def _all_buildable_here(self, chains):
        real = eng._host_can_build
        eng._host_can_build = lambda t: True
        try:
            return eng.verification_is_real(chains)
        finally:
            eng._host_can_build = real

    def test_the_measured_GrantFlow_shape_now_SAYS_node_was_verified(self):
        """java+node bootstrapped, one component not: the note names both.

        Measured 2026-08-31: GrantFlow's report said "Build verification: NOT
        AVAILABLE - dependencies not installed for swift:ios/App/CapApp-SPM ...
        Fixes in this run were NOT build-verified", beside "Dependency
        bootstrap: 1/2 install step(s) succeeded", while node held that
        repository's 8242-test vitest suite and 3098-test node suite and both
        ran fine.
        """
        ok, note = self._all_buildable_here([
            _tc("node", "."),
            _tc("java", "android"),
            _tc("swift", "ios/App/CapApp-SPM", installed=False),
        ])
        self.assertIs(ok, False, "the refusal is deliberately unchanged")
        self.assertIn("swift:ios/App/CapApp-SPM", note)
        self.assertIn("IS bootstrapped and available for verification", note)
        self.assertNotIn("was verified", note)
        self.assertIn("node:.", note)

    def test_the_measured_Ellie_shape_does_the_same_with_a_different_gap(self):
        """Same defect, different missing toolchain - so it is not swift-specific."""
        ok, note = self._all_buildable_here([
            _tc("node", "."),
            _tc("python", "."),
            _tc("java", "android", installed=False),
        ])
        self.assertIs(ok, False)
        self.assertIn("java:android", note)
        self.assertIn("IS bootstrapped and available for verification", note)
        self.assertNotIn("was verified", note)

    def test_the_UNVERIFIED_component_is_still_NAMED_not_quietly_dropped(self):
        """Scoping the claim is the point; hiding the gap would be worse."""
        _, note = eng.verification_is_real([
            _tc("node", "."),
            _tc("java", "android", installed=False),
        ])
        self.assertIn("java:android", note)
        self.assertIn("build gate would false-fail", note)

    def test_when_NOTHING_bootstrapped_the_gate_still_FAILS(self):
        """The honesty guard is preserved, not weakened."""
        ok, note = self._all_buildable_here([
            _tc("node", ".", installed=False),
            _tc("java", "android", installed=False),
        ])
        self.assertIs(ok, False)
        self.assertIn("dependencies not installed", note)

    def test_a_fully_bootstrapped_project_is_unchanged(self):
        ok, note = eng.verification_is_real([_tc("node", ".")])
        self.assertIs(ok, True)
        self.assertEqual(note, "build verification available")

    def test_no_build_system_still_fails(self):
        self.assertIs(eng.verification_is_real([])[0], False)
        self.assertIs(eng.verification_is_real([_tc("node", ".", build=False)])[0],
                      False)

    def test_nothing_verifiable_says_ONLY_the_refusal_and_claims_nothing(self):
        """With no bootstrapped component the note must not claim any verification."""
        ok, note = self._all_buildable_here([
            _tc("node", ".", installed=False),
        ])
        self.assertIs(ok, False)
        self.assertNotIn("was verified", note)

    def test_a_host_incapable_component_and_an_unbootstrapped_one_are_BOTH_named(self):
        """The two gaps are different facts and must not shadow each other."""
        chains = [_tc("node", "."),
                  _tc("java", "android", installed=False),
                  _tc("swift", "ios", installed=False)]
        real = eng._host_can_build
        eng._host_can_build = lambda t: t.ecosystem != "swift"
        try:
            ok, note = eng.verification_is_real(chains)
        finally:
            eng._host_can_build = real
        self.assertIs(ok, False)
        self.assertIn("java:android", note)
        self.assertIn("swift:ios", note)


class NeverWeakenRuleReachesTheFixPromptsTests(unittest.TestCase):
    """The rule must be ON the path that produced the damage, not beside it.

    This repo has hit the written-but-not-wired trap four times
    (`flexfactor_runstate`, the `set_phase` group, `_UI_EXPLORER_JS`, and the
    purpose-evidence runners). A rule that exists in a constant nothing sends
    to a model is the same shape, so these tests assert MEMBERSHIP in the two
    prompts the fix path actually uses.
    """

    def setUp(self):
        import flexfactor as ff
        self.ff = ff

    def test_the_rule_is_in_every_fix_prompt_including_structural(self):
        for name in ("FIX_SYSTEM", "FIX_EDITS_SYSTEM", "STRUCTURAL_FIX_SYSTEM"):
            with self.subTest(prompt=name):
                self.assertIn(self.ff.NEVER_WEAKEN_RULE, getattr(self.ff, name))

    def test_it_forbids_each_regression_class_that_was_actually_measured(self):
        rule = self.ff.NEVER_WEAKEN_RULE.lower()
        # GrantFlow 22898ede deleted an `expect(...).toBe('invalid')`.
        self.assertIn("expect", rule)
        # GrantFlow a1defc85 allowlisted the PII to make the PII gate green.
        self.assertIn("allowlist", rule)
        # GrantFlow CrawlCoverage.jsx relaxed 25%->30% / 10%->15%.
        self.assertIn("threshold", rule)
        # ReverseLookup/HamiltonSelectionToolbar replaced err.message with a
        # constant; genemap replaced a throw with console.error + a zero result.
        self.assertIn("diagnostic", rule)
        # BudgetDetail.jsx returned {error} instead of throwing.
        self.assertIn("thrown error into a returned", rule)
        # sermonsmith pages.config.js deleted an import still used at line 83.
        self.assertIn("remaining references", rule)

    def test_it_says_building_is_not_the_bar(self):
        """Every one of the 47 regressions still built. That was the whole hole."""
        self.assertIn("BUILDING IS NOT THE BAR", self.ff.NEVER_WEAKEN_RULE)

    def test_the_prompts_still_demand_partial_progress(self):
        """The fix must not turn into permission to refuse work.

        Weakening `PARTIAL PROGRESS IS MANDATORY` would trade this defect for
        the 6-hour $17.75 run that found 3,464 defects and fixed zero - the
        failure the exit-code-3 rule exists to prevent.
        """
        for name in ("FIX_SYSTEM", "FIX_EDITS_SYSTEM"):
            with self.subTest(prompt=name):
                self.assertIn("PARTIAL PROGRESS IS MANDATORY",
                              getattr(self.ff, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
