#!/usr/bin/env python3
"""Two model modes, and each one means exactly what it says.

Owner order 2026-08-24:

  "currently I am given three choices as for pay, local is a choice and auto is
   a choice. I don't fully understand the difference. My choices should be
   either paid or free. that's it. paid uses both anthropic and openai
   exclusively until credits expire and free uses free exclusively."

What the old three actually did, measured:
  - 'local' meant LOOPBACK ONLY - it excluded all 126 credentialed cloud
    free-tier routes and pinned the run to CPU-only Ollama. It was the
    launcher's DEFAULT, so the safe-sounding choice was the slowest one.
  - 'auto' was free-first with paid allowed to rotate in, and on the live
    2026-08-24 GrantFlow run produced spend_usd 0.0 with every free allowance
    exhausted and 0 of 3537 files reviewed - neither reliably free nor
    reliably paid.

The subtle one these tests exist to pin: `anthropic:max-plan` has cost_class
'subscription', which flexfactor_rotation.FREE_COST_CLASSES counts as FREE
(correctly - a flat-rate plan bills nothing extra per call). But the owner's
modes are about WHOSE ACCOUNT it is, so a Claude subscription belongs in 'paid'
where they asked for Anthropic - never smuggled into a run they asked to keep
free.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import flexfactor as ff
import flexfactor_rotation as rot

_ISOLATED = tempfile.mkdtemp(prefix="ffmode-")
ff.BRAIN_PATH = os.path.join(_ISOLATED, "brain.json")
ff.STATUS_PATH = os.path.join(_ISOLATED, "status.json")
ff.RUNS_PATH = os.path.join(_ISOLATED, "runs")


def _route(**kw):
    base = dict(id="x/y", backend="openrouter", backend_label="OpenRouter",
                model="m", wire_model="m", api="openai",
                base_url="https://openrouter.ai/api/v1", pool="p",
                auth_env="", cost_class="free-tier")
    base.update(kw)
    return rot.Route(**base)


class NormalizeTests(unittest.TestCase):
    def test_the_two_real_modes_pass_through(self):
        self.assertEqual(ff.normalize_model_mode("free"), "free")
        self.assertEqual(ff.normalize_model_mode("paid"), "paid")

    def test_the_retired_spellings_run_as_free(self):
        """They must not die on argparse exit 2 - the launcher-drift trap."""
        for old in ("auto", "local", "AUTO", " Local "):
            self.assertEqual(ff.normalize_model_mode(old), "free", old)

    def test_unknown_and_empty_default_to_free_not_paid(self):
        """The failure that costs money is the one that guesses 'paid'."""
        for bad in ("", None, "cheap", "whatever"):
            self.assertEqual(ff.normalize_model_mode(bad), "free", repr(bad))

    def test_only_two_modes_are_offered(self):
        self.assertEqual(tuple(ff.MODEL_MODES), ("free", "paid"))


class FreeModeExcludesEverythingBillable(unittest.TestCase):
    def test_free_tier_routes_are_admitted(self):
        for backend, api in (("nvidia_nim", "openai"), ("gemini", "gemini"),
                             ("groq", "openai"), ("cerebras", "openai"),
                             ("openrouter", "openai")):
            r = _route(backend=backend, api=api, cost_class="free-tier")
            self.assertEqual(ff.model_mode_refusal(r, "free"), "", backend)

    def test_local_ollama_is_admitted(self):
        r = _route(backend="ollama", api="ollama", cost_class="local-unlimited",
                   base_url="http://127.0.0.1:11434")
        self.assertEqual(ff.model_mode_refusal(r, "free"), "")

    def test_paid_metered_routes_are_EXCLUDED(self):
        """Excluded, not merely ordered last - a preference is not a promise."""
        for backend in ("openai_api", "anthropic_api", "openrouter"):
            r = _route(backend=backend, cost_class="paid-metered")
            why = ff.model_mode_refusal(r, "free")
            self.assertIn("excludes paid route", why, backend)

    def test_the_anthropic_SUBSCRIPTION_is_excluded_from_free(self):
        """rotation.FREE_COST_CLASSES calls this free (no marginal cost); the
        owner's modes are about whose account it is, and this is Anthropic's."""
        r = _route(backend="anthropic_sub", api="anthropic",
                   cost_class="subscription", pool="anthropic:max-plan")
        self.assertIn("subscription", rot.FREE_COST_CLASSES,
                      "precondition: the rotator does treat it as free")
        self.assertTrue(ff.model_mode_refusal(r, "free"),
                        "a paid Anthropic plan must not run in 'free' mode")


class PaidModeIsAnthropicAndOpenAIOnly(unittest.TestCase):
    def test_the_owners_CLI_subscriptions_belong_to_paid_not_to_nothing(self):
        """catalog.auto.json carries `claude-code` and `codex-cli`, which ARE the
        owner's Anthropic and OpenAI accounts reached through the local CLIs.

        They carry cost_class 'subscription', so FREE excludes them - correctly,
        by the same rule that keeps `anthropic:max-plan` out. If PAID did not
        also name their backends they would belong to NEITHER mode, and two
        whole route lanes would be retired by a change that never mentioned
        them. That is the failure this pins: not a wrong answer, a silently
        missing one.
        """
        for backend in ("claude-code", "codex-cli"):
            r = _route(backend=backend, api=backend, cost_class="subscription",
                       pool=f"{backend}:subscription", base_url="")
            self.assertEqual(ff.model_mode_refusal(r, "paid"), "", backend)
            self.assertTrue(ff.model_mode_refusal(r, "free"),
                            f"{backend} is a plan the owner PAYS for")

    def test_cursor_is_a_reseller_and_belongs_to_neither_mode(self):
        """The third row in catalog.auto.json. A Cursor seat is a subscription
        the owner holds, but it is not an Anthropic or OpenAI ACCOUNT - same
        reason openrouter credits are refused. Being in neither mode is the
        CORRECT answer here, and it is asserted so that a later well-meaning
        'restore the missing CLI lanes' fix cannot quietly readmit it."""
        r = _route(backend="cursor", api="cursor", cost_class="subscription",
                   pool="cursor:subscription", base_url="")
        self.assertTrue(ff.model_mode_refusal(r, "paid"))
        self.assertTrue(ff.model_mode_refusal(r, "free"))

    def test_the_owners_own_accounts_are_admitted(self):
        for backend, api in (("anthropic_sub", "anthropic"),
                             ("anthropic_api", "anthropic"),
                             ("openai_api", "openai")):
            r = _route(backend=backend, api=api, cost_class="paid-metered")
            self.assertEqual(ff.model_mode_refusal(r, "paid"), "", backend)

    def test_openrouter_credits_are_EXCLUDED_from_paid(self):
        """A reseller is not 'anthropic and openai'. 383 routes hang off this
        pool, so admitting it would quietly make 'paid' mean 'mostly not them'."""
        r = _route(backend="openrouter", cost_class="paid-metered",
                   pool="openrouter:credits")
        self.assertIn("Anthropic/OpenAI accounts", ff.model_mode_refusal(r, "paid"))

    def test_free_backends_are_excluded_from_paid(self):
        for backend in ("nvidia_nim", "groq", "cerebras", "gemini", "ollama"):
            r = _route(backend=backend, cost_class="free-tier")
            self.assertTrue(ff.model_mode_refusal(r, "paid"), backend)


class AgainstTheRealCatalog(unittest.TestCase):
    """The owner's actual routes.json - the numbers that decide a real run."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AITime", "routes.json")
        if not os.path.isfile(p):
            raise unittest.SkipTest(f"no live catalog at {p}")
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        raw = d if isinstance(d, list) else (d.get("routes") or [])
        cls.routes = []
        for r in raw:
            if not r.get("enabled", True):
                continue
            try:
                cls.routes.append(_route(
                    id=str(r.get("id") or ""), backend=str(r.get("backend") or ""),
                    backend_label=str(r.get("backend_label") or ""),
                    model=str(r.get("model") or ""), wire_model=str(r.get("wire_model") or ""),
                    api=str(r.get("api") or ""), base_url=str(r.get("base_url") or ""),
                    pool=str(r.get("pool") or ""), auth_env="",
                    cost_class=str(r.get("cost_class") or "")))
            except Exception:
                continue

    def _admitted(self, mode):
        # The dedicated seam: auth/capability filters are separate concerns and
        # must not be able to masquerade as a mode decision.
        out = []
        for r in self.routes:
            why = ff.model_mode_refusal(r, mode)
            if "model mode" not in why:
                out.append(r)
        return out

    def test_free_mode_admits_no_billable_route(self):
        for r in self._admitted("free"):
            self.assertNotEqual(r.cost_class, "paid-metered",
                                f"free mode admitted a metered route: {r.pool}/{r.id}")
            self.assertNotEqual(r.cost_class, "subscription",
                                f"free mode admitted a subscription: {r.pool}/{r.id}")

    def test_paid_mode_admits_only_anthropic_and_openai(self):
        for r in self._admitted("paid"):
            self.assertIn(r.backend, ff._PAID_MODE_BACKENDS,
                          f"paid mode admitted {r.backend} ({r.pool})")

    def test_both_modes_actually_have_capacity(self):
        """A mode that admits nothing is a broken mode, not a safe one."""
        self.assertGreater(len(self._admitted("free")), 50)
        self.assertGreater(len(self._admitted("paid")), 10)

    def test_the_two_modes_do_not_overlap(self):
        f = {id(r) for r in self._admitted("free")}
        p = {id(r) for r in self._admitted("paid")}
        self.assertEqual(f & p, set(), "a route may not be both free and paid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
