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
        self._prior_extensions = os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS")
        # These core catalog tests supply their own fixtures. Keep the default-on
        # discovered catalog from being merged into them; extension discovery has
        # its own dedicated suite.
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "0"
        for var in ("AI_ROTATE", "AI_ROTATE_PIN", "AI_ROTATE_CATALOG",
                    "AI_ROTATE_STATE", "AITIME_STATE_DIR"):
            os.environ.pop(var, None)

    def tearDown(self) -> None:
        if self._prior_extensions is None:
            os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        else:
            os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = self._prior_extensions
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
            # allow_paid=True: the pinned target is paid-metered, and a pin does
            # not override the cost boundary (see the test below).
            self.assertEqual(
                rot.next_route(pin=pin, allow_paid=True).route.model,
                "x-ai/grok-4.6", pin)

    def test_a_pin_to_a_paid_route_cannot_bypass_allow_paid(self):
        """The module header contract: a $0 call must never silently become a
        paid one. The pin can come from the SHARED state file (another app's
        'global' pin), so honoring it blind under allow_paid=False billed money
        on a free-mode run. The refusal is LOUD (PinUnavailable) and names the
        cost boundary, not a cooldown."""
        rot = self.rotator(catalog(
            route("openrouter/x-ai/grok-4.6", "openrouter:credits",
                  backend="openrouter", model="x-ai/grok-4.6",
                  cost=R.PAID_METERED)))
        with self.assertRaises(R.PinUnavailable) as ctx:
            rot.next_route(pin="openrouter", allow_paid=False)
        self.assertIn("paid", str(ctx.exception).lower())
        self.assertNotIn("cooling", str(ctx.exception).lower())

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
        self.assertTrue(pick.catalog_stale,
                        "a stale catalog must still route AND still say so")
        # ...but NOT on the per-route line. `describe()` used to append
        # "stale catalog", and the caller prints one line per distinct route, so
        # a live 5-program run on 2026-08-19 emitted ~30 copies of one fact
        # about one FILE. The flag above is the machine-readable answer; the
        # human-readable one is `catalog_staleness_note()`, said once per run.
        self.assertNotIn("stale", pick.describe().lower())
        self.assertIn("a/one", pick.describe())

    def test_the_stale_catalog_warning_is_actionable_and_said_once(self):
        """Neither suppress it nor repeat it: name the file, its age, and the
        exact refresh command. FlexFactor never runs that command itself --
        AI Time owns the catalog."""
        fresh = catalog(route("a/one", "pool-a"), age=60.0)
        self.assertIsNone(R.catalog_staleness_note(fresh))
        self.assertIsNone(R.catalog_staleness_note(None))
        stale = catalog(route("a/one", "pool-a"), age=R.CATALOG_MAX_AGE_S + 3600)
        stale.path = "X:/routes.json"
        note = R.catalog_staleness_note(stale)
        self.assertIn("X:/routes.json", note)
        self.assertIn("4.0h", note)
        self.assertIn("python -m aitime.catalog", note)

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

    def test_legacy_signature_fallback_cannot_drop_strict_family_policy(self):
        class LegacyRotator:
            def __init__(self):
                self.catalog = catalog(route("a/gpt", "pool-a", model="gpt-5"))

            def next_route(self, tier, allow_paid):
                return self.unexpected_call(tier, allow_paid)

            def unexpected_call(self, *_args):
                raise AssertionError("strict metadata was silently discarded")

        provider = R.RotatingProvider(
            LegacyRotator(), lambda selected: FakeProvider(selected),
            allow_paid=True,
        )
        intent = R.CallIntent(R.ROLE_REVIEWER, avoid_families=("gpt",))
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
            provider.complete("x", intent=intent)

    def test_legacy_signature_fallback_cannot_drop_paid_first_policy(self):
        class LegacyRotator:
            def __init__(self):
                self.catalog = catalog(route("a/gpt", "pool-a"))

            def next_route(self, tier, allow_paid):
                raise AssertionError("paid-first policy was silently discarded")

        provider = R.RotatingProvider(
            LegacyRotator(), lambda selected: FakeProvider(selected),
            allow_paid=True, paid_first=True,
        )
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
            provider.complete("x")

    def test_grading_uses_the_cheap_tier_not_the_author_tier(self):
        prov = self._provider(catalog(
            route("front/big", "pool-front", tier=R.FRONTIER),
            route("light/small", "pool-light", tier=R.LIGHT)))
        self.assertEqual(prov.complete("x"), "completed by front/big")
        self.assertEqual(prov.grade()["by"], "light/small")

    def test_independent_grader_proves_an_alternative_family_was_used(self):
        prov = self._provider(catalog(
            route("front/gpt-5.6-sol", "openai-front", tier=R.FRONTIER,
                  model="gpt-5.6-sol"),
            route("light/gpt-5.6-luna", "openai-light", tier=R.LIGHT,
                  model="gpt-5.6-luna"),
            route("light/claude-sonnet-5", "anthropic-light", tier=R.LIGHT,
                  model="claude-sonnet-5"),
        ))
        self.assertEqual(prov.complete("x"), "completed by front/gpt-5.6-sol")
        self.assertEqual(
            prov.grade_independent()["by"], "light/claude-sonnet-5"
        )

    def test_grader_fails_closed_when_only_an_author_family_exists(self):
        prov = self._provider(catalog(
            route("front/gpt-5.6-sol", "openai-front", tier=R.FRONTIER,
                  model="gpt-5.6-sol"),
            route("light/gpt-5.6-luna", "openai-light", tier=R.LIGHT,
                  model="gpt-5.6-luna"),
        ), judge_tier=R.LIGHT)
        self.assertEqual(prov.complete("x"), "completed by front/gpt-5.6-sol")
        with self.assertRaisesRegex(R.RotationError, "independent"):
            prov.grade_independent()

    def test_independent_grader_requires_a_recorded_author(self):
        prov = self._provider(catalog(
            route("light/claude", "anthropic-light", tier=R.LIGHT,
                  model="claude-sonnet-5"),
        ), tier=R.LIGHT, judge_tier=R.LIGHT)
        with self.assertRaisesRegex(R.RotationError, "recorded candidate author"):
            prov.grade_independent()

    def test_independent_grader_refuses_an_opaque_auto_author(self):
        prov = self._provider(catalog(
            route("front/copilot", "copilot", tier=R.FRONTIER, model="auto"),
            route("light/claude", "anthropic-light", tier=R.LIGHT,
                  model="claude-sonnet-4.6"),
        ))
        self.assertEqual(prov.complete("x"), "completed by front/copilot")
        with self.assertRaisesRegex(R.RotationError, "opaque author"):
            prov.grade_independent()

    def test_independent_grader_refuses_an_opaque_auto_reviewer(self):
        prov = self._provider(catalog(
            route("front/qwen", "qwen-front", tier=R.FRONTIER,
                  model="qwen3-coder"),
            route("light/auto", "opaque-light", tier=R.LIGHT, model="auto"),
        ))
        self.assertEqual(prov.complete("x"), "completed by front/qwen")
        with self.assertRaisesRegex(R.RotationError, "reviewer family"):
            prov.grade_independent()

    def test_separate_ladder_instances_share_author_identity(self):
        coordinator = R.RoleCoordinator()
        cat = catalog(
            route("front/claude-opus", "anthropic-front", tier=R.FRONTIER,
                  model="claude-opus-5"),
            route("light/claude-sonnet", "anthropic-light", tier=R.LIGHT,
                  model="claude-sonnet-5"),
            route("light/qwen", "open-light", tier=R.LIGHT,
                  model="qwen3-coder"),
        )
        author = R.RotatingProvider(
            self.rotator(cat), lambda rt: FakeProvider(rt),
            role_coordinator=coordinator)
        reviewer = R.RotatingProvider(
            self.rotator(cat), lambda rt: FakeProvider(rt),
            judge_tier=R.LIGHT, role_coordinator=coordinator)

        self.assertEqual(author.complete("x"), "completed by front/claude-opus")
        self.assertEqual(reviewer.grade()["by"], "light/qwen")

    def test_reviewer_avoids_every_family_that_authored_the_candidate(self):
        coordinator = R.RoleCoordinator()
        openai = route("light/gpt", "openai", tier=R.LIGHT,
                       model="gpt-5.6-luna")
        anthropic = route("light/claude", "anthropic", tier=R.LIGHT,
                          model="claude-sonnet-5")
        qwen = route("light/qwen", "open", tier=R.LIGHT, model="qwen3-coder")
        first_author = R.RotatingProvider(
            self.rotator(catalog(openai)), lambda rt: FakeProvider(rt),
            tier=R.LIGHT, judge_tier=R.LIGHT, role_coordinator=coordinator)
        second_author = R.RotatingProvider(
            self.rotator(catalog(anthropic)), lambda rt: FakeProvider(rt),
            tier=R.LIGHT, judge_tier=R.LIGHT, role_coordinator=coordinator)
        reviewer = R.RotatingProvider(
            self.rotator(catalog(openai, anthropic, qwen)),
            lambda rt: FakeProvider(rt), tier=R.LIGHT, judge_tier=R.LIGHT,
            role_coordinator=coordinator)
        first_author.complete("first")
        second_author.complete("second")
        self.assertEqual(coordinator.author_families, {"openai", "anthropic"})
        self.assertEqual(reviewer.grade()["by"], "light/qwen")

    def test_paid_mode_rotates_to_the_second_paid_pool_after_a_failure(self):
        """Owner order 2026-08-21: paid rotates until exhausted.

        The old one-paid-round logic revoked allow_paid on attempts 1..N. It
        was written for the retired `auto` mode, whose fallback was FREE
        routes; in `paid` mode the catalog holds ONLY paid backends, so a
        single metered failure filtered out every remaining pool and the call
        died with a wrong 'held back because allow_paid is off' diagnosis."""
        cat = catalog(
            route("anthropic_api/claude", "anthropic:paid", cost=R.PAID_METERED),
            route("openai_api/gpt", "openai:paid", cost=R.PAID_METERED))
        prov = self._provider(
            cat, failures={"anthropic_api/claude": Boom("overloaded", status_code=503)},
            allow_paid=True, paid_first=True)
        # Whichever paid pool is drawn first, the metered failure must hand the
        # call to the OTHER paid pool instead of stranding it.
        result = prov.complete("x")
        self.assertIn("completed by", result)

    def test_rotation_exhaustion_reports_the_real_provider_error(self):
        """When every pool has genuinely failed, the raise must carry the last
        provider error as its cause - a bare 'no route available' after a real
        failure is a confidently wrong diagnosis."""
        prov = self._provider(
            catalog(route("a/m", "pool-a")),
            failures={"a/m": Boom("secret upstream detail", status_code=503)})
        with self.assertRaises(R.RotationError) as ctx:
            prov.complete("x")
        chain = str(ctx.exception) + repr(ctx.exception.__cause__ or "")
        self.assertIn("secret upstream detail", chain,
                      "the real provider failure was discarded by the "
                      "rotation-exhaustion raise")

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


