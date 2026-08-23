"""AUTO MODE: paid pools first for ONE attempt per call, then free.

Owner, 2026-08-23: "If auto mode is selected, then paid models first followed
by free. Only do one round, though. You should get what you need from that."

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import flexfactor_rotation as R


def route(rid, pool, cost):
    model = rid.split("/", 1)[1]
    return R.Route(id=rid, backend=rid.split("/")[0], backend_label="", model=model,
                   wire_model=model, api="openai", base_url="http://x", pool=pool,
                   cost_class=cost, tier=R.STRONG)


PAID = route("openai_api/gpt-5", "openai:api", R.PAID_METERED)
FREE_A = route("groq/llama", "groq:free", R.FREE_TIER)
FREE_B = route("nim/deepseek", "nim:free", R.FREE_TIER)


class _Base(unittest.TestCase):
    def setUp(self):
        self.store = R.StateStore(os.path.join(tempfile.mkdtemp(prefix="ff-paidfirst-"), "s.json"))

    def rot(self):
        return R.Rotator(R.Catalog([FREE_A, PAID, FREE_B]), self.store, app="test")


class PaidFirstOrdering(_Base):
    def test_paid_pool_ranks_ahead_of_free_when_asked(self):
        rot = self.rot()
        for _ in range(3):
            sel = rot.next_route(tier=R.STRONG, allow_paid=True, paid_first=True)
            self.assertEqual(sel.route.id, "openai_api/gpt-5")

    def test_without_paid_first_cheapest_first_is_unchanged(self):
        rot = self.rot()
        picks = {rot.next_route(tier=R.STRONG, allow_paid=True, now=100 + i).pool for i in range(3)}
        # LRU walks every pool, paid included (owner 2026-08-21), no preference
        self.assertEqual(picks, {"groq:free", "openai:api", "nim:free"})

    def test_paid_first_without_allow_paid_is_free_only(self):
        rot = self.rot()
        for i in range(3):
            self.assertTrue(rot.next_route(tier=R.STRONG, allow_paid=False, paid_first=True,
                                           now=100 + i).route.is_free)


class _Failing:
    """Provider double: the paid route fails, free routes answer."""
    def __init__(self, route): self.route = route
    def structured(self, *a, **k):
        if not self.route.is_free:
            raise RuntimeError("503 overloaded")        # retryable -> rotation continues
        return {"served_by": self.route.id}


class OnePaidRoundThenFree(_Base):
    def test_one_paid_attempt_then_free_pools(self):
        rot = self.rot()
        seen = []
        p = R.RotatingProvider(rot, _Failing, tier=R.STRONG, judge_tier=R.STRONG,
                               allow_paid=True, paid_first=True, on_route=seen.append)
        out = p.structured("s", "u", {})
        self.assertFalse(seen[0].route.is_free)            # first attempt: paid
        self.assertTrue(all(s.route.is_free for s in seen[1:]))   # then free only
        self.assertTrue(out["served_by"].startswith(("groq/", "nim/")))
        self.assertEqual(sum(1 for s in seen if not s.route.is_free), 1)   # exactly one paid round

    def test_every_call_gets_its_own_single_paid_round(self):
        # With a paid route that ANSWERS, each call is served by exactly one
        # paid attempt and never touches a free pool. (A paid route that fails
        # goes on the rotator's normal error cooldown, so the next call's paid
        # round finds no paid pool -- that is correct, and the test above
        # covers the fall-to-free half.)
        class _Working:
            def __init__(self, route): self.route = route
            def structured(self, *a, **k): return {"served_by": self.route.id}
        rot = self.rot()
        seen = []
        p = R.RotatingProvider(rot, _Working, tier=R.STRONG, judge_tier=R.STRONG,
                               allow_paid=True, paid_first=True, on_route=seen.append)
        p.structured("s", "u", {}); p.structured("s", "u", {})
        self.assertEqual([s.route.id for s in seen], ["openai_api/gpt-5", "openai_api/gpt-5"])

    def test_paid_first_is_inert_when_paid_is_not_allowed(self):
        rot = self.rot()
        seen = []
        p = R.RotatingProvider(rot, _Failing, tier=R.STRONG, judge_tier=R.STRONG,
                               allow_paid=False, paid_first=True, on_route=seen.append)
        p.structured("s", "u", {})
        self.assertTrue(all(s.route.is_free for s in seen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
