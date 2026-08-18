"""Tests for pool-first model rotation.

Every test here targets a way the rotator could look like it was spreading load
while actually draining one allowance, or could quietly spend money, or could
quietly ignore what the operator pinned. Those are the three failures that
matter; the rest is bookkeeping.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

import flexfactor_rotation as R


def route(rid: str, pool: str, tier: str = R.FRONTIER,
          cost: str = R.SUBSCRIPTION, enabled: bool = True,
          backend: str = "", model: str = "", **kw) -> R.Route:
    return R.Route(
        id=rid, backend=backend or rid.split("/")[0],
        backend_label=backend or rid.split("/")[0],
        model=model or rid.split("/", 1)[-1],
        wire_model=model or rid.split("/", 1)[-1],
        api="openai", base_url="https://example.invalid/v1",
        pool=pool, cost_class=cost, tier=tier, enabled=enabled, **kw)


def catalog(*routes: R.Route, age: float = 0.0) -> R.Catalog:
    return R.Catalog(routes=list(routes), generated_at="2026-08-18T00:00:00+00:00",
                     age_seconds=age, path="<test>")


class RotationTestCase(unittest.TestCase):
    """Each test gets its own state file so nothing leaks between them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, "rotation-state.json")
        self.store = R.StateStore(self.state_path)
        for var in ("AI_ROTATE", "AI_ROTATE_PIN", "AI_ROTATE_CATALOG",
                    "AI_ROTATE_STATE", "AITIME_STATE_DIR"):
            os.environ.pop(var, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rotator(self, cat: R.Catalog, app: str = "flexfactor") -> R.Rotator:
        return R.Rotator(catalog=cat, store=self.store, app=app)


# --------------------------------------------------------------------------- #
# The core claim: rotation spreads across LEDGERS, not model names
# --------------------------------------------------------------------------- #

class PoolFirstRotationTests(RotationTestCase):
    def test_consecutive_picks_walk_different_pools(self):
        rot = self.rotator(catalog(
            route("a/one", "pool-a"), route("b/one", "pool-b"), route("c/one", "pool-c")))
        pools = [rot.next_route().pool for _ in range(3)]
        self.assertEqual(sorted(pools), ["pool-a", "pool-b", "pool-c"])

    def test_many_models_on_one_ledger_do_not_fake_headroom(self):
        """The whole reason `pool` exists.

        Six OpenAI model ids and one Ollama model. If rotation walked ROUTES,
        OpenAI would take 6 of every 7 calls while looking balanced. Walking
        POOLS splits the load evenly between the two real ledgers.
        """
        routes = [route(f"openai_api/gpt-4o-2024-0{i}", "openai:paid",
                        cost=R.PAID_METERED) for i in range(6)]
        routes.append(route("ollama/qwen3-coder:30b", "local:ollama",
                            cost=R.LOCAL_UNLIMITED))
        rot = self.rotator(catalog(*routes))
        picks = [rot.next_route(allow_paid=True).pool for _ in range(10)]
        self.assertEqual(picks.count("local:ollama"), 5)
        self.assertEqual(picks.count("openai:paid"), 5)

    def test_least_recently_used_pool_wins(self):
        rot = self.rotator(catalog(route("a/one", "pool-a"), route("b/one", "pool-b")))
        first = rot.next_route(now=1000.0)
        second = rot.next_route(now=1001.0)
        self.assertNotEqual(first.pool, second.pool)
        third = rot.next_route(now=1002.0)
        self.assertEqual(third.pool, first.pool)

    def test_within_a_pool_the_least_recently_used_model_wins(self):
        rot = self.rotator(catalog(
            route("sub/claude-opus-5", "anthropic:max-plan"),
            route("sub/claude-fable-5", "anthropic:max-plan")))
        seen = {rot.next_route(now=1000.0 + i).route.id for i in range(2)}
        self.assertEqual(seen, {"sub/claude-opus-5", "sub/claude-fable-5"})


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #

class CostContainmentTests(RotationTestCase):
    def test_paid_routes_are_invisible_by_default(self):
        rot = self.rotator(catalog(
            route("free/local", "local:ollama", cost=R.LOCAL_UNLIMITED),
            route("paid/gpt-4o", "openai:paid", cost=R.PAID_METERED)))
        for _ in range(6):
            self.assertTrue(rot.next_route().route.is_free)

    def test_a_free_only_catalog_that_runs_dry_does_not_reach_for_paid(self):
        """The regression this guards is the expensive one: a $0 pipeline
        quietly turning into a billed one because everything free was busy."""
        rot = self.rotator(catalog(
            route("free/local", "local:ollama", cost=R.LOCAL_UNLIMITED,
                  enabled=False, disabled_reason="ollama offline"),
            route("paid/gpt-4o", "openai:paid", cost=R.PAID_METERED)))
        with self.assertRaises(R.RotationError) as ctx:
            rot.next_route()
        self.assertIn("allow_paid is off", str(ctx.exception))

    def test_the_error_names_the_paid_routes_it_withheld(self):
        rot = self.rotator(catalog(
            route("paid/gpt-4o", "openai:paid", cost=R.PAID_METERED),
            route("paid/opus", "anthropic:paid", cost=R.PAID_METERED)))
        with self.assertRaises(R.RotationError) as ctx:
            rot.next_route()
        self.assertIn("2 paid-metered", str(ctx.exception))

    def test_allow_paid_opens_the_gate_explicitly(self):
        rot = self.rotator(catalog(
            route("paid/gpt-4o", "openai:paid", cost=R.PAID_METERED)))
        self.assertEqual(rot.next_route(allow_paid=True).route.id, "paid/gpt-4o")

    def test_free_pools_are_preferred_before_paid_even_when_paid_is_allowed(self):
        rot = self.rotator(catalog(
            route("paid/gpt-4o", "openai:paid", cost=R.PAID_METERED),
            route("free/local", "local:ollama", cost=R.LOCAL_UNLIMITED)))
        # Both pools are unused, so ordering falls to calls then name; the free
        # pool must not be starved. Over six picks each ledger takes half.
        picks = [rot.next_route(allow_paid=True).route.cost_class for _ in range(6)]
        self.assertEqual(picks.count(R.LOCAL_UNLIMITED), 3)


# --------------------------------------------------------------------------- #
# Demotion
# --------------------------------------------------------------------------- #

class DemotionTests(RotationTestCase):
    def test_empty_frontier_falls_to_strong_and_says_so(self):
        rot = self.rotator(catalog(
            route("ollama/qwen3-coder:30b", "local:ollama", tier=R.STRONG,
                  cost=R.LOCAL_UNLIMITED)))
        pick = rot.next_route(tier=R.FRONTIER)
        self.assertEqual(pick.tier, R.STRONG)
        self.assertEqual(pick.demoted_from, R.FRONTIER)
        self.assertTrue(pick.demoted)
        self.assertIn("demoted from frontier", pick.describe())

    def test_demotion_walks_all_the_way_down_to_light(self):
        rot = self.rotator(catalog(
            route("ollama/phi3", "local:ollama", tier=R.LIGHT,
                  cost=R.LOCAL_UNLIMITED)))
        pick = rot.next_route(tier=R.FRONTIER)
        self.assertEqual(pick.tier, R.LIGHT)
        self.assertEqual(pick.demoted_from, R.FRONTIER)

    def test_demotion_never_promotes_a_cost_class(self):
        """Down a capability tier is allowed. Up into paid is not."""
        rot = self.rotator(catalog(
            route("paid/light", "openai:paid", tier=R.LIGHT, cost=R.PAID_METERED)))
        with self.assertRaises(R.RotationError):
            rot.next_route(tier=R.FRONTIER)

    def test_an_available_frontier_route_is_not_demoted(self):
        rot = self.rotator(catalog(
            route("sub/claude-opus-5", "anthropic:max-plan", tier=R.FRONTIER),
            route("ollama/phi3", "local:ollama", tier=R.LIGHT,
                  cost=R.LOCAL_UNLIMITED)))
        pick = rot.next_route(tier=R.FRONTIER)
        self.assertEqual(pick.tier, R.FRONTIER)
        self.assertIsNone(pick.demoted_from)


# --------------------------------------------------------------------------- #
# The operator's toggle
# --------------------------------------------------------------------------- #

class PinTests(RotationTestCase):
    def test_a_pin_overrides_rotation_entirely(self):
        rot = self.rotator(catalog(
            route("a/one", "pool-a"), route("b/grok", "pool-b", model="grok-4.6")))
        for _ in range(4):
            pick = rot.next_route(pin="b/grok")
            self.assertEqual(pick.route.id, "b/grok")
            self.assertTrue(pick.pinned)

    def test_a_pin_may_name_a_backend_a_pool_or_a_model(self):
        rot = self.rotator(catalog(
            route("openrouter/x-ai/grok-4.6", "openrouter:credits",
                  backend="openrouter", model="x-ai/grok-4.6",
                  cost=R.PAID_METERED)))
        for pin in ("openrouter", "openrouter:credits", "x-ai/grok-4.6",
                    "openrouter/x-ai/grok-4.6"):
            self.assertEqual(rot.next_route(pin=pin).route.model, "x-ai/grok-4.6", pin)

    def test_an_unavailable_pin_fails_loudly_instead_of_substituting(self):
        """A silent substitution is the defect this whole codebase keeps
        re-learning: the run reports success having done something else."""
        rot = self.rotator(catalog(
            route("pinned/dead", "pool-dead", enabled=False,
                  disabled_reason="quota exhausted (AI Time live meter)"),
            route("healthy/alive", "pool-alive")))
        with self.assertRaises(R.PinUnavailable) as ctx:
            rot.next_route(pin="pinned/dead")
        self.assertIn("quota exhausted", str(ctx.exception))

    def test_a_pin_naming_nothing_says_so_rather_than_rotating(self):
        rot = self.rotator(catalog(route("a/one", "pool-a")))
        with self.assertRaises(R.PinUnavailable) as ctx:
            rot.next_route(pin="typo/not-a-model")
        self.assertIn("matches no route", str(ctx.exception))

    def test_env_pin_is_honoured(self):
        os.environ["AI_ROTATE_PIN"] = "b/two"
        try:
            rot = self.rotator(catalog(route("a/one", "pool-a"),
                                       route("b/two", "pool-b")))
            self.assertEqual(rot.next_route().route.id, "b/two")
        finally:
            os.environ.pop("AI_ROTATE_PIN")

    def test_a_persisted_per_app_pin_beats_the_global_one(self):
        self.store.set_pin("a/one", app="global")
        self.store.set_pin("b/two", app="flexfactor")
        rot = self.rotator(catalog(route("a/one", "pool-a"), route("b/two", "pool-b")))
        self.assertEqual(rot.next_route().route.id, "b/two")

    def test_clearing_a_pin_restores_rotation(self):
        self.store.set_pin("b/two", app="flexfactor")
        rot = self.rotator(catalog(route("a/one", "pool-a"), route("b/two", "pool-b")))
        self.assertTrue(rot.next_route().pinned)
        self.store.set_pin(None, app="flexfactor")
        self.assertFalse(rot.next_route().pinned)

    def test_non_strict_pin_falls_through_when_asked(self):
        rot = self.rotator(catalog(
            route("pinned/dead", "pool-dead", enabled=False),
            route("healthy/alive", "pool-alive")))
        pick = rot.next_route(pin="pinned/dead", pin_strict=False)
        self.assertEqual(pick.route.id, "healthy/alive")


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #

class CooldownTests(RotationTestCase):
    def test_rate_limit_cools_the_whole_pool_not_just_the_model(self):
        """A 429 is the account's answer, not one model's."""
        hot = route("a/one", "pool-a")
        rot = self.rotator(catalog(hot, route("a/two", "pool-a"),
                                   route("b/one", "pool-b")))
        rot.report(hot, "rate_limited", retry_after_seconds=120, now=1000.0)
        for _ in range(4):
            self.assertEqual(rot.next_route(now=1001.0).pool, "pool-b")

    def test_a_cooldown_expires(self):
        hot = route("a/one", "pool-a")
        rot = self.rotator(catalog(hot, route("b/one", "pool-b")))
        rot.report(hot, "rate_limited", retry_after_seconds=60, now=1000.0)
        self.assertEqual(rot.next_route(now=1030.0).pool, "pool-b")
        pools = {rot.next_route(now=1100.0 + i).pool for i in range(4)}
        self.assertIn("pool-a", pools)

    def test_one_bad_model_does_not_take_its_provider_out_of_rotation(self):
        bad = route("a/broken", "pool-a")
        rot = self.rotator(catalog(bad, route("a/good", "pool-a")))
        rot.report(bad, "error", now=1000.0)
        self.assertEqual(rot.next_route(now=1001.0).route.id, "a/good")

    def test_three_strikes_cools_the_pool(self):
        bad = route("a/broken", "pool-a")
        rot = self.rotator(catalog(bad, route("b/one", "pool-b")))
        for _ in range(R.STRIKES_BEFORE_POOL_COOLDOWN):
            rot.report(bad, "error", now=1000.0)
        self.assertEqual(rot.next_route(now=1001.0).pool, "pool-b")

    def test_success_clears_strikes(self):
        flaky = route("a/flaky", "pool-a")
        rot = self.rotator(catalog(flaky, route("b/one", "pool-b")))
        rot.report(flaky, "error", now=1000.0)
        rot.report(flaky, "error", now=1001.0)
        rot.report(flaky, "ok", now=1002.0)
        rot.report(flaky, "error", now=1003.0)
        self.assertNotIn("pool-a", self.store.read()["cooldowns"])

    def test_quota_exhaustion_cools_until_the_reset_it_reported(self):
        dead = route("a/one", "pool-a",
                     resets_at="2026-08-18T01:00:00+00:00")
        rot = self.rotator(catalog(dead, route("b/one", "pool-b")))
        now = 1755478800.0  # 2026-08-18T00:00:00Z
        rot.report(dead, "quota_exhausted", now=now)
        until = self.store.read()["cooldowns"]["pool-a"]
        self.assertGreater(until, now)

    def test_quota_exhaustion_without_a_reset_uses_the_default(self):
        dead = route("a/one", "pool-a")
        rot = self.rotator(catalog(dead, route("b/one", "pool-b")))
        rot.report(dead, "quota_exhausted", now=1000.0)
        self.assertAlmostEqual(self.store.read()["cooldowns"]["pool-a"],
                               1000.0 + R.DEFAULT_QUOTA_COOLDOWN, places=1)

    def test_selection_counts_the_call_against_its_pool(self):
        """Calls are counted when the route is CHOSEN, not when it succeeds.

        A call in flight has already committed that pool's capacity, and
        counting at selection means the success path needs no second locked
        write -- worth ~68ms per call on this machine.
        """
        rot = self.rotator(catalog(route("a/one", "pool-a")))
        rot.next_route(now=1000.0)
        rot.next_route(now=1001.0)
        self.assertEqual(self.store.read()["pools"]["pool-a"]["calls"], 2)

    def test_a_clean_success_needs_no_second_write(self):
        good = route("a/one", "pool-a")
        rot = self.rotator(catalog(good))
        rot.next_route(now=1000.0)
        before = os.stat(self.state_path).st_mtime_ns
        rot.report(good, "ok", now=1001.0)
        self.assertEqual(os.stat(self.state_path).st_mtime_ns, before)

    def test_a_success_after_an_error_does_write_to_clear_the_strike(self):
        flaky = route("a/flaky", "pool-a")
        rot = self.rotator(catalog(flaky))
        rot.report(flaky, "error", now=1000.0)
        self.assertIn("a/flaky", self.store.read()["strikes"])
        rot.report(flaky, "ok", now=1001.0)
        self.assertNotIn("a/flaky", self.store.read()["strikes"])


# --------------------------------------------------------------------------- #
# Catalog handling
# --------------------------------------------------------------------------- #

class CatalogTests(RotationTestCase):
    def _write(self, payload) -> str:
        path = os.path.join(self._tmp.name, "routes.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_missing_catalog_yields_no_rotator_with_a_reason(self):
        path = os.path.join(self._tmp.name, "absent.json")
        self.assertIsNone(R.build_rotator(catalog_file=path))
        self.assertIn("run `python -m aitime.catalog`",
                      R.unavailable_reason(catalog_file=path))

    def test_wrong_schema_is_rejected_rather_than_guessed_at(self):
        path = self._write({"schema": 99, "routes": [{"id": "a/one"}]})
        self.assertIsNone(R.load_catalog(path))
        self.assertIn("wrong schema", R.unavailable_reason(catalog_file=path))

    def test_corrupt_catalog_is_not_fatal(self):
        path = os.path.join(self._tmp.name, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ truncated")
        self.assertIsNone(R.load_catalog(path))

    def test_one_malformed_row_does_not_void_the_catalog(self):
        path = self._write({"schema": 1, "routes": [
            {"nope": True},
            {"id": "a/one", "pool": "pool-a", "tier": "frontier",
             "cost_class": "subscription", "enabled": True},
        ]})
        cat = R.load_catalog(path)
        self.assertEqual(len(cat.routes), 1)

    def test_a_route_without_a_pool_falls_back_to_its_backend(self):
        """A blank pool would look like a private ledger and win every
        least-recently-used race, silently monopolising rotation."""
        path = self._write({"schema": 1, "routes": [
            {"id": "openai_api/gpt-4o", "backend": "openai_api", "enabled": True},
        ]})
        self.assertEqual(R.load_catalog(path).routes[0].pool, "openai_api:pool")

    def test_stale_catalog_still_routes_but_flags_itself(self):
        rot = self.rotator(catalog(route("a/one", "pool-a"),
                                   age=R.CATALOG_MAX_AGE_S + 60))
        pick = rot.next_route()
        self.assertTrue(pick.catalog_stale)
        self.assertIn("stale catalog", pick.describe())

    def test_all_routes_disabled_yields_no_rotator(self):
        path = self._write({"schema": 1, "routes": [
            {"id": "a/one", "pool": "pool-a", "enabled": False},
        ]})
        self.assertIsNone(R.build_rotator(catalog_file=path))
        self.assertIn("none are enabled", R.unavailable_reason(catalog_file=path))

    def test_rotation_off_disables_the_rotator_and_says_why(self):
        os.environ["AI_ROTATE"] = "off"
        try:
            self.assertFalse(R.rotation_enabled())
            self.assertIsNone(R.build_rotator())
            self.assertEqual(R.unavailable_reason(), "AI_ROTATE=off")
        finally:
            os.environ.pop("AI_ROTATE")

    def test_rotation_is_on_by_default(self):
        self.assertTrue(R.rotation_enabled())


# --------------------------------------------------------------------------- #
# Shared state across processes
# --------------------------------------------------------------------------- #

class SharedStateTests(RotationTestCase):
    def test_state_survives_a_new_store_instance(self):
        rot = self.rotator(catalog(route("a/one", "pool-a"), route("b/one", "pool-b")))
        first = rot.next_route(now=1000.0)
        fresh = R.Rotator(catalog=rot.catalog, store=R.StateStore(self.state_path))
        self.assertNotEqual(fresh.next_route(now=1001.0).pool, first.pool)

    def test_concurrent_writers_do_not_corrupt_the_file(self):
        """Three apps share this file. A torn write would break all of them."""
        cat = catalog(*[route(f"r/{i}", f"pool-{i}") for i in range(4)])
        errors = []

        def worker():
            try:
                rot = R.Rotator(catalog=cat, store=R.StateStore(self.state_path))
                for _ in range(15):
                    pick = rot.next_route()
                    rot.report(pick.route, "ok")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # A join that times out silently leaves threads writing into a temp dir
        # tearDown is about to delete, which shows up later as an unrelated
        # flake in whichever test runs next. Fail here, where the cause is.
        self.assertFalse([t for t in threads if t.is_alive()],
                         "worker threads did not finish within 60s")
        # 60 routed picks is a trivial amount of work; anything near a minute
        # means lock latency dominates, which would tax every real LLM call.
        self.assertLess(time.time() - started, 30.0,
                        "lock contention made 60 picks take over 30s")
        self.assertEqual(errors, [])
        with open(self.state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)          # must still parse
        self.assertEqual(data["schema"], R.SCHEMA)
        self.assertTrue(data["pools"])

    def test_concurrent_pickers_do_not_stampede_one_pool(self):
        """The failure a corruption-only test misses entirely.

        If read-select-stamp is not one locked transaction, every worker reads
        the same "least recently used" pool and picks it. The state file stays
        perfectly well-formed and the distribution looks plausible, while in
        fact one ledger absorbed the burst. Four pools, 40 picks: a fair split
        is 10 each, so nothing may exceed 15.
        """
        cat = catalog(*[route(f"r/{i}", f"pool-{i}") for i in range(4)])
        counts = {}
        errors = []
        lock = threading.Lock()

        def worker():
            rot = R.Rotator(catalog=cat, store=R.StateStore(self.state_path))
            for _ in range(10):
                try:
                    pool = rot.next_route().pool
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)
                    continue
                with lock:
                    counts[pool] = counts.get(pool, 0) + 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertFalse([t for t in threads if t.is_alive()],
                         "worker threads did not finish within 60s")
        self.assertEqual(errors, [], f"pickers raised: {errors}")
        self.assertEqual(sum(counts.values()), 40)
        self.assertEqual(len(counts), 4, f"some pool never used: {counts}")
        self.assertLessEqual(max(counts.values()), 15, counts)

    def test_a_stale_lock_is_broken_rather_than_wedging_rotation(self):
        lock = self.state_path + ".lock"
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write("99999")
        old = time.time() - (R.LOCK_STALE_S + 10)
        os.utime(lock, (old, old))
        rot = self.rotator(catalog(route("a/one", "pool-a")))
        # next_route always writes; report("ok") deliberately does not, so it
        # cannot demonstrate that the abandoned lock was broken.
        rot.next_route(now=1000.0)
        self.assertEqual(self.store.read()["pools"]["pool-a"]["calls"], 1)
        self.assertFalse(os.path.exists(lock), "the broken lock was not released")

    def test_no_leftover_temp_files(self):
        rot = self.rotator(catalog(route("a/one", "pool-a")))
        for i in range(5):
            rot.report(route("a/one", "pool-a"), "ok", now=1000.0 + i)
        leftovers = [f for f in os.listdir(self._tmp.name) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class EmptyCatalogTests(RotationTestCase):
    def test_an_empty_catalog_fails_loudly_with_the_fix(self):
        rot = self.rotator(catalog())
        with self.assertRaises(R.RotationError) as ctx:
            rot.next_route()
        self.assertIn("python -m aitime.catalog", str(ctx.exception))

    def test_the_failure_lists_why_pools_were_skipped(self):
        rot = self.rotator(catalog(
            route("a/one", "pool-a", enabled=False, disabled_reason="offline"),
            route("b/one", "pool-b", enabled=False, disabled_reason="key rejected")))
        with self.assertRaises(R.RotationError) as ctx:
            rot.next_route()
        message = str(ctx.exception)
        self.assertIn("offline", message)
        self.assertIn("key rejected", message)


# --------------------------------------------------------------------------- #
# The provider adapter
# --------------------------------------------------------------------------- #

class FakeProvider:
    """Stands in for AnthropicProvider / OpenAIProvider / OllamaProvider."""

    def __init__(self, route: R.Route, fail_with: BaseException | None = None):
        self.route = route
        self.model = route.model
        self.judge_model = route.model
        self.meter = None
        self.fail_with = fail_with
        self.calls = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with

    def complete(self, prompt=""):
        self._maybe_fail("complete")
        return f"completed by {self.route.id}"

    def structured(self, *a, **k):
        self._maybe_fail("structured")
        return {"by": self.route.id}

    def grade(self, *a, **k):
        self._maybe_fail("grade")
        return {"grade": 100, "by": self.route.id}

    def ping(self, *a, **k):
        self._maybe_fail("ping")
        return True


class Boom(Exception):
    def __init__(self, message="boom", status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class RotatingProviderTests(RotationTestCase):
    def _provider(self, cat, failures=None, **kw):
        failures = failures or {}
        rot = self.rotator(cat)
        return R.RotatingProvider(
            rot, lambda rt: FakeProvider(rt, failures.get(rt.id)), **kw)

    def test_successive_calls_land_on_different_pools(self):
        prov = self._provider(catalog(route("a/one", "pool-a"),
                                      route("b/one", "pool-b")))
        results = {prov.complete("x") for _ in range(2)}
        self.assertEqual(results, {"completed by a/one", "completed by b/one"})

    def test_grading_uses_the_cheap_tier_not_the_author_tier(self):
        prov = self._provider(catalog(
            route("front/big", "pool-front", tier=R.FRONTIER),
            route("light/small", "pool-light", tier=R.LIGHT)))
        self.assertEqual(prov.complete("x"), "completed by front/big")
        self.assertEqual(prov.grade()["by"], "light/small")

    def test_a_rate_limited_pool_is_skipped_on_the_next_call(self):
        prov = self._provider(
            catalog(route("a/one", "pool-a"), route("b/one", "pool-b")),
            failures={"a/one": Boom("rate limit exceeded", status_code=429)})
        # First call may hit a/one, fail, then rotate to b/one and succeed.
        self.assertEqual(prov.complete("x"), "completed by b/one")
        # a/one's whole pool is now cooling, so it is not retried.
        self.assertEqual(prov.complete("x"), "completed by b/one")

    def test_a_bad_request_is_raised_immediately_not_rotated_past(self):
        """A 400 stays a 400 on every backend. Rotating past it would burn
        every pool reproducing one bug and report 'all providers failed'."""
        prov = self._provider(
            catalog(route("a/one", "pool-a"), route("b/one", "pool-b")),
            failures={"a/one": Boom("invalid request", status_code=400),
                      "b/one": Boom("invalid request", status_code=400)})
        with self.assertRaises(Boom):
            prov.complete("x")

    def test_a_programming_error_is_not_retried_across_pools(self):
        prov = self._provider(
            catalog(route("a/one", "pool-a"), route("b/one", "pool-b")),
            failures={"a/one": TypeError("bad kwarg"),
                      "b/one": TypeError("bad kwarg")})
        with self.assertRaises(TypeError):
            prov.complete("x")

    def test_every_pool_failing_reports_the_last_real_error(self):
        prov = self._provider(
            catalog(route("a/one", "pool-a"), route("b/one", "pool-b")),
            failures={"a/one": Boom("overloaded", status_code=503),
                      "b/one": Boom("overloaded", status_code=503)})
        with self.assertRaises(R.RotationError) as ctx:
            prov.complete("x")
        self.assertIn("overloaded", str(ctx.exception))

    def test_the_shared_meter_is_pushed_onto_each_backing_provider(self):
        sentinel = object()
        prov = self._provider(catalog(route("a/one", "pool-a")), meter=sentinel)
        prov.complete("x")
        self.assertIs(prov._cache["a/one"].meter, sentinel)

    def test_model_reflects_the_route_actually_used(self):
        prov = self._provider(catalog(
            route("openrouter/x-ai/grok-4.6", "openrouter:credits",
                  model="x-ai/grok-4.6", cost=R.PAID_METERED)),
            allow_paid=True)
        prov.complete("x")
        self.assertEqual(prov.model, "x-ai/grok-4.6")

    def test_a_retry_after_header_sets_the_cooldown_length(self):
        prov = self._provider(
            catalog(route("a/one", "pool-a"), route("b/one", "pool-b")),
            failures={"a/one": Boom("rate limit", status_code=429,
                                    retry_after=900)})
        prov.complete("x")
        cooldowns = self.store.read()["cooldowns"]
        self.assertIn("pool-a", cooldowns)
        self.assertGreater(cooldowns["pool-a"] - time.time(), 600)

    def test_backing_providers_are_reused_not_rebuilt_per_call(self):
        built = []
        rot = self.rotator(catalog(route("a/one", "pool-a")))

        def factory(rt):
            built.append(rt.id)
            return FakeProvider(rt)

        prov = R.RotatingProvider(rot, factory)
        for _ in range(4):
            prov.complete("x")
        self.assertEqual(built, ["a/one"])


class ClassificationTests(unittest.TestCase):
    def test_429_is_rate_limited(self):
        self.assertEqual(R._classify(Boom("slow down", status_code=429)),
                         "rate_limited")

    def test_credit_language_is_quota_exhaustion(self):
        for message in ("insufficient credits", "quota exceeded",
                        "billing hard limit reached"):
            self.assertEqual(R._classify(Boom(message)), "quota_exhausted", message)

    def test_anything_else_is_a_plain_error(self):
        self.assertEqual(R._classify(Boom("segfault in the tokeniser")), "error")

    def test_interrupts_are_never_retried(self):
        self.assertFalse(R._is_retryable(KeyboardInterrupt()))
        self.assertFalse(R._is_retryable(SystemExit()))

    def test_retry_after_is_read_from_headers_too(self):
        exc = Boom("slow down", status_code=429)
        exc.headers = {"retry-after": "42"}
        self.assertEqual(R._retry_after(exc), 42.0)

    def test_a_garbage_retry_after_is_ignored_rather_than_crashing(self):
        exc = Boom("slow down", status_code=429)
        exc.headers = {"retry-after": "soon"}
        self.assertIsNone(R._retry_after(exc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