# --------------------------------------------------------------------------- #
# PR 1: Verification fail-closed — every failure mode blocks coverage assignment
# --------------------------------------------------------------------------- #

class VerificationFailClosedTests(RotationTestCase):
    """Prove that every provider verification failure blocks coverage assignment.

    "Verification" is any call that confirms a backing route is healthy —
    complete(), structured(), ping(), etc.  "Coverage assignment" is a
    successful return from one of those calls, routing work to that provider.

    Fail-closed means: a verification failure always raises.  It never
    silently returns a result as if the call succeeded through a broken route,
    and it never returns None as a stand-in for "no provider available".

    Every test here is written so that removing or relaxing the fail-closed
    invariants in _run() or _is_retryable() would cause it to fail.
    """

    # -- Helpers -----------------------------------------------------------

    def _provider(self, cat: R.Catalog, failures=None, **kw) -> R.RotatingProvider:
        failures = failures or {}
        rot = self.rotator(cat)
        return R.RotatingProvider(
            rot, lambda rt: FakeProvider(rt, failures.get(rt.id)), **kw)

    def _all_fail(self, failure: BaseException, n_pools: int = 2) -> R.RotatingProvider:
        """Return a RotatingProvider whose every route raises *failure*."""
        routes = [route(f"{chr(ord('a') + i)}/m",
                        f"pool-{chr(ord('a') + i)}")
                  for i in range(n_pools)]
        cat = catalog(*routes)
        return self._provider(cat, failures={r.id: failure for r in cat.routes})

    def _tracked_provider(self, cat: R.Catalog,
                          failure: BaseException,
                          call_log: list) -> R.RotatingProvider:
        """Provider that records which route id was attempted in *call_log*."""
        def factory(rt: R.Route) -> FakeProvider:
            fp = FakeProvider(rt, fail_with=failure)
            orig = fp.complete
            def logged(*a, **k):
                call_log.append(rt.id)
                return orig(*a, **k)
            fp.complete = logged
            return fp
        return R.RotatingProvider(self.rotator(cat), factory)

    # -- Network timeout ---------------------------------------------------

    def test_network_timeout_on_single_provider_routes_to_healthy_backup(self):
        """A transient 503/timeout on pool-a causes rotation to pool-b.

        The route that timed out is marked cooling so it is not reused
        immediately — the failure is recorded, never silently discarded.
        """
        prov = self._provider(
            catalog(route("a/m", "pool-a"), route("b/m", "pool-b")),
            failures={"a/m": Boom("timed out", status_code=503)})
        result = prov.complete("assign")
        self.assertIn("b/m", result,
                      "rotation must deliver the call to a live pool, not silently fail")
        cooldowns = self.store.read().get("cooldowns", {})
        self.assertIn("route:a/m", cooldowns,
                      "the timed-out route must enter cooldown, not be silently reused")

    def test_network_timeout_on_all_providers_raises_rotation_error(self):
        """All pools timing out must raise RotationError — never return None."""
        prov = self._all_fail(Boom("timed out", status_code=503))
        with self.assertRaises(R.RotationError):
            prov.complete("assign provider coverage")

    def test_network_timeout_never_returns_none(self):
        """complete() must raise when all pools time out, not silently return None."""
        prov = self._all_fail(Boom("gateway timeout", status_code=504))
        raised: list = []
        returned: list = []
        try:
            returned.append(prov.complete("x"))
        except Exception as exc:
            raised.append(exc)
        self.assertEqual(returned, [],
                         f"complete() returned a value through a timing-out provider: "
                         f"{returned}")
        self.assertTrue(raised, "complete() must raise on total timeout, not return None")

    # -- Invalid provider response -----------------------------------------

    def test_invalid_response_raises_immediately_not_silently_swallowed(self):
        """A parse/value error from the provider is never swallowed.

        ValueError is non-retryable: it must propagate immediately rather than
        rotating through all pools and returning a RotationError that hides the
        real cause.
        """
        prov = self._all_fail(ValueError("unexpected token in response"))
        with self.assertRaises(ValueError):
            prov.complete("assign provider coverage")

    def test_invalid_response_is_not_rotated_to_a_second_pool(self):
        """A non-transient parse error must not trigger pool rotation.

        If _is_retryable ever returned True for ValueError, two pools would be
        tried and this assertion on call_log length would fail.
        """
        call_log: list = []
        cat = catalog(route("a/m", "pool-a"), route("b/m", "pool-b"))
        prov = self._tracked_provider(
            cat, ValueError("response schema mismatch"), call_log)
        with self.assertRaises(ValueError):
            prov.complete("x")
        self.assertEqual(len(call_log), 1,
                         f"ValueError must not rotate to a second pool; "
                         f"attempted: {call_log}")

    def test_structured_call_fails_closed_on_parse_error(self):
        """structured(), used for typed verification calls, is equally fail-closed."""
        prov = self._all_fail(ValueError("unexpected token in JSON response"))
        with self.assertRaises(ValueError):
            prov.structured(schema={}, prompt="verify coverage")

    # -- Verification service unreachable ----------------------------------

    def test_service_unreachable_on_all_pools_raises(self):
        """All pools unreachable → an exception is raised, not a None return."""
        prov = self._all_fail(OSError("Connection refused"))
        with self.assertRaises((OSError, R.RotationError)):
            prov.complete("assign provider coverage")

    def test_service_unreachable_single_pool_rotates_to_backup(self):
        """One unreachable pool causes rotation to a healthy backup.

        A connection error is retryable (another pool may be reachable), but
        the work is still assigned to a real, live provider — it is never
        fabricated or returned empty.
        """
        prov = self._provider(
            catalog(route("a/m", "pool-a"), route("b/m", "pool-b")),
            failures={"a/m": OSError("Connection refused")})
        result = prov.complete("x")
        self.assertIn("b/m", result,
                      "work must arrive at a live pool after the unreachable one is skipped")

    def test_ping_failure_on_all_routes_raises_not_returns_false(self):
        """A verification ping that fails must raise — never return None or False."""
        prov = self._all_fail(Boom("service unavailable", status_code=503))
        with self.assertRaises((Boom, R.RotationError)):
            prov.ping()

    def test_unreachable_route_is_cooled_off_not_silently_retried(self):
        """After a connection error the route enters cooldown.

        This proves the failure was recorded: a route that returns immediately
        from cooldown would be eligible for re-selection, but one properly
        cooled off is skipped on the next call.
        """
        prov = self._provider(
            catalog(route("a/m", "pool-a"), route("b/m", "pool-b")),
            failures={"a/m": OSError("Connection refused")})
        prov.complete("first call")          # a/m fails → b/m serves
        prov.complete("second call")         # a/m cooling → b/m serves again
        state = self.store.read()
        self.assertIn("route:a/m", state.get("cooldowns", {}),
                      "the unreachable route must remain in cooldown, not be recycled")

    # -- Credential mismatch -----------------------------------------------

    def test_credential_mismatch_blocks_assignment(self):
        """A 401 on the provider propagates as-is — it is never swallowed."""
        prov = self._all_fail(Boom("invalid API key", status_code=401), n_pools=1)
        with self.assertRaises(Boom) as ctx:
            prov.complete("assign provider coverage")
        self.assertIn("invalid API key", str(ctx.exception))

    def test_credential_mismatch_is_not_retried_across_pools(self):
        """A 401 must raise immediately — rotating through all pools is wrong.

        A bad API key is bad on every backend.  If _is_retryable ever returned
        True for a 401, both pools would be attempted and call_log would contain
        two entries; this assertion catches that regression.
        """
        call_log: list = []
        cat = catalog(route("a/m", "pool-a"), route("b/m", "pool-b"))
        prov = self._tracked_provider(
            cat, Boom("unauthorized", status_code=401), call_log)
        with self.assertRaises(Boom):
            prov.complete("x")
        self.assertEqual(len(call_log), 1,
                         f"a 401 must not rotate to a second pool; "
                         f"attempted: {call_log}")

    def test_forbidden_response_also_blocks_without_rotation(self):
        """A 403 (Forbidden) is treated identically to 401 — not retried."""
        call_log: list = []
        cat = catalog(route("a/m", "pool-a"), route("b/m", "pool-b"))
        prov = self._tracked_provider(
            cat, Boom("forbidden", status_code=403), call_log)
        with self.assertRaises(Boom):
            prov.complete("x")
        self.assertEqual(len(call_log), 1,
                         f"a 403 must not rotate to a second pool; "
                         f"attempted: {call_log}")

    # -- General fail-closed invariant -------------------------------------

    def test_failed_call_never_silently_returns_a_result(self):
        """complete() must raise on total failure — it must not return a value.

        The only safe outcomes are "a result from a live provider" or "an
        exception explaining what failed".  Returning None would look like
        success to callers that do not check the return type.
        """
        prov = self._all_fail(Boom("overloaded", status_code=503))
        with self.assertRaises(R.RotationError):
            result = prov.complete("x")
            self.fail(f"expected RotationError but got: {result!r}")

    def test_rotation_error_message_names_the_failure(self):
        """RotationError on total failure must carry the last error message.

        A RotationError that swallows the real cause is nearly as bad as
        not raising at all — the operator needs to know what broke.
        """
        prov = self._all_fail(Boom("service overloaded", status_code=503))
        with self.assertRaises(R.RotationError) as ctx:
            prov.complete("x")
        self.assertIn("overloaded", str(ctx.exception).lower())


class RouteRefusal403Tests(unittest.TestCase):
    """A 403 that names a per-route capability refusal must rotate; a plain
    403 (wrong key) must still fail fast."""

    class _Exc(Exception):
        def __init__(self, msg, status):
            super().__init__(msg)
            self.status_code = status

    def test_agentic_harness_403_rotates(self):
        exc = self._Exc("Error code: 403 - {'error': {'message': 'thinkingmachines/inkling:free "
                        "is only available on agentic harnesses. Try plugging it into a coding "
                        "agent', 'code': 403}}", 403)
        self.assertTrue(R.is_route_capability_error(exc))
        self.assertTrue(R._is_retryable(exc))

    def test_plain_403_still_fails_fast(self):
        exc = self._Exc("Error code: 403 - {'error': {'message': 'Forbidden'}}", 403)
        self.assertFalse(R._is_retryable(exc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
