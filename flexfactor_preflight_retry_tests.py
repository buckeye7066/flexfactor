"""One dead route is not a dead ladder.

Measured 2026-09-05. A run with a catalog of 169 routes over 7 pools died in 25
log lines because the FIRST route preflight probed was Google's retired
`gemini-2.5-pro`, which 404s "no longer available to new users". The run
reported

    the best-available model ladder has no live inference route

and stopped before cycle 1. 168 live routes were never asked.

The old code even observed the fix in a comment -- "route/pool is already
benched by rotation" -- and then returned `[]` anyway. Because the failed route
IS benched, the very next ping draws a different one; nothing was retrying.

Retries are bounded (a catalog can hold hundreds of routes and preflight must
not tour them all) and only happen when there is something else to draw: a
single-route ladder keeps the old one-shot behaviour, because re-pinging the
same dead transport three times just burns three deadlines.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

import flexfactor as ff


class _Args:
    """Minimal args object for the preflight branch."""
    def __init__(self, **kw):
        self.no_preflight = False
        self.model_mode = "best"
        for k, v in kw.items():
            setattr(self, k, v)


class _Provider:
    """Pings fail for the first `dead` calls, then succeed (a fresh route)."""
    def __init__(self, dead: int, exc=None):
        self.dead = dead
        self.calls = 0
        self._exc = exc or RuntimeError(
            "Error code: 404 - models/gemini-2.5-pro is no longer available")

    def ping(self):
        self.calls += 1
        if self.calls <= self.dead:
            raise self._exc
        return True


class _AlwaysFalse:
    def __init__(self):
        self.calls = 0

    def ping(self):
        self.calls += 1
        return False


class PreflightRetriesPastADeadRouteTests(unittest.TestCase):
    """Drives the REAL `build_audit_providers` branch via injection."""

    def setUp(self):
        self._proxy = patch.object(ff, "_auto_activate_fcc_proxy")
        self._proxy.start()
        self.addCleanup(self._proxy.stop)
        self.addCleanup(setattr, ff, "_PROVIDER_DIAGNOSIS", ff._PROVIDER_DIAGNOSIS)
        self._prior_build = ff._build_rotating_provider
        self._prior_usable = ff._LAST_ROTATION_USABLE

    def tearDown(self):
        ff._build_rotating_provider = self._prior_build
        ff._LAST_ROTATION_USABLE = self._prior_usable

    def _run(self, provider, usable_routes):
        ff._build_rotating_provider = lambda *a, **k: provider
        ff._LAST_ROTATION_USABLE = usable_routes
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = ff.build_audit_providers(_Args(), None)
        return out, buf.getvalue()

    def test_the_live_case_one_retired_route_out_of_many(self):
        """169 routes, the first one dead. The run must NOT be abandoned."""
        prov = _Provider(dead=1)
        out, err = self._run(prov, usable_routes=169)
        self.assertTrue(out, "the whole ladder was abandoned over ONE dead route")
        self.assertEqual(prov.calls, 2, "it never tried a second route")
        self.assertIn("trying the next one", err)

    def test_it_gives_up_after_the_bound(self):
        """A genuinely dead ladder must still fail -- and say how many it tried."""
        prov = _Provider(dead=99)
        out, _ = self._run(prov, usable_routes=169)
        self.assertEqual(out, [])
        self.assertEqual(prov.calls, ff.PREFLIGHT_PING_ATTEMPTS)
        self.assertIn("no live inference route", ff._PROVIDER_DIAGNOSIS)
        self.assertIn(str(ff.PREFLIGHT_PING_ATTEMPTS), ff._PROVIDER_DIAGNOSIS)

    def test_a_single_route_ladder_is_still_one_shot(self):
        """Re-pinging one dead transport just burns deadlines."""
        prov = _Provider(dead=99)
        out, _ = self._run(prov, usable_routes=1)
        self.assertEqual(out, [])
        self.assertEqual(prov.calls, 1, "wasted extra deadlines on a lone route")

    def test_a_healthy_first_route_pings_exactly_once(self):
        """The common path must not become three pings."""
        prov = _Provider(dead=0)
        out, err = self._run(prov, usable_routes=169)
        self.assertTrue(out)
        self.assertEqual(prov.calls, 1)
        self.assertEqual(err, "")

    def test_a_False_verdict_counts_as_a_failure_not_a_pass(self):
        """`ping() is False` is an explicit failed health verdict."""
        prov = _AlwaysFalse()
        out, _ = self._run(prov, usable_routes=169)
        self.assertEqual(out, [])
        self.assertEqual(prov.calls, ff.PREFLIGHT_PING_ATTEMPTS)

    def test_no_preflight_skips_the_probe_entirely(self):
        prov = _Provider(dead=99)
        ff._build_rotating_provider = lambda *a, **k: prov
        ff._LAST_ROTATION_USABLE = 169
        out = ff.build_audit_providers(_Args(no_preflight=True), None)
        self.assertTrue(out)
        self.assertEqual(prov.calls, 0)


class PreflightBoundaryTests(unittest.TestCase):
    setUp = PreflightRetriesPastADeadRouteTests.setUp
    tearDown = PreflightRetriesPastADeadRouteTests.tearDown
    _run = PreflightRetriesPastADeadRouteTests._run

    def test_programming_error_is_not_retried(self):
        prov = _Provider(dead=99, exc=TypeError('implementation bug'))
        out, _ = self._run(prov, usable_routes=169)
        self.assertEqual(out, [])
        self.assertEqual(prov.calls, 1)

    def test_egress_policy_refusal_is_not_retried(self):
        error = type('EgressBlockedError', (RuntimeError,), {})('blocked')
        prov = _Provider(dead=99, exc=error)
        out, _ = self._run(prov, usable_routes=169)
        self.assertEqual(out, [])
        self.assertEqual(prov.calls, 1)

    def test_cancellation_propagates(self):
        for exc in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(exc).__name__):
                prov = _Provider(dead=99, exc=exc)
                with self.assertRaises(type(exc)):
                    self._run(prov, usable_routes=169)
                self.assertEqual(prov.calls, 1)

    def test_two_routes_do_not_get_three_outer_attempts(self):
        prov = _AlwaysFalse()
        out, _ = self._run(prov, usable_routes=2)
        self.assertEqual(out, [])
        self.assertEqual(prov.calls, 2)
        self.assertIn('untried routes may remain', ff._PROVIDER_DIAGNOSIS)


class RealRotatorPreflightTests(unittest.TestCase):
    setUp = PreflightRetriesPastADeadRouteTests.setUp
    tearDown = PreflightRetriesPastADeadRouteTests.tearDown
    _run = PreflightRetriesPastADeadRouteTests._run

    def _real_run(self, outcomes, *, same_pool=False, paid_then_free=False):
        import tempfile
        from types import SimpleNamespace
        import flexfactor_rotation as rotation
        from flexfactor_rotation_tests import route, catalog
        self.calls, self.errors = [], []
        routes = [route(f'backend{i}/model{i}', 'shared' if same_pool else f'pool{i}',
                        cost=rotation.FREE_TIER if paid_then_free and i else rotation.PAID_METERED)
                  for i in range(len(outcomes))]
        pending = iter(outcomes)
        def factory(selected):
            def ping():
                self.calls.append(selected)
                value = next(pending)
                if isinstance(value, BaseException):
                    raise value
                return value
            return SimpleNamespace(ping=ping)
        with tempfile.TemporaryDirectory() as tmp:
            rotator = rotation.Rotator(catalog=catalog(*routes),
                                       store=rotation.StateStore(tmp + '/state.json'))
            provider = rotation.RotatingProvider(rotator, factory, judge_tier=rotation.FRONTIER,
                        allow_paid=True, paid_first=True,
                        on_error=lambda selected, exc: self.errors.append((selected.id, exc)))
            return self._run(provider, len(routes))

    def test_retired_model_uses_a_distinct_healthy_route(self):
        from flexfactor_rotation_tests import Boom
        out, _ = self._real_run([Boom('model no longer available', status_code=404), True])
        self.assertTrue(out)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0].id, self.calls[1].id)
        self.assertEqual(len(self.errors), 1)

    def test_false_health_verdict_benches_route_in_shared_pool(self):
        out, _ = self._real_run([False, True], same_pool=True)
        self.assertTrue(out)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0].id, self.calls[1].id)
        self.assertEqual(len(self.errors), 1)

    def test_two_retired_models_do_not_abandon_healthy_shared_pool(self):
        from flexfactor_rotation_tests import Boom
        bad = lambda: Boom('model no longer available', status_code=404)
        out, _ = self._real_run([bad(), bad(), True], same_pool=True)
        self.assertTrue(out)
        self.assertEqual(len({r.id for r in self.calls}), 3)

    def test_all_failed_health_verdicts_fail_closed(self):
        out, _ = self._real_run([False, False, False], same_pool=True)
        self.assertEqual(out, [])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.errors), 3)

    def test_quota_failure_descends_from_paid_to_free(self):
        from flexfactor_rotation_tests import Boom
        out, _ = self._real_run([Boom('quota exhausted', status_code=429), True], paid_then_free=True)
        self.assertTrue(out)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0].uses_paid_capacity)
        self.assertFalse(self.calls[1].uses_paid_capacity)

    def test_auth_failure_uses_another_credential_pool(self):
        from flexfactor_rotation_tests import Boom
        out, _ = self._real_run([Boom('invalid credential', status_code=401), True])
        self.assertTrue(out)
        self.assertNotEqual(self.calls[0].pool, self.calls[1].pool)

    def test_malformed_request_is_not_retried(self):
        from flexfactor_rotation_tests import Boom
        out, _ = self._real_run([Boom('invalid request body', status_code=400), True])
        self.assertEqual(out, [])
        self.assertEqual(len(self.calls), 1)

    def test_untried_healthy_route_is_not_claimed_unavailable(self):
        from flexfactor_rotation_tests import Boom
        bad = lambda: Boom('model no longer available', status_code=404)
        out, _ = self._real_run([bad(), bad(), bad(), True], same_pool=True)
        self.assertEqual(out, [])
        self.assertEqual(len(self.calls), 3)
        self.assertIn('untried routes may remain', ff._PROVIDER_DIAGNOSIS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
