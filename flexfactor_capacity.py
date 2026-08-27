"""Global provider-capacity orchestration for concurrent FlexFactor programs.

This module owns the shared traffic-control layer above route selection.
``flexfactor_rotation`` already chooses healthy pools and persists route/pool
cooldowns; this module prevents simultaneous programs from consuming the same
real provider allowance as though each worker owned a private credential.

Properties:
- one persistent ledger across threads/processes/restarts;
- FIFO admission per real allowance key;
- OS-backed cross-process locking, never stale-file lock stealing;
- renewable leases with stale-lease recovery after crashes;
- bounded exponential waits with jitter instead of provider stampedes;
- account-wide Retry-After/quota cooldowns survive restarts;
- per-route/per-pool throttles remain scoped to rotation, never widened here;
- provider exhaustion is infrastructure state, not a target-app defect;
- free mode never expands into paid capacity;
- no weakening of route-quality, build, containment, or verification gates.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

SCHEMA = 1
LEASE_TTL_S = 15 * 60.0
WAITER_TTL_S = 24 * 60 * 60.0
DEFAULT_WAIT_MAX_S = 12 * 60 * 60.0


def state_path() -> str:
    root = os.environ.get("FLEXFACTOR_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".flexfactor")
    return os.environ.get("FLEXFACTOR_PROVIDER_CAPACITY_STATE") or os.path.join(
        root, "provider-capacity.json")


def _empty() -> dict:
    return {
        "schema": SCHEMA,
        "next_ticket": 1,
        "leases": {},
        "waiters": {},
        "cooldowns": {},
        "runtime": {"state": "running", "detail": "", "updated_at": 0.0},
    }


class CapacityState:
    """Atomic JSON store protected by an OS-owned cross-process file lock."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or state_path()

    def read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return _empty()
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return _empty()
        for key in ("leases", "waiters", "cooldowns", "runtime"):
            if not isinstance(data.get(key), dict):
                data[key] = _empty()[key]
        data["next_ticket"] = max(1, int(data.get("next_ticket") or 1))
        return data

    def _lock_path(self) -> str:
        return self.path + ".lock"

    def _try_os_lock(self, fh) -> bool:
        """Acquire an exclusive nonblocking lock on an already-open handle."""
        if os.name == "nt":
            import msvcrt
            try:
                fh.seek(0)
                if os.fstat(fh.fileno()).st_size < 1:
                    fh.write(b"\0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _unlock_os(self, fh) -> None:
        """Release the OS lock held by ``fh``; closing is the final fallback."""
        if os.name == "nt":
            import msvcrt
            with contextlib.suppress(OSError):
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _acquire(self, timeout: float = 10.0):
        """Return a locked file handle; never unlink another process's lock."""
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        deadline = time.time() + timeout
        delay = 0.001
        while True:
            fh = open(self._lock_path(), "a+b")
            if self._try_os_lock(fh):
                return fh
            fh.close()
            if time.time() >= deadline:
                return None
            time.sleep(delay + random.random() * delay)
            delay = min(0.05, delay * 2)

    def update(self, fn: Callable[[dict], Any]) -> Any:
        """Read-modify-replace the ledger while holding one OS lock handle."""
        lock_handle = self._acquire()
        if lock_handle is None:
            raise TimeoutError("provider capacity state lock unavailable")
        try:
            data = self.read()
            result = fn(data)
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="capacity-", suffix=".json", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(data, fh, indent=1, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                with contextlib.suppress(OSError):
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            return result
        finally:
            self._unlock_os(lock_handle)
            lock_handle.close()


@dataclass(frozen=True)
class Lease:
    ident: str
    allowance: str
    app: str


class CapacityTimeout(RuntimeError):
    """Raised when qualified provider capacity cannot be acquired in time."""


class CapacityManager:
    """Coordinate fair, persistent provider allowance admission."""

    def __init__(self, store: Optional[CapacityState] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.store = store or CapacityState()
        self.sleep = sleep
        self.clock = clock

    @staticmethod
    def allowance(route) -> str:
        try:
            import flexfactor_rotation as rotation
            return rotation.allowance_key(route)
        except Exception:
            return f"{getattr(route, 'backend', 'unknown')}:{getattr(route, 'cost_class', 'unknown')}"

    @staticmethod
    def limit(route) -> int:
        explicit = os.environ.get("FLEXFACTOR_PROVIDER_MAX_INFLIGHT")
        if explicit:
            with contextlib.suppress(ValueError):
                return max(1, int(explicit))
        cost = str(getattr(route, "cost_class", ""))
        if cost in ("free-tier", "subscription"):
            return 1
        if cost == "local-unlimited":
            return 2
        return 4

    def _cleanup(self, data: dict, now: float) -> None:
        leases = data.setdefault("leases", {})
        for ident, row in list(leases.items()):
            if float(row.get("expires_at") or 0) <= now:
                leases.pop(ident, None)
        waiters = data.setdefault("waiters", {})
        for ident, row in list(waiters.items()):
            if now - float(row.get("created_at") or 0) > WAITER_TTL_S:
                waiters.pop(ident, None)
        cooldowns = data.setdefault("cooldowns", {})
        for key, until in list(cooldowns.items()):
            if float(until or 0) <= now:
                cooldowns.pop(key, None)

    def acquire(self, route, app: str = "flexfactor", *, timeout: Optional[float] = None) -> Lease:
        timeout = float(timeout if timeout is not None else
                        os.environ.get("FLEXFACTOR_PROVIDER_WAIT_MAX_S", DEFAULT_WAIT_MAX_S))
        allowance = self.allowance(route)
        ident = uuid.uuid4().hex
        created = self.clock()
        deadline = created + max(0.0, timeout)
        delay = 0.05

        while True:
            now = self.clock()
            outcome: Dict[str, Any] = {"granted": False, "wait": 0.05}

            def mutate(data: dict) -> None:
                self._cleanup(data, now)
                waiters = data.setdefault("waiters", {})
                if ident not in waiters:
                    ticket = int(data.get("next_ticket") or 1)
                    data["next_ticket"] = ticket + 1
                    waiters[ident] = {
                        "ticket": ticket, "allowance": allowance, "app": app,
                        "created_at": created, "pid": os.getpid(),
                    }
                ticket = int(waiters[ident]["ticket"])
                cooldown = float(data.setdefault("cooldowns", {}).get(allowance) or 0)
                if cooldown > now:
                    outcome["wait"] = max(0.05, min(30.0, cooldown - now))
                    data["runtime"] = {
                        "state": "waiting-for-provider",
                        "detail": f"{app} waiting for shared allowance {allowance} cooldown",
                        "updated_at": now,
                    }
                    return
                active = [row for row in data.setdefault("leases", {}).values()
                          if row.get("allowance") == allowance]
                ahead = [row for wid, row in waiters.items()
                         if wid != ident and row.get("allowance") == allowance
                         and int(row.get("ticket") or 0) < ticket]
                if len(active) < self.limit(route) and not ahead:
                    data["leases"][ident] = {
                        "allowance": allowance, "app": app, "pid": os.getpid(),
                        "thread": threading.get_ident(), "started_at": now,
                        "expires_at": now + LEASE_TTL_S,
                    }
                    waiters.pop(ident, None)
                    outcome["granted"] = True
                    data["runtime"] = {"state": "running", "detail": "provider capacity granted",
                                       "updated_at": now}
                    return
                data["runtime"] = {
                    "state": "waiting-for-provider",
                    "detail": f"{app} queued for shared allowance {allowance}",
                    "updated_at": now,
                }

            self.store.update(mutate)
            if outcome["granted"]:
                return Lease(ident, allowance, app)
            if now >= deadline:
                self.cancel_waiter(ident)
                raise CapacityTimeout(
                    f"provider capacity wait exceeded {timeout:.0f}s for {allowance}")
            sleep_for = min(outcome["wait"], delay, max(0.0, deadline - now))
            self.sleep(max(0.01, sleep_for + random.random() * min(0.05, sleep_for)))
            delay = min(5.0, delay * 2)

    def cancel_waiter(self, ident: str) -> None:
        def mutate(data: dict) -> None:
            data.setdefault("waiters", {}).pop(ident, None)
        with contextlib.suppress(Exception):
            self.store.update(mutate)

    def renew(self, lease: Lease) -> bool:
        """Extend a live lease so a long provider call cannot be double-admitted."""
        now = self.clock()
        renewed = {"ok": False}

        def mutate(data: dict) -> None:
            row = data.setdefault("leases", {}).get(lease.ident)
            if not row or row.get("allowance") != lease.allowance:
                return
            row["expires_at"] = now + LEASE_TTL_S
            renewed["ok"] = True

        try:
            self.store.update(mutate)
        except Exception:
            return False
        return renewed["ok"]

    def release(self, lease: Lease) -> None:
        now = self.clock()

        def mutate(data: dict) -> None:
            self._cleanup(data, now)
            data.setdefault("leases", {}).pop(lease.ident, None)
            if not data["leases"] and not data.setdefault("waiters", {}):
                data["runtime"] = {"state": "running", "detail": "provider capacity available",
                                   "updated_at": now}
        with contextlib.suppress(Exception):
            self.store.update(mutate)

    def note_outcome(self, route, outcome: str, retry_after_seconds: Optional[float] = None,
                     *, scope: str = "pool", reset_at: Optional[float] = None) -> None:
        """Persist only account-wide exhaustion at allowance scope."""
        if outcome not in ("rate_limited", "quota_exhausted"):
            return
        now = self.clock()
        allowance = self.allowance(route)
        if scope != "account":
            def mark_wait(data: dict) -> None:
                self._cleanup(data, now)
                data["runtime"] = {
                    "state": "waiting-for-provider",
                    "detail": f"{outcome}: route/pool cooldown remains scoped in rotation",
                    "updated_at": now,
                }
            with contextlib.suppress(Exception):
                self.store.update(mark_wait)
            return
        if reset_at and reset_at > now:
            until = float(reset_at)
        elif retry_after_seconds:
            until = now + max(1.0, float(retry_after_seconds))
        else:
            until = now + (3600.0 if outcome == "quota_exhausted" else 60.0)

        def mutate(data: dict) -> None:
            self._cleanup(data, now)
            data.setdefault("cooldowns", {})[allowance] = max(
                float(data["cooldowns"].get(allowance) or 0), until)
            data["runtime"] = {
                "state": "waiting-for-provider",
                "detail": f"{outcome}: {allowance} cooling until {until:.0f}",
                "updated_at": now,
            }
        with contextlib.suppress(Exception):
            self.store.update(mutate)

    def snapshot(self) -> dict:
        now = self.clock()

        def mutate(data: dict) -> dict:
            self._cleanup(data, now)
            return json.loads(json.dumps(data))
        return self.store.update(mutate)


_MANAGER = CapacityManager()
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()
_PROVIDER_WRAP_LOCK = threading.Lock()


def manager() -> CapacityManager:
    return _MANAGER


def recommended_program_parallelism(requested: int, model_mode: str = "free") -> int:
    """Admission-control program lanes from currently usable strong allowances."""
    requested = max(1, int(requested or 1))
    if str(model_mode or "free").lower() != "free":
        return requested
    try:
        import flexfactor_rotation as rotation
        catalog = rotation.load_catalog()
        if catalog is None:
            return 1
        state = rotation.StateStore().read()
        now = time