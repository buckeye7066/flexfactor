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
        self._prior_ext = os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS")
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "0"
        for var in ("AI_ROTATE", "AI_ROTATE_PIN", "AI_ROTATE_CATALOG",
                    "AI_ROTATE_STATE", "AITIME_STATE_DIR"):
            os.environ.pop(var, None)

    def tearDown(self):
        if self._prior_ext is None:
            os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        else:
            os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = self._prior_ext
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
