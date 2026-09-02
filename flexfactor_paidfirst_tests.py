"""Best-available ladder: strongest paid capacity down to free.

Runs offline. No credentials, network, or token spend.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import flexfactor_rotation as R


def route(rid, pool, cost, tier):
    model = rid.split("/", 1)[1]
    return R.Route(
        id=rid, backend=rid.split("/")[0], backend_label="", model=model,
        wire_model=model, api="openai", base_url="http://x", pool=pool,
        cost_class=cost, tier=tier,
    )


PAID_FRONTIER = route(
    "anthropic_api/opus", "anthropic:api", R.PAID_METERED, R.FRONTIER
)
PAID_FRONTIER_2 = route(
    "openai_api/frontier", "openai:api", R.PAID_METERED, R.FRONTIER
)
PAID_STRONG = route(
    "copilot/auto", "copilot:subscription", R.SUBSCRIPTION, R.STRONG
)
FREE_STRONG = route(
    "ollama/qwen", "ollama:local", R.LOCAL_UNLIMITED, R.STRONG
)


class _Base(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="ff-paidfirst-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        self.store = R.StateStore(os.path.join(root, "state.json"))

    def rot(self):
        return R.Rotator(
            R.Catalog([
                PAID_FRONTIER, PAID_FRONTIER_2, PAID_STRONG, FREE_STRONG
            ]),
            self.store,
            app="test",
        )


class LadderOrdering(_Base):
    def test_healthy_best_paid_route_stays_first(self):
        rotator = self.rot()
        for now in (100, 101, 102):
            selected = rotator.next_route(
                tier=R.FRONTIER, allow_paid=True, paid_first=True, now=now
            )
            self.assertEqual(selected.route.id, PAID_FRONTIER.id)
            rotator.report(selected.route, "ok", now=now)

    def test_exhaustion_walks_every_paid_level_before_free(self):
        rotator = self.rot()
        observed = []
        for now in (100, 101, 102):
            selected = rotator.next_route(
                tier=R.FRONTIER, allow_paid=True, paid_first=True, now=now
            )
            observed.append(selected.route.id)
            rotator.report(
                selected.route, "quota_exhausted", retry_after_seconds=3600, now=now
            )
        final = rotator.next_route(
            tier=R.FRONTIER, allow_paid=True, paid_first=True, now=103
        )
        observed.append(final.route.id)
        self.assertEqual(observed, [
            PAID_FRONTIER.id,
            PAID_FRONTIER_2.id,
            PAID_STRONG.id,
            FREE_STRONG.id,
        ])

    def test_paid_capacity_is_not_available_when_spend_is_forbidden(self):
        rotator = self.rot()
        selected = rotator.next_route(
            tier=R.FRONTIER, allow_paid=False, paid_first=True, now=100
        )
        self.assertEqual(selected.route.id, FREE_STRONG.id)


class _Provider:
    def __init__(self, selected):
        self.selected = selected

    def structured(self, *args, **kwargs):
        if self.selected.uses_paid_capacity:
            raise RuntimeError("insufficient quota")
        return {"served_by": self.selected.id}


class RetryFallthrough(_Base):
    def test_one_call_descends_through_paid_pools_then_free(self):
        observed = []
        provider = R.RotatingProvider(
            self.rot(), _Provider, tier=R.FRONTIER, judge_tier=R.FRONTIER,
            allow_paid=True, paid_first=True, on_route=observed.append,
        )
        result = provider.structured("system", "prompt", {})
        self.assertEqual([selection.route.id for selection in observed], [
            PAID_FRONTIER.id,
            PAID_FRONTIER_2.id,
            PAID_STRONG.id,
            FREE_STRONG.id,
        ])
        self.assertEqual(result["served_by"], FREE_STRONG.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
