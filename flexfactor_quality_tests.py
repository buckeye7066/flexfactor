"""Purpose effectiveness: the rotator learns from RESULTS, not just from serving.

`report()` says a call was served. `report_quality()` says whether the work
helped -- verified / rejected / noop / build_failed -- for a program purpose.
These tests pin that a high-yield route is preferred INSIDE the pool LRU
chose (pool rotation untouched), that a chronically off-purpose route is
cooled down for that purpose only with a visible reason, and that the
provider wrapper attributes results to the route that actually authored.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import flexfactor_rotation as R


def route(rid, pool, caps=(R.CAP_CODE_AUTHOR,)):
    model = rid.split("/", 1)[1]
    return R.Route(id=rid, backend=rid.split("/")[0], backend_label="", model=model,
                   wire_model=model, api="openai", base_url="http://x", pool=pool,
                   cost_class=R.FREE_TIER, tier=R.STRONG, capabilities=tuple(caps),
                   capabilities_source="measured")


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ff-quality-")
        self.store = R.StateStore(os.path.join(self.dir, "state.json"))

    def rot(self, routes):
        return R.Rotator(R.Catalog(routes), self.store, app="test")


class YieldInsideThePool(_Base):
    def test_verified_work_wins_the_pool_over_lru(self):
        good, bad = route("a/good", "pool-a"), route("a/bad", "pool-a")
        rot = self.rot([good, bad])
        for _ in range(3):
            rot.report_quality(good, "verified", "prog")
            rot.report_quality(bad, "rejected", "prog")
        intent = R.CallIntent(R.ROLE_AUTHOR, purpose="prog")
        picks = [rot.next_route(tier=R.STRONG, intent=intent).route.id for _ in range(4)]
        self.assertEqual(picks, ["a/good"] * 4)

    def test_pool_order_is_untouched_by_yield(self):
        # pool-b has the better route, but pool-a is least recently used:
        # pool-a still goes first. Quota spreading beats quality ordering.
        a, b = route("a/x", "pool-a"), route("b/y", "pool-b")
        rot = self.rot([a, b])
        for _ in range(4):
            rot.report_quality(b, "verified", "prog")
            rot.report_quality(a, "noop", "prog")
        intent = R.CallIntent(R.ROLE_AUTHOR, purpose="prog")
        first = rot.next_route(tier=R.STRONG, intent=intent, now=100).pool
        second = rot.next_route(tier=R.STRONG, intent=intent, now=101).pool
        self.assertEqual({first, second}, {"pool-a", "pool-b"})

    def test_no_history_is_neutral(self):
        self.assertEqual(R._yield({}), 0.5)
        self.assertLess(R._yield({"rejected": 1}), 0.5)
        self.assertGreater(R._yield({"verified": 1}), 0.5)


class ChronicOffenderCooldown(_Base):
    def test_low_yield_after_enough_attempts_cools_the_route_for_that_purpose(self):
        bad, ok = route("a/bad", "pool-a"), route("b/ok", "pool-b")
        rot = self.rot([bad, ok])
        note = None
        for _ in range(R.Rotator.QUALITY_MIN_ATTEMPTS):
            note = rot.report_quality(bad, "rejected", "prog", now=1000)
        self.assertIsNotNone(note)
        self.assertIn("cooled down", note)
        intent = R.CallIntent(R.ROLE_AUTHOR, purpose="prog")
        for _ in range(4):
            self.assertEqual(rot.next_route(tier=R.STRONG, intent=intent, now=1001).route.id, "b/ok")

    def test_cooldown_is_scoped_to_the_purpose(self):
        bad = route("a/bad", "pool-a")
        rot = self.rot([bad])
        for _ in range(R.Rotator.QUALITY_MIN_ATTEMPTS):
            rot.report_quality(bad, "rejected", "prog-one", now=1000)
        # Same route, a different program: still available.
        other = R.CallIntent(R.ROLE_AUTHOR, purpose="prog-two")
        self.assertEqual(rot.next_route(tier=R.STRONG, intent=other, now=1001).route.id, "a/bad")
        # And with no purpose at all.
        self.assertEqual(rot.next_route(tier=R.STRONG, now=1002).route.id, "a/bad")

    def test_cooldown_expires(self):
        bad = route("a/bad", "pool-a")
        rot = self.rot([bad])
        for _ in range(R.Rotator.QUALITY_MIN_ATTEMPTS):
            rot.report_quality(bad, "rejected", "prog", now=1000)
        intent = R.CallIntent(R.ROLE_AUTHOR, purpose="prog")
        with self.assertRaises(R.RotationError) as cm:
            rot.next_route(tier=R.STRONG, intent=intent, now=1001)
        self.assertIn("low yield", str(cm.exception.reasons))
        later = 1000 + R.Rotator.QUALITY_COOLDOWN_S + 1
        self.assertEqual(rot.next_route(tier=R.STRONG, intent=intent, now=later).route.id, "a/bad")

    def test_unknown_signal_is_recorded_not_raised(self):
        r = route("a/x", "pool-a")
        rot = self.rot([r])
        rot.report_quality(r, "weird", "prog")
        self.assertEqual(rot.quality_for(r, "prog").get("other"), 1)


class _FakeProvider:
    def __init__(self, route): self.route = route
    def structured(self, *a, **k): return {"ok": 1}


class ProviderAttributesResults(_Base):
    def test_result_is_attributed_to_the_route_that_authored(self):
        a, b = route("a/one", "pool-a"), route("b/two", "pool-b")
        rot = self.rot([a, b])
        seen = []
        p = R.RotatingProvider(rot, _FakeProvider, tier=R.STRONG, judge_tier=R.STRONG,
                               on_route=seen.append)
        p.set_purpose("prog")
        p.structured("s", "u", {}, intent=R.CallIntent(R.ROLE_AUTHOR))
        authored = seen[-1].route
        p.report_quality(R.ROLE_AUTHOR, "rejected")
        self.assertEqual(rot.quality_for(authored, "prog").get("rejected"), 1)
        other = b if authored.id == a.id else a
        self.assertEqual(rot.quality_for(other, "prog"), {})

    def test_report_without_a_prior_call_is_a_noop(self):
        rot = self.rot([route("a/one", "pool-a")])
        p = R.RotatingProvider(rot, _FakeProvider, tier=R.STRONG)
        self.assertIsNone(p.report_quality(R.ROLE_AUTHOR, "verified"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
