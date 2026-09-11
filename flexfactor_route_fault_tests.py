#!/usr/bin/env python3
"""Route-fault classification: the four shapes that stopped review reaching files.

MEASURED, from the two live 10-program audits of 2026-08-24
(`~/.flexfactor/runs/*-21424` and `*-16164`). EVERY ONE of the 13 programs
ended with the same line:

    review made no progress: three consecutive semantic review batches
    completed ZERO files (0 of 3537 candidate file(s) reviewed all run)

GrantFlow reviewed 0 of 3537 candidates; genemap 2 of 368; incognito 0 of 446;
sermonsmith 0 of 318; repo-rewards 0 of 84. The circuit breaker was right that
this was a route fault - but four of FlexFactor's own classification rules
turned individually-recoverable route failures into DEAD CALLS, and a dead call
is an INCOMPLETE file. Three batches of eight dead files is the breaker.

Each class below is one of those rules, with the ledger evidence that it fired:

1. `CliUnavailable` is not recognised as a route fault.
   30x `cli/codex: exited 1: ... "The 'gpt-5.6-sol' model is not supported when
   using Codex with a ChatGPT account."` and 23x `cli/claude-code: exceeded 600s
   and was killed` in local-ai-factory-20260824-005448-500119-21424. A CLI
   binary IS the route, so its failure can never be a property of the payload -
   yet `_is_retryable` said no, so the whole review call died on the spot
   instead of trying any of the other 640 routes. That run reviewed 2 of 287.

2. A structurally dead transport gets the ordinary 30s route cooldown.
   The codex CLI cannot serve this account AT ALL - it was re-selected 30 times.

3. An `EgressBlockedError` is charged to the route.
   Measured 3 distinct innocent routes struck per blocked payload, in five
   separate runs (e.g. repo-rewards 04:47:34 openrouter/anthropic/
   claude-opus-4.8-fast, nvidia_nim/nvidia/cosmos-reason2-8b, gemini/
   gemini-3.1-pro-preview - all within 2 seconds, all for the SAME
   `payload contains ['private_key'] (near line(s) [369])`). Three strikes cool
   a whole POOL for 300s, so a secret in the repo benches healthy providers.

4. An ollama HTTP 400's BODY is thrown away.
   3x `HTTPError: HTTP Error 400: Bad Request` on ollama/deepseek-r1:8b in the
   GrantFlow run, from `_chat`. Ollama is the one FREE, UNMETERED, un-rate-
   limitable reviewer on this machine; it failed on every attempt and the
   ledger cannot say why, because `urllib` discards the response body unless
   somebody reads it.

Offline. No credentials, no network, no tokens spent.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error

import flexfactor_rotation as R
import flexfactor_capacity as C

# TEST HYGIENE (house rule, see CLAUDE.md): never touch the real
# ~/.flexfactor. Redirected at IMPORT, before flexfactor is used for anything,
# because a test run has evicted the owner's real brain.json before.
_ISOLATED = tempfile.mkdtemp(prefix="ffroutefault-state-")
import flexfactor as ff  # noqa: E402

ff.BRAIN_PATH = os.path.join(_ISOLATED, "brain.json")
ff.STATUS_PATH = os.path.join(_ISOLATED, "status.json")
ff.RUNS_PATH = os.path.join(_ISOLATED, "runs")

from providers.cli_provider import CliUnavailable  # noqa: E402


CODEX_ACCOUNT_REFUSAL = (
    r"C:\Users\firer\AppData\Roaming\npm\codex.CMD: exited 1: "
    'hook: UserPromptSubmit\nhook: UserPromptSubmit Completed\n'
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-5.6-sol\' model is not supported when using Codex with '
    'a ChatGPT account."}}'
)
CLAUDE_CLI_TIMEOUT = r"C:\Users\firer\.local\bin\claude.EXE: exceeded 600s and was killed"


class StateIsolationTests(unittest.TestCase):
    def test_this_module_never_points_at_the_real_flexfactor_state(self):
        real = os.path.join(os.path.expanduser("~"), ".flexfactor")
        for p in (ff.BRAIN_PATH, ff.STATUS_PATH, ff.RUNS_PATH):
            self.assertFalse(
                os.path.normcase(str(p)).startswith(os.path.normcase(real)),
                f"{p} points at the owner's real state directory")


def route(rid: str, pool: str, tier: str = R.FRONTIER,
          cost: str = R.SUBSCRIPTION, api: str = "openai") -> R.Route:
    return R.Route(
        id=rid, backend=rid.split("/")[0], backend_label=rid.split("/")[0],
        model=rid.split("/", 1)[-1], wire_model=rid.split("/", 1)[-1],
        api=api, base_url="https://example.invalid/v1",
        pool=pool, cost_class=cost, tier=tier, enabled=True)


def catalog(*routes: R.Route) -> R.Catalog:
    return R.Catalog(routes=list(routes), generated_at="2026-08-24T00:00:00+00:00",
                     age_seconds=0.0, path="<test>")


class _Fake:
    def __init__(self, rt, fail_with=None):
        self.route = rt
        self.model = rt.model
        self.judge_model = rt.model
        self.meter = None
        self.calls = 0

        self.fail_with = fail_with

    def _go(self):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return f"completed by {self.route.id}"

    def complete(self, *a, **k):
        return self._go()

    def structured(self, *a, **k):
        self._go()
        return {"by": self.route.id}

    def grade(self, *a, **k):
        self._go()
        return {"grade": 100}

    def ping(self, *a, **k):
        return self._go()


class RouteFaultTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = R.StateStore(os.path.join(self._tmp.name, "rotation-state.json"))
        # Route-fault tests deliberately create account-wide quota cooldowns.
        # When the capacity runtime was installed by an earlier test module,
        # those cooldowns also landed in its process-global persistent store and
        # the next test waited for the six-hour reset.  Give every test both of
        # its real ledgers, rotation and capacity, in the same isolated tempdir.
        self._prior_capacity_manager = C._MANAGER
        C._MANAGER = C.CapacityManager(C.CapacityState(
            os.path.join(self._tmp.name, "provider-capacity.json")))
        self._prior_ext = os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS")
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "0"
        self._isolated_env_names = (
            "AI_ROTATE", "AI_ROTATE_PIN", "AI_ROTATE_CATALOG",
            "AI_ROTATE_STATE", "AITIME_STATE_DIR",
        )
        self._prior_isolated_env = {
            var: os.environ.get(var) for var in self._isolated_env_names
        }
        for var in self._isolated_env_names:
            os.environ.pop(var, None)

    def tearDown(self):
        C._MANAGER = self._prior_capacity_manager
        if self._prior_ext is None:
            os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        else:
            os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = self._prior_ext
        for var, value in self._prior_isolated_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        self._tmp.cleanup()

    def provider(self, cat, failures=None, **kw):
        failures = failures or {}
        self.built = {}

        def factory(rt):
            self.built[rt.id] = _Fake(rt, failures.get(rt.id))
            return self.built[rt.id]

        rot = R.Rotator(catalog=cat, store=self.store, app="flexfactor")
        return R.RotatingProvider(rot, factory, **kw)


# --------------------------------------------------------------------------- #
# 1. A CLI transport failure is a ROUTE fault: rotate, do not kill the call.
# --------------------------------------------------------------------------- #

class CliTransportIsARouteFaultTests(RouteFaultTestCase):
    def test_a_codex_account_refusal_rotates_instead_of_killing_the_call(self):
        """The measured 30x failure. Another route must get a turn."""
        prov = self.provider(
            catalog(route("cli/codex", "codex-cli:subscription"),
                    route("or/good", "openrouter:free")),
            failures={"cli/codex": CliUnavailable(CODEX_ACCOUNT_REFUSAL)})
        self.assertEqual(prov.complete("x"), "completed by or/good")

    def test_a_cli_timeout_kill_rotates_instead_of_killing_the_call(self):
        """The measured 23x, 600 seconds each."""
        prov = self.provider(
            catalog(route("cli/claude-code", "claude-code:subscription"),
                    route("or/good", "openrouter:free")),
            failures={"cli/claude-code": CliUnavailable(CLAUDE_CLI_TIMEOUT)})
        self.assertEqual(prov.complete("x"), "completed by or/good")

    def test_a_cli_failure_is_classified_as_a_route_capability_fault(self):
        for msg in (CODEX_ACCOUNT_REFUSAL, CLAUDE_CLI_TIMEOUT):
            self.assertTrue(R.is_route_capability_error(CliUnavailable(msg)), msg[:40])
            self.assertTrue(R._is_retryable(CliUnavailable(msg)), msg[:40])


# --------------------------------------------------------------------------- #
# 2. A dead transport is BENCHED, not merely cooled for 30 seconds.
# --------------------------------------------------------------------------- #

class DeadTransportIsBenchedTests(RouteFaultTestCase):
    def test_a_dead_cli_is_not_re_selected_after_the_ordinary_route_cooldown(self):
        """30 selections of a CLI that cannot serve this account at all."""
        prov = self.provider(
            catalog(route("cli/codex", "codex-cli:subscription"),
                    route("or/good", "openrouter:free")),
            failures={"cli/codex": CliUnavailable(CODEX_ACCOUNT_REFUSAL)})
        prov.complete("x")
        cooldowns = self.store.read()["cooldowns"]
        remaining = float(cooldowns.get("route:cli/codex", 0)) - time.time()
        self.assertGreater(
            remaining, R.ROUTE_ERROR_COOLDOWN * 10,
            "a transport that cannot serve this machine at all must be benched "
            "for the run, not retried 30 seconds later")

    def test_a_dead_transport_never_cools_the_whole_pool_of_a_shared_backend(self):
        """Benching is per ROUTE. One broken model id must not bench a provider."""
        prov = self.provider(
            catalog(route("cli/codex", "codex-cli:subscription"),
                    route("or/good", "openrouter:free")),
            failures={"cli/codex": CliUnavailable(CODEX_ACCOUNT_REFUSAL)})
        prov.complete("x")
        self.assertNotIn("codex-cli:subscription", self.store.read()["cooldowns"])


# --------------------------------------------------------------------------- #
# 3. An egress refusal is a property of the PAYLOAD, never of the route.
# --------------------------------------------------------------------------- #

EGRESS_MSG = ("flexfactor_egress_blocked: payload contains ['private_key'] "
              "(near line(s) [369]); refusing to send to a cloud model.")


class EgressIsAPayloadFaultTests(RouteFaultTestCase):
    def _blocked(self):
        return ff.EgressBlockedError(EGRESS_MSG)

    def test_a_blocked_payload_does_not_strike_the_route(self):
        prov = self.provider(catalog(route("a/one", "pool-a"),
                                     route("b/one", "pool-b")),
                             failures={"a/one": self._blocked(),
                                       "b/one": self._blocked()})
        with self.assertRaises(ff.EgressBlockedError):
            prov.complete("x")
        state = self.store.read()
        self.assertEqual(state.get("strikes") or {}, {},
                         "the route did nothing wrong; the payload did")
        self.assertEqual(state.get("cooldowns") or {}, {},
                         "a blocked payload must never bench a healthy route")

    def test_a_blocked_payload_is_tried_exactly_once(self):
        """Rotating an egress refusal can never help: same payload, same verdict."""
        prov = self.provider(catalog(route("a/one", "pool-a"),
                                     route("b/one", "pool-b"),
                                     route("c/one", "pool-c")),
                             failures={"a/one": self._blocked(),
                                       "b/one": self._blocked(),
                                       "c/one": self._blocked()})
        with self.assertRaises(ff.EgressBlockedError):
            prov.complete("x")
        self.assertEqual(sum(f.calls for f in self.built.values()), 1)

    def test_the_ledger_still_sees_the_refusal(self):
        """Not charging the route must not make the failure invisible."""
        seen = []
        prov = self.provider(catalog(route("a/one", "pool-a")),
                             failures={"a/one": self._blocked()},
                             on_error=lambda rt, exc: seen.append((rt.id, str(exc))))
        with self.assertRaises(ff.EgressBlockedError):
            prov.complete("x")
        self.assertEqual(len(seen), 1)
        self.assertIn("private_key", seen[0][1])

    def test_is_payload_fault_is_true_only_for_payload_refusals(self):
        self.assertTrue(R.is_payload_fault(self._blocked()))
        self.assertFalse(R.is_payload_fault(CliUnavailable(CODEX_ACCOUNT_REFUSAL)))
        self.assertFalse(R.is_payload_fault(RuntimeError("rate limit")))


# --------------------------------------------------------------------------- #
# 4. An ollama HTTP error must carry the body ollama actually sent.
# --------------------------------------------------------------------------- #

class _HttpErrorOpener:
    """Stands in for `_local_only_opener()`; raises the HTTPError urllib raises."""

    def __init__(self, code: int, body: bytes):
        self.code = code
        self.body = body

    def open(self, req, timeout=None):
        raise urllib.error.HTTPError(
            getattr(req, "full_url", "http://127.0.0.1:11434/api/chat"),
            self.code, "Bad Request", {}, io.BytesIO(self.body))


class OllamaErrorBodyTests(unittest.TestCase):
    """`HTTPError: HTTP Error 400: Bad Request` - three times, cause unknown.

    urllib puts the server's explanation in the response body and nowhere else.
    Discarding it is why the ledger's suggestion for the only free unmetered
    reviewer on this machine reads 'no known fix'.
    """

    OLLAMA_BODY = json.dumps(
        {"error": "deepseek-r1:8b does not support tools"}).encode("utf-8")

    def _provider(self, body=None, code=400):
        prov = ff.OllamaProvider("deepseek-r1:8b")
        prov._opener = _HttpErrorOpener(code, body if body is not None else self.OLLAMA_BODY)
        return prov

    def test_the_servers_explanation_reaches_the_exception_message(self):
        prov = self._provider()
        with self.assertRaises(Exception) as ctx:
            prov.structured("sys", "user", {"type": "object"}, max_tokens=64)
        self.assertIn("does not support tools", str(ctx.exception))

    def test_the_status_code_survives_so_rotation_can_still_classify_it(self):
        prov = self._provider()
        with self.assertRaises(Exception) as ctx:
            prov.structured("sys", "user", {"type": "object"}, max_tokens=64)
        exc = ctx.exception
        self.assertEqual(getattr(exc, "status", None) or getattr(exc, "status_code", None),
                         400)

    def test_an_unreadable_body_still_raises_the_original_shape(self):
        """A body that cannot be read must never turn a 400 into a crash."""
        class _Broken(_HttpErrorOpener):
            def open(self, req, timeout=None):
                class _Fp:
                    def read(self, *a):
                        raise OSError("stream gone")

                    def close(self):
                        pass
                raise urllib.error.HTTPError(
                    "http://127.0.0.1:11434/api/chat", 400, "Bad Request", {}, _Fp())

        prov = ff.OllamaProvider("deepseek-r1:8b")
        prov._opener = _Broken(400, b"")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            prov.structured("sys", "user", {"type": "object"}, max_tokens=64)
        self.assertEqual(str(ctx.exception), "HTTP Error 400: Bad Request")
        self.assertEqual(ctx.exception.code, 400)

    def test_a_named_ollama_capability_400_rotates_to_another_route(self):
        """The whole point: a local 400 must not end a review that a cloud
        route could have completed."""
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat", 400, "Bad Request", {},
            io.BytesIO(self.OLLAMA_BODY))
        exc = ff._ollama_http_error(exc)
        self.assertTrue(R._is_retryable(exc), str(exc))


# --------------------------------------------------------------------------- #
# 5. ONE account-wide daily allowance is not eighteen independent ledgers.
# --------------------------------------------------------------------------- #

# The exact body, from the ledgers. Note what it hands us and nobody read:
# `limit_source: openrouter_free_tier_daily` (the SCOPE is the account, not the
# model) and `X-RateLimit-Reset: <epoch ms>` (the exact reset instant).
#
# TIME BOMB, FIXED 2026-08-25: the reset was frozen at the literal
# `1787616000000` (== 2026-08-25T00:00:00Z) that the live ledger happened to
# carry, and `limit_scope` deliberately trusts only a reset that is IN THE
# FUTURE and within 25 hours -- a bogus far-future stamp must not bench an
# allowance for a decade. So the fixture silently expired the moment real time
# passed it, and `test_the_reset_timestamp_in_the_message_is_used` began
# failing with `until is None`: the test aged out, the code did not change.
# The reset is now computed relative to now, which is what the assertion was
# always about (that the stamp in the body is READ), and the 25-hour window
# itself gets its own explicit test below.
_DAILY_RESET_EPOCH_S = int(time.time()) + 6 * 3600


def _openrouter_daily(reset_epoch_ms: int) -> str:
    return (
        "Error code: 429 - {'error': {'message': 'Rate limit exceeded: "
        "free-models-per-day. Add 10 credits to unlock 1000 free model requests "
        "per day', 'code': 429, 'metadata': {'headers': {'X-RateLimit-Limit': "
        "'50', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': "
        + repr(str(reset_epoch_ms)) +
        "}, 'limit_source': 'openrouter_free_tier_daily', 'remedy_hint': 'Wait "
        "for the daily reset (see X-RateLimit-Reset)'}}}")


OPENROUTER_DAILY = _openrouter_daily(_DAILY_RESET_EPOCH_S * 1000)

# Groq's, for contrast: a per-model tokens-per-minute limit that names its own
# ten-second retry. Nothing account-wide about it.
GROQ_TPM = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`meta-llama/llama-4-scout-17b-16e-instruct` in organization `org_01k` "
    "service tier `on_demand` on tokens per minute (TPM): Limit 30000, Used "
    "15684, Requested 19382. Please try again in 10.132s.'}}")


class Limited(Exception):
    def __init__(self, message, status_code=429):
        super().__init__(message)
        self.status_code = status_code


def or_free(model: str) -> R.Route:
    """An OpenRouter free route exactly as AI Time's catalog spells it.

    MEASURED from `%LOCALAPPDATA%\\AITime\\routes.json` on 2026-08-24: 19 enabled
    OpenRouter free routes carrying 18 DIFFERENT pool names, one per model
    (`openrouter:free:cohere/north-mini-code:free`, ...). OpenRouter's free tier
    is ONE account-wide daily allowance, so the rotator was told it had eighteen
    ledgers where it had one - the precise failure ("rotating model NAMES
    spreads nothing") that pool-first rotation exists to prevent. 574 of the two
    runs' 898 ledger entries are that one exhausted allowance, re-tried.
    """
    return R.Route(
        id=f"openrouter/{model}:free", backend="openrouter",
        backend_label="openrouter", model=f"{model}:free",
        wire_model=f"{model}:free", api="openai",
        base_url="https://openrouter.ai/api/v1",
        pool=f"openrouter:free:{model}:free", cost_class=R.FREE_TIER,
        tier=R.FRONTIER, enabled=True)


class AccountWideAllowanceTests(RouteFaultTestCase):
    def test_the_scope_of_a_daily_account_limit_is_read_from_the_message(self):
        self.assertEqual(R.limit_scope(Limited(OPENROUTER_DAILY))[0], "account")
        self.assertEqual(R.limit_scope(Limited(GROQ_TPM))[0], "pool")

    def test_the_reset_timestamp_in_the_message_is_used(self):
        _scope, until = R.limit_scope(Limited(OPENROUTER_DAILY))
        self.assertIsNotNone(until, "the epoch-ms reset in the body was ignored")
        self.assertEqual(int(until), _DAILY_RESET_EPOCH_S)

    def test_a_reset_already_in_the_past_is_not_trusted(self):
        # The other half of the window, and the half that made the frozen
        # fixture above fail as a "bug" for a year: a stale stamp must produce
        # no bench at all, not a bench that ended before it started.
        _scope, until = R.limit_scope(
            Limited(_openrouter_daily((int(time.time()) - 3600) * 1000)))
        self.assertIsNone(until)

    def test_a_bogus_far_future_reset_is_not_trusted(self):
        _scope, until = R.limit_scope(
            Limited(_openrouter_daily((int(time.time()) + 400 * 86400) * 1000)))
        self.assertIsNone(until, "a far-future stamp would bench for a decade")

    def test_one_daily_429_benches_every_route_on_that_allowance(self):
        """The 574-entry storm: 18 synthetic pools, one dead allowance."""
        cat = catalog(or_free("a/one"), or_free("b/two"), or_free("c/three"),
                      route("zz/backup", "zz:free-tier", cost=R.FREE_TIER))
        prov = self.provider(
            cat, failures={f"openrouter/{m}:free": Limited(OPENROUTER_DAILY)
                           for m in ("a/one", "b/two", "c/three")})
        self.assertEqual(prov.complete("x"), "completed by zz/backup")
        tried = [rid for rid, f in self.built.items() if f.calls]
        self.assertEqual(
            len([t for t in tried if t.startswith("openrouter/")]), 1,
            "one account-wide refusal must bench the whole allowance, not be "
            f"re-tried once per synthetic per-model pool (tried: {tried})")

    def test_the_bench_outlives_the_60_second_pool_cooldown(self):
        """A DAILY quota must not be re-tested every minute for the rest of the
        night. The ordinary pool cooldown is expired here on purpose - it is the
        only thing hiding the defect within a single call."""
        cat = catalog(or_free("a/one"), or_free("b/two"),
                      route("zz/backup", "zz:free-tier", cost=R.FREE_TIER))
        prov = self.provider(
            cat, failures={f"openrouter/{m}:free": Limited(OPENROUTER_DAILY)
                           for m in ("a/one", "b/two")})
        prov.complete("x")
        before = sum(f.calls for rid, f in self.built.items()
                     if rid.startswith("openrouter/"))

        def expire_pool_cooldowns(data):
            data["cooldowns"] = {k: v for k, v in (data.get("cooldowns") or {}).items()
                                 if k.startswith("allowance:")}
        self.store.update(expire_pool_cooldowns)

        prov.complete("x")
        after = sum(f.calls for rid, f in self.built.items()
                    if rid.startswith("openrouter/"))
        self.assertEqual(before, after,
                         "the daily allowance is still exhausted; only the "
                         "60-second per-pool cooldowns have lapsed")
        until = self.store.read()["cooldowns"]["allowance:openrouter:free-tier"]
        self.assertGreater(until - time.time(), R.DEFAULT_RATE_LIMIT_COOLDOWN * 10)

    def test_a_per_model_rate_limit_still_only_cools_its_own_pool(self):
        """Groq's TPM limit is NOT account-wide. Over-cooling would bench a
        backend that is answering fine on every other model."""
        cat = catalog(route("groq/a", "groq:free-tier", cost=R.FREE_TIER),
                      route("groq/b", "groq:other", cost=R.FREE_TIER))
        prov = self.provider(cat, failures={"groq/a": Limited(GROQ_TPM)})
        self.assertEqual(prov.complete("x"), "completed by groq/b")
        cooldowns = self.store.read()["cooldowns"]
        self.assertIn("groq:free-tier", cooldowns)
        self.assertNotIn("allowance:groq:free-tier", cooldowns)

    def test_the_allowance_key_names_the_backend_and_its_cost_class(self):
        self.assertEqual(R.allowance_key(or_free("a/one")), "openrouter:free-tier")
        # Paid OpenRouter credits are a DIFFERENT allowance from the free tier;
        # exhausting the free one must never bench the paid one.
        paid = route("openrouter/big", "openrouter:credits", cost=R.PAID_METERED)
        self.assertNotEqual(R.allowance_key(paid), R.allowance_key(or_free("a/one")))


# --------------------------------------------------------------------------- #
# 5. A model's SAFETY REFUSAL is a property of that model family: rotate.
# --------------------------------------------------------------------------- #

# The exact provider text from the live 2026-09-11 scout of a benign 6-file
# scratch repository (tinystats: a mean() helper and a port parser).
ANTHROPIC_CYBER_REFUSAL = (
    "Model refused (stop_details=RefusalStopDetails(category='cyber', "
    "explanation=\"This request triggered restrictions on violative cyber "
    "content and was blocked under Anthropic's Usage Policy.\", type='refusal'))."
)


class ModelRefusalRotatesTests(RouteFaultTestCase):
    """LIVE 2026-09-11: `flexfactor scout` on a benign repo exited 2 at purpose
    inference. The ladder picked anthropic_api/claude-fable-5, the provider
    answered stop_reason=refusal (category cyber), AnthropicProvider raised a
    bare RuntimeError, and `_is_retryable` said no - so ONE false-positive
    safety classification ended the call while ~1200 other routes, most of
    them other model families, never got a turn."""

    def test_a_refusal_is_retried_on_a_different_family(self):
        prov = self.provider(
            catalog(route("anthropic_api/claude-fable-5", "anthropic:paid"),
                    route("openai_api/gpt-5", "openai:paid")),
            failures={"anthropic_api/claude-fable-5":
                      ff.ModelRefusalError(ANTHROPIC_CYBER_REFUSAL)})
        self.assertEqual(prov.structured("s", "p", {}),
                         {"by": "openai_api/gpt-5"})

    def test_the_legacy_untyped_refusal_text_is_recognised_too(self):
        exc = RuntimeError(ANTHROPIC_CYBER_REFUSAL)
        self.assertTrue(R.is_model_refusal(exc))
        self.assertTrue(R._is_retryable(exc))

    def test_the_refusing_family_is_not_asked_again_in_the_same_call(self):
        """Two Claude routes refuse identically; the second must never be
        spent on the same bytes once the first said no."""
        prov = self.provider(
            catalog(route("anthropic_api/claude-fable-5", "anthropic:a"),
                    route("anthropic_api/claude-fable-5-1", "anthropic:b"),
                    route("openai_api/gpt-5", "openai:paid")),
            failures={
                "anthropic_api/claude-fable-5":
                    ff.ModelRefusalError(ANTHROPIC_CYBER_REFUSAL),
                "anthropic_api/claude-fable-5-1":
                    ff.ModelRefusalError(ANTHROPIC_CYBER_REFUSAL),
            })
        self.assertEqual(prov.structured("s", "p", {}),
                         {"by": "openai_api/gpt-5"})
        claude_calls = sum(f.calls for rid, f in self.built.items()
                           if "claude" in rid)
        self.assertLessEqual(claude_calls, 1)

    def test_a_refusal_never_cools_the_pool(self):
        """A payload-specific classification is not evidence the provider is
        sick; three of them must not bench a whole pool for five minutes."""
        prov = self.provider(
            catalog(route("anthropic_api/claude-fable-5", "anthropic:paid"),
                    route("openai_api/gpt-5", "openai:paid")),
            failures={"anthropic_api/claude-fable-5":
                      ff.ModelRefusalError(ANTHROPIC_CYBER_REFUSAL)})
        for _ in range(4):
            prov.structured("s", "p", {})
        state = self.store.read()
        self.assertNotIn("anthropic:paid", state.get("cooldowns") or {})
        self.assertNotIn("anthropic_api/claude-fable-5",
                         state.get("strikes") or {})

    def test_when_every_family_refuses_the_refusal_surfaces(self):
        """Rotation must never turn a universal refusal into a silent success."""
        prov = self.provider(
            catalog(route("anthropic_api/claude-fable-5", "anthropic:paid")),
            failures={"anthropic_api/claude-fable-5":
                      ff.ModelRefusalError(ANTHROPIC_CYBER_REFUSAL)})
        with self.assertRaises(Exception) as ctx:
            prov.structured("s", "p", {})
        self.assertIn("Model refused", str(ctx.exception))

    def test_a_refusal_leaves_no_shared_cooldown_for_unrelated_calls(self):
        """Review on #174: a 30s route cooldown in the SHARED rotation state made
        unrelated calls and other workers skip a healthy route - fatal in a
        catalog where it is the only usable route. The refusal was about the
        bytes of one call, so only that call may step around it."""
        rid = "anthropic_api/claude-fable-5"
        prov = self.provider(
            catalog(route(rid, "anthropic:paid")),
            failures={rid: ff.ModelRefusalError(ANTHROPIC_CYBER_REFUSAL)})
        with self.assertRaises(Exception):
            prov.structured("s", "refused bytes", {})
        self.built[rid].fail_with = None
        self.assertEqual(prov.structured("s", "different bytes", {}), {"by": rid})
        self.assertNotIn(f"route:{rid}", self.store.read().get("cooldowns") or {})

    def test_a_structured_refusal_is_not_re_sent_inside_the_provider(self):
        """Review on #174: _stream_structured re-rolled a refusal (no parseable
        text) three times with 6s sleeps before anyone read stop_reason, sending
        the refused bytes to the same family again and possibly on to the paid
        rescue - defeating the same-family exclusion."""
        import types
        from unittest import mock
        calls = []
        refusal = types.SimpleNamespace(stop_reason="refusal", content=[],
                                        stop_details="category=cyber", usage=None)

        def fake_stream(_client, **kwargs):
            calls.append(kwargs)
            return refusal

        prov = object.__new__(ff.AnthropicProvider)
        prov.client = object()
        with mock.patch.object(ff, "_stream_with_deadline", side_effect=fake_stream), \
                mock.patch.object(ff, "_fallback_hold_active", return_value=False), \
                mock.patch.object(ff, "_fallback_available", return_value=True), \
                mock.patch.object(ff, "_paid_message_unused", create=True), \
                mock.patch.object(ff.time, "sleep") as slept:
            got = prov._stream_structured(
                model="claude-fable-5", max_tokens=100, system="s",
                messages=[{"role": "user", "content": "p"}],
                fmt={"format": {"type": "json_schema", "schema": {"type": "object"}}})
        self.assertIs(got, refusal)
        self.assertEqual(len(calls), 1, "a refusal must not be re-sent")
        slept.assert_not_called()

    def test_the_anthropic_provider_raises_the_typed_refusal(self):
        self.assertTrue(issubclass(ff.ModelRefusalError, RuntimeError),
                        "existing `except RuntimeError` callers must keep working")
        for site in ("Model refused the rewrite", "Model refused to grade",
                     "Model refused (stop_details"):
            self.assertIn(f"raise ModelRefusalError(f\"{site}", 
                          __import__("inspect").getsource(ff.AnthropicProvider))


# --------------------------------------------------------------------------- #
# 6. A grader that ignores the grade schema is the ROUTE's fault: rotate.
# --------------------------------------------------------------------------- #

GRADE_ISSUES_SHAPE = "grade response field 'issues' must be an array of strings"


class MalformedGradeRotatesTests(RouteFaultTestCase):
    """LIVE 2026-09-11: a real `flexfactor refactor` exited 1 after 443s. One
    rotated grader route answered `issues` in the wrong shape, `_parse_grade`
    raised a plain ValueError, `_is_retryable` called that fatal, and the whole
    refactor ended - although a structured call that ignores its schema
    (StructuredOutputShapeError) already rotates to another model."""

    def test_the_grade_shape_error_is_a_typed_value_error(self):
        self.assertTrue(issubclass(ff.GradeShapeError, ValueError),
                        "existing `except ValueError` callers must keep working")
        with self.assertRaises(ff.GradeShapeError):
            ff._parse_grade(json.dumps({"grade": 100, "meets_goal": True,
                                        "rationale": "ok", "issues": [1]}))
        with self.assertRaises(ff.GradeShapeError):
            ff._parse_grade("not json at all")

    def test_a_malformed_grade_is_retryable_and_classified_as_malformed(self):
        exc = ff.GradeShapeError(GRADE_ISSUES_SHAPE)
        self.assertTrue(R.is_malformed_output(exc))
        self.assertTrue(R._is_retryable(exc))

    def test_a_malformed_grade_moves_the_call_to_another_route(self):
        prov = self.provider(
            catalog(route("gemini/gemini-2.5-pro", "gemini:paid"),
                    route("openai_api/gpt-5", "openai:paid")),
            failures={"gemini/gemini-2.5-pro": ff.GradeShapeError(GRADE_ISSUES_SHAPE)})
        self.assertEqual(prov.structured("s", "p", {}), {"by": "openai_api/gpt-5"})

    def test_a_plain_value_error_is_still_fatal(self):
        """Only the grader-contract shape rotates; a programming ValueError
        must not tour every pool reproducing the same bug."""
        self.assertFalse(R._is_retryable(ValueError("invalid literal for int()")))


# --------------------------------------------------------------------------- #
# 7. Review on #176: the grade-shape fix must reach every grader path.
# --------------------------------------------------------------------------- #

_BAD_GRADE = {"grade": 100, "meets_goal": True, "rationale": "ok", "issues": [1]}
_GOOD_GRADE = {"grade": 100, "meets_goal": True, "rationale": "ok", "issues": []}


class GradeShapeReachesRotationTests(RouteFaultTestCase):
    def _grading_provider(self, payloads):
        # Grading is a REVIEWER call on the judge (light) tier, and independent
        # review excludes opaque model families - so the routes must be light
        # tier with recognized families, like real graders.
        rot = R.Rotator(catalog=catalog(*[route(rid, f"pool:{rid}", tier=R.LIGHT)
                                          for rid in payloads]),
                        store=self.store, app="flexfactor")

        class _Grader:
            def __init__(self, rt):
                self.route = rt
                self.model = rt.model
                self.judge_model = rt.model
                self.meter = None
                self.calls = 0

            def grade(self, *a, **k):
                self.calls += 1
                return payloads[self.route.id]

        prov = R.RotatingProvider(rot, _Grader)
        prov.grade_validator = ff._normalize_grade
        return prov

    def test_a_malformed_subscription_grade_is_rejected_inside_the_attempt(self):
        """CLI/Cursor graders return raw dicts/text; validated only after
        grade() returned, a malformed one could never be rotated away from."""
        prov = self._grading_provider({"openai_api/gpt-5-mini": _BAD_GRADE})
        with self.assertRaises(Exception) as ctx:
            prov.grade("p")
        self.assertIn("GradeShapeError", str(ctx.exception))

    def test_a_rotated_grade_comes_back_normalized(self):
        prov = self._grading_provider({"anthropic_api/claude-sonnet-5": _BAD_GRADE,
                                       "openai_api/gpt-5-mini": _GOOD_GRADE})
        result = prov.grade("p")
        self.assertIsInstance(result, ff.Grade)
        self.assertEqual(result.issues, [])

    def test_the_anthropic_grader_keeps_the_grade_shape_type(self):
        """AnthropicProvider.grade wrapped every parse failure in RuntimeError,
        which rotation treats as fatal."""
        import contextlib
        import types
        from unittest import mock
        prov = object.__new__(ff.AnthropicProvider)
        prov.client = object()
        prov.meter = None
        prov.judge_model = "claude-fable-5"
        prov._meter = lambda *a, **k: None
        text = '{"grade": 100, "meets_goal": true, "rationale": "x", "issues": [1]}'
        msg = types.SimpleNamespace(
            stop_reason="end_turn", stop_details=None,
            content=[types.SimpleNamespace(type="text", text=text)])
        with mock.patch.object(ff.AnthropicProvider, "_stream_structured", return_value=msg), \
                mock.patch.object(ff, "_budget_guard", return_value=contextlib.nullcontext()):
            with self.assertRaises(ff.GradeShapeError):
                prov.grade("p")

    def test_malformed_graders_stay_cooled_when_the_whole_ladder_fails(self):
        """The cooldown release exists for a structured call's corrective retry;
        grade() has none, so releasing it re-offered the same malformed graders
        to the next target."""
        prov = self.provider(
            catalog(route("anthropic_api/claude-sonnet-5", "pool:a", tier=R.LIGHT),
                    route("openai_api/gpt-5-mini", "pool:b", tier=R.LIGHT)),
            failures={"anthropic_api/claude-sonnet-5": ff.GradeShapeError(
                          "grade response field 'issues' must be an array of strings"),
                      "openai_api/gpt-5-mini": ff.GradeShapeError(
                          "grade response field 'issues' must be an array of strings")})
        with self.assertRaises(Exception):
            prov.grade("p")
        cooldowns = self.store.read().get("cooldowns") or {}
        self.assertIn("route:anthropic_api/claude-sonnet-5", cooldowns)
        self.assertIn("route:openai_api/gpt-5-mini", cooldowns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
