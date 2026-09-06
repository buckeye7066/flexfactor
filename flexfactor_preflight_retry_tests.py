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
        self._built = None
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
