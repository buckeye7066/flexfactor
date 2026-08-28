"""Deterministic contracts for shared provider-capacity orchestration.

No network, credentials, or target repositories are used. The integration tests
exercise the real Rotator/RotatingProvider call path with constrained fake
backends so six program lanes cannot accidentally bypass the shared allowance.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass

import flexfactor_capacity as cap
import flexfactor_rotation as rotation


@dataclass
class Route:
    backend: str = "groq"
    cost_class: str = "free-tier"
    pool: str = "groq:model-a"
    id: str = "groq/model-a"
    tier: str = "strong"
    enabled: bool = True
    is_free: bool = True


def real_route(rid: str, pool: str) -> rotation.Route:
    model = rid.split("/", 1)[-1]
    return rotation.Route(
        id=rid, backend="groq", backend_label="Groq", model=model,
        wire_model=model, api="openai", base_url="https://example.invalid/v1",
        pool=pool, cost_class=rotation.SUBSCRIPTION, tier=rotation.STRONG,
        enabled=True,
    )


def real_catalog(route: rotation.Route) -> rotation.Catalog:
    return rotation.Catalog(
        routes=[route], generated_at="2026-08-27T00:00:00+00:00",
        age_seconds=0.0, path="<capacity-test>")


class CapacityTests(unittest.TestCase):
    def setUp(self):
        name = "FLEXFACTOR_PROVIDER_MAX_INFLIGHT"
        prior = os.environ.pop(name, None)
        if prior is None:
            self.addCleanup(os.environ.pop, name, None)
        else:
            self.addCleanup(os.environ.__setitem__, name, prior)
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

    def test_account_429_cooldown_is_persistent_and_wait_state_is_not_app_error(self):
        self.manager.note_outcome(self.route, "rate_limited", 0.25, scope="account")
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        allowance = self.manager.allowance(self.route)
        self.assertGreater(raw["cooldowns"][allowance], time.time())
        self.assertEqual("waiting-for-provider", raw["runtime"]["state"])
        self.assertIn("rate_limited", raw["runtime"]["detail"])

        manager2 = cap.CapacityManager(cap.CapacityState(self.path))
        snap = manager2.snapshot()
        self.assertIn(allowance, snap["cooldowns"])

    def test_pool_429_does_not_widen_into_account_cooldown(self):
        self.manager.note_outcome(self.route, "rate_limited", 30, scope="pool")
        allowance = self.manager.allowance(self.route)
        snap = self.manager.snapshot()
        self.assertNotIn(allowance, snap["cooldowns"])
        self.assertEqual("waiting-for-provider", snap["runtime"]["state"])

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


class RotatingProviderCapacityIntegrationTests(unittest.TestCase):
    """The real rotated call path must obey the same shared allowance."""

    def setUp(self):
        name = "FLEXFACTOR_PROVIDER_MAX_INFLIGHT"
        prior = os.environ.pop(name, None)
        if prior is None:
            self.addCleanup(os.environ.pop, name, None)
        else:
            self.addCleanup(os.environ.__setitem__, name, prior)
        self.tmp = tempfile.TemporaryDirectory()
        self.capacity_path = os.path.join(self.tmp.name, "capacity.json")
        self.original_manager = cap._MANAGER
        cap._MANAGER = cap.CapacityManager(cap.CapacityState(self.capacity_path))
        cap.install()

    def tearDown(self):
        cap._MANAGER = self.original_manager
        os.environ.pop("FLEXFACTOR_PROVIDER_WAIT_MAX_S", None)
        self.tmp.cleanup()

    def _provider(self, i, backing):
        route = real_route(f"groq/model-{i}", f"groq:pool-{i}")
        store = rotation.StateStore(os.path.join(self.tmp.name, f"rotation-{i}.json"))
        rotator = rotation.Rotator(real_catalog(route), store, app=f"app-{i}")
        return rotation.RotatingProvider(
            rotator, lambda _route: backing, tier=rotation.STRONG,
            allow_paid=False,
        )

    def test_six_real_rotating_providers_share_backend_allowance(self):
        active = 0
        peak = 0
        calls = 0
        guard = threading.Lock()
        barrier = threading.Barrier(6)

        class Backing:
            def complete(inner, *args, **kwargs):
                nonlocal active, peak, calls
                with guard:
                    active += 1
                    calls += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.025)
                    return "ok"
                finally:
                    with guard:
                        active -= 1

        providers = [self._provider(i, Backing()) for i in range(6)]
        results = [None] * 6

        def worker(i):
            barrier.wait()
            results[i] = providers[i].complete("system", "prompt")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(["ok"] * 6, results)
        self.assertEqual(6, calls)
        self.assertEqual(1, peak,
                         "six RotatingProvider instances bypassed the shared allowance")

    def test_retryable_429_waits_recovers_and_does_not_pollute_target_error_hook(self):
        os.environ["FLEXFACTOR_PROVIDER_WAIT_MAX_S"] = "3"
        errors = []
        calls = 0

        class RateLimited(Exception):
            status_code = 429
            headers = {"Retry-After": "0.05"}

        class Backing:
            def complete(inner, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RateLimited("rate limit reached; retry later")
                return "recovered"

        route = real_route("groq/recovery", "groq:recovery")
        store = rotation.StateStore(os.path.join(self.tmp.name, "rotation-recovery.json"))
        provider = rotation.RotatingProvider(
            rotation.Rotator(real_catalog(route), store, app="recovery-app"),
            lambda _route: Backing(), tier=rotation.STRONG,
            allow_paid=False, on_error=lambda _route, exc: errors.append(exc),
        )
        self.assertEqual("recovered", provider.complete("system", "prompt"))
        self.assertEqual(2, calls)
        self.assertEqual([], errors,
                         "temporary provider exhaustion leaked into the target-app error ledger")


class DirectedStatusSemanticsTests(unittest.TestCase):
    def test_partial_run_is_not_done_and_excess_programs_are_queued(self):
        import flexfactor_directed as directed

        class Progress:
            def __init__(self):
                self.rows = []
            def update(self, index, **kwargs):
                self.rows.append((index, kwargs))

        progress = Progress()
        seen = []
        def run_audit(args):
            seen.append(args.parallel)
            return args.parallel
        module_globals = {
            "_PROGRESS": progress,
            "run_audit": run_audit,
            "_route_unusable_reason": lambda route, mode: "",
            "_existing_failure_path": lambda project, raw: raw,
            "_SKIP_DIRS": set(),
        }
        prior = cap.recommended_program_parallelism
        cap.recommended_program_parallelism = lambda requested, mode: min(2, requested)
        try:
            directed.install(module_globals)
            args = type("Args", (), {"parallel": 6, "model_mode": "free"})()
            self.assertEqual(2, module_globals["run_audit"](args))
            self.assertEqual([2], seen)
            progress.update(1, phase="done - partial", done=True)
        finally:
            cap.recommended_program_parallelism = prior
        _, row = progress.rows[-1]
        self.assertFalse(row["done"])
        self.assertEqual("review complete - repairs/verification pending", row["phase"])


if __name__ == "__main__":
    unittest.main()
