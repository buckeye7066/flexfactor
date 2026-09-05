"""A dead process's LEASE must not hold an allowance for its full TTL.

`CapacityManager.acquire` already refuses to queue behind a WAITER whose
same-host pid is provably dead. Leases had no such belt: `_cleanup` reaped them
by `expires_at` alone, so a killed or crashed run held its allowance for the
full `LEASE_TTL_S` (15 minutes).

Measured 2026-09-05 on a live FlexFactor prodready run: a taskkill'd process
left `anthropic_sub:subscription` leased with 891s still to run. `limit()` is 1
for that cost class, so the next run could not start at all until the TTL
elapsed -- and every model call in the audit funnels through that one allowance.

Runs offline. No credentials, no network, no tokens spent.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import flexfactor_capacity as C


class _Route:
    """A subscription route -- limit() is 1, so one stale lease blocks everyone."""
    backend = "anthropic_sub"
    cost_class = "subscription"


def _dead_pid() -> int:
    """A pid that is provably not running on this host.

    Spawns nothing: picks a high pid and confirms `_pid_alive` says dead, so the
    test never depends on a number that happens to be free.
    """
    for candidate in range(999_000, 999_400):
        if not C._pid_alive(candidate):
            return candidate
    raise unittest.SkipTest("no provably-dead pid found on this host")


class DeadLeaseIsReapedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "capacity.json")
        self.store = C.CapacityState(self.path)
        self.mgr = C.CapacityManager(store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, leases):
        self.store.update(lambda d: d.__setitem__("leases", leases))

    def _leases(self):
        return self.store.read().get("leases") or {}

    def test_a_lease_held_by_a_dead_same_host_pid_is_reaped(self):
        dead = _dead_pid()
        self._write({"stale": {
            "allowance": "anthropic_sub:subscription", "app": "flexfactor",
            "pid": dead, "host": C._HOST,
            "started_at": 0.0, "expires_at": C.time.time() + 891.0,
        }})
        # Precondition: the TTL has NOT expired, so only the pid check can free it.
        self.assertGreater(
            float(self._leases()["stale"]["expires_at"]) - C.time.time(), 600.0)

        lease = self.mgr.acquire(_Route(), timeout=5)

        self.assertIsNotNone(lease, "the allowance stayed blocked by a dead owner")
        self.assertNotIn("stale", self._leases(), "the dead lease was not reaped")

    def test_a_live_lease_is_never_reaped(self):
        """The guard that keeps this from stealing capacity from a running audit."""
        self._write({"live": {
            "allowance": "anthropic_sub:subscription", "app": "flexfactor",
            "pid": os.getpid(), "host": C._HOST,
            "started_at": 0.0, "expires_at": C.time.time() + 891.0,
        }})
        with self.assertRaises(C.CapacityTimeout):
            self.mgr.acquire(_Route(), timeout=1)
        self.assertIn("live", self._leases(),
                      "a LIVE process's lease was reaped -- two runs now share "
                      "an allowance whose limit is 1")

    def test_a_lease_from_another_host_is_left_to_its_TTL(self):
        """Pids are not comparable across machines."""
        self._write({"remote": {
            "allowance": "anthropic_sub:subscription", "app": "flexfactor",
            "pid": _dead_pid(), "host": C._HOST + "-other",
            "started_at": 0.0, "expires_at": C.time.time() + 891.0,
        }})
        with self.assertRaises(C.CapacityTimeout):
            self.mgr.acquire(_Route(), timeout=1)
        self.assertIn("remote", self._leases(),
                      "reaped another host's lease on a pid that means nothing here")

    def test_a_legacy_lease_with_no_host_keeps_the_old_TTL_behaviour(self):
        """Rows written before this change carry no host; do not guess."""
        self._write({"legacy": {
            "allowance": "anthropic_sub:subscription", "app": "flexfactor",
            "pid": _dead_pid(),
            "started_at": 0.0, "expires_at": C.time.time() + 891.0,
        }})
        with self.assertRaises(C.CapacityTimeout):
            self.mgr.acquire(_Route(), timeout=1)
        self.assertIn("legacy", self._leases())

    def test_an_expired_lease_is_still_reaped_by_TTL(self):
        """The original path must keep working, dead pid or not."""
        self._write({"expired": {
            "allowance": "anthropic_sub:subscription", "app": "flexfactor",
            "pid": os.getpid(), "host": C._HOST,
            "started_at": 0.0, "expires_at": C.time.time() - 1.0,
        }})
        lease = self.mgr.acquire(_Route(), timeout=5)
        self.assertIsNotNone(lease)
        self.assertNotIn("expired", self._leases())


class LeasesRecordTheirHostTests(unittest.TestCase):
    def test_a_granted_lease_records_the_host_so_it_can_be_reaped(self):
        """Without this the pid check above can never fire on a real row."""
        with tempfile.TemporaryDirectory() as tmp:
            store = C.CapacityState(os.path.join(tmp, "capacity.json"))
            mgr = C.CapacityManager(store=store)
            lease = mgr.acquire(_Route(), timeout=5)
            self.assertIsNotNone(lease)
            rows = store.read().get("leases") or {}
            self.assertTrue(rows, "no lease was recorded at all")
            row = rows[lease.ident]
            self.assertEqual(row.get("host"), C._HOST)
            self.assertEqual(row.get("pid"), os.getpid())


if __name__ == "__main__":
    unittest.main(verbosity=2)
