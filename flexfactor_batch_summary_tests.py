"""The batch summary must not call a FAILED run OK.

Found by running FlexFactor against FreeAndClean on 2026-09-05. The run aborted
on a route fault, reviewed 9 of 81 candidate files, fixed 0 of 19 defects, and
exited non-zero. Its own console said, three lines apart:

    FAILED: 1 program(s) (FreeAndClean) did not reach a verified complete state
    ...
    totals: 1/1 program(s) OK | 19 defect(s) found | 0 file(s) fixed

`ok` counted programs that raised no EXCEPTION, which is not what OK means
anywhere else in this tool. A supervisor, a scheduled task, or an owner reading
the last line would have recorded a successful night.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import io
import unittest

import flexfactor as ff


def result(**over) -> dict:
    """A program result that is a genuine success unless a test says otherwise."""
    base = {
        "name": "prog", "defects": 0, "fixed": 0, "test_status": True,
        "e2e_status": "skipped", "commit_status": "pushed", "error": None,
        "converged": True, "suite_status": True, "readiness_ready": True,
        "quality_gate_passed": True, "product_invariants_ready": True,
        "publication_required": False, "publication_complete": True,
        "review_ledger": {"candidates": 10, "acted_on": 10},
        "review_incomplete": 0,
    }
    base.update(over)
    return base


def summary_text(results, **kw) -> str:
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ff._print_batch_summary(results, **kw)
    return buf.getvalue()


class TheLiveFreeAndCleanRunTests(unittest.TestCase):
    """The exact shape that printed 1/1 OK."""

    def freeandclean(self) -> dict:
        return result(
            name="FreeAndClean", defects=19, fixed=0, test_status=None,
            commit_status="PROVIDER-OUTAGE ABORT on main",
            converged=False,
            review_ledger={"candidates": 81, "acted_on": 9},
        )

    def test_the_aborted_run_is_not_counted_OK(self):
        text = summary_text([self.freeandclean()])
        self.assertIn("totals: 0/1 program(s) OK", text)
        self.assertNotIn("1/1 program(s) OK", text)

    def test_the_summary_says_WHY_it_is_not_ok(self):
        """A bare 0/1 sends the reader back to scrollback."""
        text = summary_text([self.freeandclean()])
        self.assertIn("NOT OK:", text)
        self.assertIn("review did not converge", text)

    def test_the_exit_code_and_the_summary_agree(self):
        """The defect was two definitions of OK, so pin them together."""
        runs = [self.freeandclean()]
        self.assertNotEqual(ff._audit_exit_code(runs, apply_requested=True), 0)
        self.assertIn("totals: 0/1", summary_text(runs))

    def test_the_run_totals_are_still_reported_truthfully(self):
        text = summary_text([self.freeandclean()])
        self.assertIn("19 defect(s) found", text)
        self.assertIn("0 file(s) fixed", text)


class OKStillMeansOKTests(unittest.TestCase):
    """Guards the fix from being 'nothing is ever OK'."""

    def test_a_genuinely_clean_program_is_OK(self):
        text = summary_text([result()])
        self.assertIn("totals: 1/1 program(s) OK", text)
        self.assertNotIn("NOT OK", text)

    def test_a_clean_repo_with_zero_defects_is_not_called_barren(self):
        """0 fixed is correct when there was nothing to fix."""
        self.assertIsNone(ff._barren_reason(result(defects=0, fixed=0)))

    def test_a_program_that_fixed_what_it_found_is_OK(self):
        text = summary_text([result(defects=5, fixed=5)])
        self.assertIn("totals: 1/1 program(s) OK", text)

    def test_mixed_batch_counts_only_the_good_one(self):
        runs = [result(name="good"),
                result(name="bad", converged=False, defects=3, fixed=0)]
        text = summary_text(runs)
        self.assertIn("totals: 1/2 program(s) OK", text)


class EachFailureConditionIsNamedTests(unittest.TestCase):
    def test_every_incomplete_condition_produces_a_reason(self):
        for field, value in (("converged", False),
                             ("suite_status", False),
                             ("readiness_ready", False),
                             ("quality_gate_passed", False),
                             ("product_invariants_ready", False)):
            with self.subTest(field=field):
                self.assertIsNotNone(ff._incomplete_reason(result(**{field: value})))

    def test_required_but_incomplete_publication_is_a_reason(self):
        self.assertIsNotNone(ff._incomplete_reason(
            result(publication_required=True, publication_complete=False)))

    def test_an_unknown_publication_state_is_not_a_pass(self):
        """`None` is not True -- the tri-state rule this repo enforces."""
        self.assertIsNotNone(ff._incomplete_reason(
            result(publication_required=True, publication_complete=None)))

    def test_reviewing_zero_candidates_is_named_with_its_denominator(self):
        reason = ff._unreviewed_reason(
            result(review_ledger={"candidates": 81, "acted_on": 0}))
        self.assertIsNotNone(reason)
        self.assertIn("81", reason, "a numerator with no denominator again")

    def test_a_repo_with_no_candidates_is_not_flagged_unreviewed(self):
        self.assertIsNone(ff._unreviewed_reason(
            result(review_ledger={"candidates": 0, "acted_on": 0})))

    def test_an_errored_program_reports_the_error_and_nothing_else(self):
        reasons = ff._program_failure_reasons(
            result(error="boom", converged=False), apply_requested=True)
        self.assertEqual(len(reasons), 1)
        self.assertIn("boom", reasons[0])

    def test_barren_is_only_applied_when_apply_was_requested(self):
        r = result(defects=4, fixed=0)
        self.assertEqual(ff._program_failure_reasons(r, apply_requested=False), [])
        self.assertTrue(ff._program_failure_reasons(r, apply_requested=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
