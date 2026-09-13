"""Owner subscriptions precede separately metered keys; no live calls."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
import flexfactor_rotation as R

class SubscriptionPriorityTests(unittest.TestCase):
    def test_both_subscriptions_precede_stronger_metered_models(self):
        def route(name, cost, tier):
            return R.Route(id=name, backend=name, backend_label=name,
                model=name, wire_model=name, api="openai", base_url="http://unused",
                pool=name, cost_class=cost, tier=tier)
        routes = [route("api", R.PAID_METERED, R.FRONTIER),
                  route("codex", R.SUBSCRIPTION, R.STRONG),
                  route("claude", R.SUBSCRIPTION, R.LIGHT),
                  route("local", R.LOCAL_UNLIMITED, R.FRONTIER)]
        with tempfile.TemporaryDirectory() as root:
            rotator = R.Rotator(R.Catalog(routes), R.StateStore(os.path.join(root,"state.json")))
            seen = []
            for now in range(100, 104):
                result = rotator.next_route(allow_paid=True, paid_first=True, now=now)
                seen.append(result.route.id)
                rotator.report(result.route, "quota_exhausted", retry_after_seconds=3600, now=now)
            self.assertEqual(seen, ["codex", "claude", "api", "local"])

if __name__ == "__main__":
    unittest.main()
