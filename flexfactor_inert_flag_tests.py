"""A retired route flag must say it is not enforced -- at the START of the run.

Measured 2026-09-05. The owner explicitly authorised a paid path and the run
was launched with `--model-mode paid --paid-models both`. Neither is enforced
any more: `MODEL_MODES` is `("best",)` and `model_mode_refusal()` returns ""
for every route, and no route filter reads `paid_models` at all.

The run went on using the flat-rate subscription -- correct for the ladder, but
NOT what was asked -- while `_write_run_manifest` filed both values under a
comment reading "the request itself has to be in the immutable evidence". The
evidence therefore recorded a choice the run never made.

The one place the retirement WAS mentioned is `normalize_model_mode`, which the
audit path reaches at manifest-write time: after the run is over, which is too
late to change the decision.

Deleting the flags is not the fix -- that is argparse exit 2 for every existing
launcher and scheduled task (the documented launcher-drift trap). They keep
working, and they say what they are.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import contextlib
import io
import unittest

import flexfactor as ff


def warn(argv):
    """Return (named_flags, stderr_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        named = ff._warn_inert_route_flags(argv)
    return named, buf.getvalue()


class ItNamesTheFlagsThatWillNotBeHonouredTests(unittest.TestCase):
    def test_the_exact_live_invocation_is_flagged(self):
        named, err = warn(["prodready", "--program", "X", "--trust-repo",
                           "--yes", "--model-mode", "paid",
                           "--paid-models", "both", "--economy"])
        self.assertEqual(named, ["--model-mode", "--paid-models"])
        self.assertIn("RETIRED", err)
        self.assertIn("NOT enforced", err)

    def test_the_equals_form_is_caught_too(self):
        """`--model-mode=paid` is the same request typed differently."""
        named, _ = warn(["prodready", "--model-mode=paid"])
        self.assertEqual(named, ["--model-mode"])

    def test_it_says_what_the_REAL_lever_is(self):
        """A warning that only says 'no' sends the reader nowhere."""
        _, err = warn(["prodready", "--model-mode", "paid"])
        self.assertIn("AI_ROTATE_CATALOG", err)
        self.assertIn("FLEXFACTOR_PROVIDER_MAX_INFLIGHT", err)

    def test_a_flag_is_named_once_even_if_repeated(self):
        named, _ = warn(["prodready", "--model-mode", "paid",
                         "--model-mode", "free"])
        self.assertEqual(named, ["--model-mode"])


class SilenceWhenNothingWasAskedForTests(unittest.TestCase):
    """The warning must not become noise on every ordinary run."""

    def test_an_invocation_without_them_warns_nothing(self):
        named, err = warn(["prodready", "--program", "X", "--yes", "--economy"])
        self.assertEqual(named, [])
        self.assertEqual(err, "")

    def test_empty_and_none_argv_are_safe(self):
        for argv in ([], None):
            named, err = warn(argv)
            self.assertEqual(named, [])
            self.assertEqual(err, "")

    def test_a_value_that_merely_looks_like_a_flag_is_not_matched(self):
        """A program path or goal string must not trip it."""
        named, _ = warn(["prodready", "--program", "--model-mode-notes.md",
                         "--goal", "describe --paid-models behaviour"])
        self.assertEqual(named, [])


class TheDefaultsAreNotAChoiceTests(unittest.TestCase):
    def test_a_defaulted_value_is_not_reported_as_requested(self):
        """This is why the check reads RAW argv, not the parsed args.

        argparse gives `model_mode='best'` and `paid_models='both'` whether or
        not the owner typed them. Warning on the parsed value would fire on
        every run and mean nothing.
        """
        named, err = warn(["prodready", "--program", "X"])
        self.assertEqual(named, [])
        self.assertNotIn("RETIRED", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
