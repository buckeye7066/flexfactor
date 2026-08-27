"""Deterministic contracts for shared provider-capacity orchestration."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass

import flexfactor_capacity as cap


@dataclass
class Route:
    backend: str = "groq"
    cost_class: str = "free-tier"
    pool: str = "groq:model-a"
    id: str = "groq/model-a"
    tier: str = "strong"
    enabled: bool = True
    is_free: bool = True


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "capacity.json")
        self.store = cap.CapacityState(self.path)
        self.manager = cap.CapacityManager(self.store)
        self.route = Route()

    def tearDown(self):
        self.tmp.cleanup()

    def test_six_workers_share_one_allowance_without_stampede(self):
        """Regression: six programs cannot each pretend the same key is private."""
        active = 0
        peak = 0
        order = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def worker(i):
            nonlocal active, peak
            start.wait()
            lease = self.manager.acquire(self.route, app=f"app-{i}", timeout=5)
            try:
                with lock:
                    active += 1
                    peak = max(peak, active)
                    order.append(i)
                time.sleep(0.025)
            finally:
                with lock:
                    active -= 1
                self.manager.release(lease)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(1, peak, "one free allowance must never have six in-flight calls")
        self.assertEqual(6, len(order))
        snap = self.manager.snapshot()
        self.assertEqual({}, snap["leases"])
        self.assertEqual({}, snap["waiters"])

    def test_distinct_allowances_can_make_progress_together(self):
        other = Route(backend="gemini", pool="gemini:model-b", id="gemini/model-b")
        first = self.manager.acquire(self.route, app="a", timeout=1)
        second = self.manager.acquire(other, app="b", timeout=1)
        try:
            snap = self.manager.snapshot()
            self.assertEqual(2, len(snap["leases"]))
        finally:
            self.manager.release(second)
            self.manager.release(first)

    def test_429_cooldown_is_persistent_and_wait_state_is_not_app_error(self):
        self.manager.note_outcome(self.route, "rate_limited", 0.25, scope="account")
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        allowance = self.manager.allowance(self.route)
        self.assertGreater(raw["cooldowns"][allowance], time.time())
        self.assertEqual("waiting-for-provider", raw["runtime"]["state"])
        self.assertIn("rate_limited", raw["runtime"]["detail"])

        # A new manager/process view sees the same cooldown. It is not a
        # thread-local memory trick that disappears on restart.
        manager2 = cap.CapacityManager(cap.CapacityState(self.path))
        snap = manager2.snapshot()
        self.assertIn(allowance, snap["cooldowns"])

    def test_stale_lease_is_reclaimed_after_crash(self):
        now = time.time()
        data = cap._empty()
        data["leases"]["dead"] = {
            "allowance": self.manager.allowance(self.route),
            "app": "dead-app", "expires_at": now - 1,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        lease = self.manager.acquire(self.route, app="replacement", timeout=1)
        try:
            snap = self.manager.snapshot()
            self.assertNotIn("dead", snap["leases"])
            self.assertIn(lease.ident, snap["leases"])
        finally:
            self.manager.release(lease)

    def test_free_allowance_defaults_to_one_inflight(self):
        self.assertEqual(1, cap.CapacityManager.limit(self.route))
        local = Route(backend="ollama", cost_class="local-unlimited",
                      pool="ollama:local", id="ollama/qwen")
        self.assertEqual(2, cap.CapacityManager.limit(local))

    def test_timeout_removes_waiter(self):
        lease = self.manager.acquire(self.route, app="holder", timeout=1)
        try:
            with self.assertRaises(cap.CapacityTimeout):
                self.manager.acquire(self.route, app="blocked", timeout=0.05)
            self.assertFalse(any(row.get("app") == "blocked"
                                 for row in self.manager.snapshot()["waiters"].values()))
        finally:
            self.manager.release(lease)


if __name__ == "__main__":
    unittest.main()
