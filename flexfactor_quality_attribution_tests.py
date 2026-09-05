"""A work verdict must strike the route THAT THREAD used.

`RotatingProvider._last_selection` is SHARED on purpose: a reviewer has to
avoid the family that authored last, across providers and across threads. But
`report_quality("reviewer", "rejected")` is a verdict on ONE call, and semantic
review fans its units across a thread pool sharing one provider. Reading the
shared map means worker A's verdict lands on whichever route worker B selected
in between -- a route cooled for work it never produced, and the rotator's
yield learning trained on noise.

This is the same shared-mutable-state defect as the `.model` misattribution
fixed in #152, one layer deeper: that one produced a wrong LOG LINE, this one
produces a wrong COOLDOWN.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import threading
import unittest

import flexfactor_rotation as R


def _route(rid: str, pool: str) -> R.Route:
    return R.Route(
        id=rid, backend=rid.split("/")[0], backend_label=rid.split("/")[0],
        model=rid.split("/", 1)[-1], wire_model=rid.split("/", 1)[-1],
        pool=pool, tier=R.FRONTIER, cost_class=R.FREE_TIER,
        api="openai", base_url="http://localhost", auth_env="", enabled=True,
    )


class _RecordingRotator:
    """Captures which route each quality verdict was charged to."""

    def __init__(self, routes):
        self._routes = list(routes)
        self._i = 0
        self.quality_calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.catalog = type("C", (), {"routes": routes})()

    def next_route(self, tier=R.FRONTIER, allow_paid=False, **kw):
        with self.lock:
            route = self._routes[self._i % len(self._routes)]
            self._i += 1
        return R.Selection(route=route, pool=route.pool, tier=tier,
                           requested_tier=tier, intent_role=
                           (kw.get("intent").role if kw.get("intent") else ""))

    def report(self, route, outcome, *a, **kw):
        return None

    def report_quality(self, route, signal, purpose="", now=None):
        self.quality_calls.append((route.id, signal))
        return None


class _Provider:
    """Minimal provider surface; blocks until this thread is released."""

    def __init__(self, gate: threading.Event | None):
        self._gate = gate

    def structured(self, *a, **kw):
        if self._gate is not None:
            self._gate.wait(5)
        return {"ok": 1}


class ConcurrentQualityAttributionTests(unittest.TestCase):
    def _provider_with(self, routes, gates):
        rot = _RecordingRotator(routes)
        client = R.RotatingProvider(
            rotator=rot,
            factory=lambda route: _Provider(gates.get(route.id)),
            tier=R.FRONTIER,
        )
        return rot, client

    def test_a_workers_verdict_never_lands_on_a_siblings_route(self):
        """The race, made deterministic.

        Worker A finishes its reviewer call on route-a. Worker B then finishes
        its own on route-b, overwriting the SHARED last-selection. Only then
        does worker A report its verdict. Before the fix that verdict was
        charged to route-b, which never saw A's payload.
        """
        a, b = _route("prov/route-a", "pool-a"), _route("prov/route-b", "pool-b")
        rot, client = self._provider_with([a, b], {})

        a_called = threading.Event()
        b_done = threading.Event()
        verdict: list = []

        def worker_a():
            client.structured("payload-a",
                              intent=R.CallIntent(R.ROLE_REVIEWER, ()))
            a_called.set()
            b_done.wait(5)          # a sibling finishes in between
            client.report_quality(R.ROLE_REVIEWER, "rejected")
            verdict.append("done")

        def worker_b():
            a_called.wait(5)
            client.structured("payload-b",
                              intent=R.CallIntent(R.ROLE_REVIEWER, ()))
            b_done.set()

        ta, tb = threading.Thread(target=worker_a), threading.Thread(target=worker_b)
        ta.start(); tb.start(); ta.join(5); tb.join(5)

        self.assertEqual(verdict, ["done"], "the interleaving did not complete")
        self.assertTrue(b_done.is_set(),
                        "precondition: the sibling really did select after A")
        self.assertEqual(
            rot.quality_calls, [("prov/route-a", "rejected")],
            "the verdict was charged to a route that never served this work; "
            "that route now cools for output it did not produce")

    def test_each_thread_is_graded_on_its_own_route(self):
        """Both workers report; neither may be charged for the other."""
        a, b = _route("prov/route-a", "pool-a"), _route("prov/route-b", "pool-b")
        rot, client = self._provider_with([a, b], {})
        both_selected = threading.Barrier(2, timeout=5)

        def work(signal):
            def run():
                client.structured("p", intent=R.CallIntent(R.ROLE_REVIEWER, ()))
                both_selected.wait()      # force the overlap
                client.report_quality(R.ROLE_REVIEWER, signal)
            return run

        t1 = threading.Thread(target=work("verified"))
        t2 = threading.Thread(target=work("rejected"))
        t1.start(); t2.start(); t1.join(5); t2.join(5)

        charged = dict(rot.quality_calls)
        self.assertEqual(len(rot.quality_calls), 2)
        self.assertEqual(set(charged), {"prov/route-a", "prov/route-b"},
                         "both verdicts landed on the same route")


class AttributionStillWorksTests(unittest.TestCase):
    """Guards the fix from becoming 'attribute nothing'."""

    def test_the_ordinary_single_threaded_case_is_unchanged(self):
        a = _route("prov/route-a", "pool-a")
        rot, client = ConcurrentQualityAttributionTests()._provider_with([a], {})
        client.structured("p", intent=R.CallIntent(R.ROLE_REVIEWER, ()))
        client.report_quality(R.ROLE_REVIEWER, "verified")
        self.assertEqual(rot.quality_calls, [("prov/route-a", "verified")])

    def test_reporting_from_another_thread_still_attributes(self):
        """The fix must not blank an attribution that used to work.

        The fix loop hands work between threads, so a verdict can legitimately
        be reported by a thread that never made the call. That falls back to
        the shared map -- exactly the old behaviour.
        """
        a = _route("prov/route-a", "pool-a")
        rot, client = ConcurrentQualityAttributionTests()._provider_with([a], {})
        client.structured("p", intent=R.CallIntent(R.ROLE_REVIEWER, ()))

        done = threading.Event()

        def elsewhere():
            client.report_quality(R.ROLE_REVIEWER, "verified")
            done.set()

        t = threading.Thread(target=elsewhere)
        t.start(); t.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(rot.quality_calls, [("prov/route-a", "verified")],
                         "a cross-thread report lost its attribution entirely")

    def test_no_call_yet_reports_nothing(self):
        a = _route("prov/route-a", "pool-a")
        rot, client = ConcurrentQualityAttributionTests()._provider_with([a], {})
        self.assertIsNone(client.report_quality(R.ROLE_REVIEWER, "verified"))
        self.assertEqual(rot.quality_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
