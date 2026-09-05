"""Paid mode must not exclude the owner's own paid Anthropic routes.

`_auto_activate_fcc_proxy` does not DELETE a real `ANTHROPIC_API_KEY` when it
turns on the free proxy -- it MOVES it to `FLEXFACTOR_FALLBACK_ANTHROPIC_KEY`
and blanks the original, and its own docstring calls that "a strict
improvement, not a loss of capability". The Anthropic client construction has
always honoured that move.

The usability FILTER did not. Measured live 2026-09-05 on a `--model-mode paid`
run against FreeAndClean:

    [rotation] ON: ... excluded ... 11x missing ANTHROPIC_API_KEY

11 is the entire `anthropic_api` backend. Paid mode was then left with the FCC
subscription routes alone -- all of which share ONE capacity allowance whose
limit is 1 -- so the run the owner had explicitly asked to be FAST serialized
behind a single lease instead.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import os
import unittest

import flexfactor as ff


class _Route:
    def __init__(self, auth_env="ANTHROPIC_API_KEY", api="anthropic",
                 backend="anthropic_api", model="claude-sonnet-5"):
        self.auth_env = auth_env
        self.api = api
        self.backend = backend
        self.model = model
        self.id = f"{backend}/{model}"
        self.base_url = ""
        self.cost_class = "paid-metered"
        self.tier = "frontier"
        self.quota_status = ""
        self.resets_at = ""


class _EnvSandbox(unittest.TestCase):
    """Never leak a fabricated key into the rest of the suite."""

    NAMES = ("ANTHROPIC_API_KEY", "FLEXFACTOR_FALLBACK_ANTHROPIC_KEY",
             "OPENAI_API_KEY")

    def setUp(self):
        self._prior = {n: os.environ.get(n) for n in self.NAMES}

    def tearDown(self):
        for n, v in self._prior.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


class DemotedKeyStillCountsTests(_EnvSandbox):
    def test_a_key_the_proxy_demoted_is_still_a_credential(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["FLEXFACTOR_FALLBACK_ANTHROPIC_KEY"] = "sk-demoted"
        self.assertEqual(ff._route_credential("ANTHROPIC_API_KEY"), "sk-demoted")

    def test_the_route_is_no_longer_excluded_as_credential_less(self):
        """The live symptom: '11x missing ANTHROPIC_API_KEY'."""
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["FLEXFACTOR_FALLBACK_ANTHROPIC_KEY"] = "sk-demoted"
        reason = ff._route_unusable_reason(_Route(), "paid")
        self.assertNotIn("missing ANTHROPIC_API_KEY", reason or "",
                         "paid mode still excludes the owner's own paid "
                         "Anthropic routes, leaving only the serialized "
                         "subscription allowance")

    def test_a_live_key_still_wins_over_the_fallback(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-live"
        os.environ["FLEXFACTOR_FALLBACK_ANTHROPIC_KEY"] = "sk-demoted"
        self.assertEqual(ff._route_credential("ANTHROPIC_API_KEY"), "sk-live")


class GenuinelyMissingIsStillMissingTests(_EnvSandbox):
    """The fix must not admit a route that cannot authenticate."""

    def test_no_key_anywhere_is_still_missing(self):
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ.pop("FLEXFACTOR_FALLBACK_ANTHROPIC_KEY", None)
        self.assertEqual(ff._route_credential("ANTHROPIC_API_KEY"), "")
        self.assertIn("missing ANTHROPIC_API_KEY",
                      ff._route_unusable_reason(_Route(), "paid") or "")

    def test_the_fallback_is_anthropic_only(self):
        """A demoted Anthropic key says nothing about OpenAI."""
        os.environ["FLEXFACTOR_FALLBACK_ANTHROPIC_KEY"] = "sk-demoted"
        os.environ["OPENAI_API_KEY"] = ""
        self.assertEqual(ff._route_credential("OPENAI_API_KEY"), "")

    def test_a_whitespace_only_key_is_not_a_credential(self):
        os.environ["ANTHROPIC_API_KEY"] = "   "
        os.environ.pop("FLEXFACTOR_FALLBACK_ANTHROPIC_KEY", None)
        self.assertEqual(ff._route_credential("ANTHROPIC_API_KEY"), "")

    def test_a_route_with_no_auth_env_is_unaffected(self):
        self.assertEqual(ff._route_credential(""), "")


class CheckAndConstructionAgreeTests(_EnvSandbox):
    def test_every_route_the_filter_admits_can_be_given_a_key(self):
        """The defect was two halves of one decision disagreeing.

        If the filter admits a route, the client must be constructible with a
        real credential -- otherwise admitting it just buys an error tour.
        """
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["FLEXFACTOR_FALLBACK_ANTHROPIC_KEY"] = "sk-demoted"
        route = _Route()
        admitted = "missing" not in (ff._route_unusable_reason(route, "paid") or "")
        self.assertTrue(admitted)
        self.assertTrue(ff._route_credential(route.auth_env),
                        "admitted a route the client cannot authenticate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
