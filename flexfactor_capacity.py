"""Global provider-capacity orchestration for concurrent FlexFactor programs.

This module owns the *shared* traffic-control layer that sits above route
selection. flexfactor_rotation already knows how to choose healthy pools and
persist provider cooldowns; this module prevents several simultaneous programs
from consuming the same allowance as though each had a private copy.

Design goals:
- one persistent ledger across threads/processes/restarts;
- FIFO admission per real allowance key;
- stale-lease recovery after crashes;
- bounded waits with jitter instead of provider stampedes;
- 429/quota cooldowns that are infrastructure state, not target-app defects;
- free mode never expands into paid capacity;
- no weakening of route-quality or verification gates.

The runtime integration is deliberately installed by ``install()`` so the
policy remains independently unit-testable and flexfactor_rotation stays usable
as a standalone library.
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
WAITER_TTL_S = 60 * 60.0
DEFAULT_WAIT_MAX_S = 30 * 60.0
LOCK_STALE_S = 30.0


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
    """Small atomic JSON store with a cross-process sidecar lock."""

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

    def _lock(self) -> str:
        return self.path + ".lock"

    def _acquire(self, timeout: float = 10.0) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + timeout
        delay = 0.001
        while True:
            try:
                fd = os.open(self._lock(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - os.stat(self._lock()).st_mtime > LOCK_STALE_S:
                        os.unlink(self._lock())
                        continue
            if time.time() >= deadline:
                return False
            time.sleep(delay + random.random() * delay)
            delay = min(0.05, delay * 2)

    def update(self, fn: Callable[[dict], Any]) -> Any:
        if not self._acquire():
            raise TimeoutError("provider capacity state lock unavailable")
        try:
            data = self.read()
            result = fn(data)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="capacity-", suffix=".json",
                                       dir=os.path.dirname(self.path))
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
            with contextlib.suppress(OSError):
                os.unlink(self._lock())


@dataclass(frozen=True)
class Lease:
    ident: str
    allowance: str
    app: str


class CapacityTimeout(RuntimeError):
    pass


class CapacityManager:
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
        # Free cloud allowances are the fragile resource exposed by the six-app
        # overnight run. Local inference can safely sustain a little parallelism;
        # paid routes are not used in free mode and retain a wider default.
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
        ticket_box: Dict[str, int] = {}
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
                    ticket_box["value"] = ticket
                ticket = int(waiters[ident]["ticket"])
                cooldown = float(data.setdefault("cooldowns", {}).get(allowance) or 0)
                if cooldown > now:
                    outcome["wait"] = max(0.05, min(30.0, cooldown - now))
                    return
                active = [row for row in data.setdefault("leases", {}).values()
                          if row.get("allowance") == allowance]
                ahead = sorted(
                    (row for wid, row in waiters.items()
                     if wid != ident and row.get("allowance") == allowance
                     and int(row.get("ticket") or 0) < ticket),
                    key=lambda row: int(row.get("ticket") or 0))
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
            # Bounded exponential delay + jitter. The persistent FIFO ticket is
            # what enforces fairness; jitter only prevents lock-step wakeups.
            sleep_for = min(outcome["wait"], delay, max(0.0, deadline - now))
            self.sleep(max(0.01, sleep_for + random.random() * min(0.05, sleep_for)))
            delay = min(5.0, delay * 2)

    def cancel_waiter(self, ident: str) -> None:
        def mutate(data: dict) -> None:
            data.setdefault("waiters", {}).pop(ident, None)
        with contextlib.suppress(Exception):
            self.store.update(mutate)

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
        if outcome not in ("rate_limited", "quota_exhausted"):
            return
        now = self.clock()
        allowance = self.allowance(route)
        if reset_at and reset_at > now:
            until = float(reset_at)
        elif retry_after_seconds:
            until = now + max(1.0, float(retry_after_seconds))
        else:
            until = now + (3600.0 if outcome == "quota_exhausted" else 60.0)
        # Account-wide limits always cool the real allowance. Pool-scoped 429s
        # are also serialized by the allowance semaphore, but do not receive a
        # long account cooldown unless the provider said they are account-wide.
        if scope != "account" and outcome == "rate_limited":
            until = min(until, now + 300.0)
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
        now = time.time()
        capacities: Dict[str, int] = {}
        for route in catalog.enabled():
            if not route.is_free or route.tier not in (rotation.FRONTIER, rotation.STRONG):
                continue
            if rotation._cooling(state, route.pool, now):
                continue
            if rotation._cooling(state, f"route:{route.id}", now):
                continue
            if rotation._cooling(state, f"allowance:{rotation.allowance_key(route)}", now):
                continue
            key = rotation.allowance_key(route)
            capacities[key] = max(capacities.get(key, 0), CapacityManager.limit(route))
        # Keep at least one lane. More programs remain queued in run_audit rather
        # than being allowed to create six independent provider storms.
        capacity = sum(capacities.values())
        return max(1, min(requested, capacity or 1))
    except Exception:
        return 1


def _waitable_rotation_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(token in text for token in (
        "no strong route available", "no frontier route available",
        "no light route available", "every strong pool failed",
        "every frontier pool failed", "every light pool failed",
        "rate limit", "quota", "allowance", "cooling down",
    ))


def install() -> None:
    """Install capacity guards into flexfactor_rotation once per interpreter."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        import flexfactor_rotation as rotation

        prior_provider_for = rotation.RotatingProvider._provider_for
        prior_report = rotation.Rotator.report
        prior_init = rotation.RotatingProvider.__init__
        prior_run = rotation.RotatingProvider._run

        def guarded_provider_for(self, route):
            provider = prior_provider_for(self, route)
            if getattr(provider, "_flexfactor_capacity_wrapped", False):
                return provider
            lock = threading.Lock()
            with lock:
                if getattr(provider, "_flexfactor_capacity_wrapped", False):
                    return provider
                for method_name in ("complete", "structured", "grade", "ping"):
                    original = getattr(provider, method_name, None)
                    if not callable(original):
                        continue
                    def make_guard(fn):
                        def guarded(*args, **kwargs):
                            app = str(getattr(self, "_purpose", "") or self.rotator.app)
                            lease = _MANAGER.acquire(route, app=app)
                            try:
                                return fn(*args, **kwargs)
                            finally:
                                _MANAGER.release(lease)
                        return guarded
                    setattr(provider, method_name, make_guard(original))
                setattr(provider, "_flexfactor_capacity_wrapped", True)
            return provider

        def report(self, route, outcome, retry_after_seconds=None, now=None,
                   scope="pool", reset_at=None):
            _MANAGER.note_outcome(route, outcome, retry_after_seconds,
                                  scope=scope, reset_at=reset_at)
            return prior_report(self, route, outcome, retry_after_seconds, now,
                                scope=scope, reset_at=reset_at)

        def init(self, *args, **kwargs):
            prior_init(self, *args, **kwargs)
            original_error = getattr(self, "_on_error", None)
            if original_error is not None:
                def infrastructure_aware_error(route, exc):
                    outcome = rotation._classify(exc)
                    if outcome in ("rate_limited", "quota_exhausted"):
                        scope, reset_at = rotation.limit_scope(exc)
                        _MANAGER.note_outcome(route, outcome, rotation._retry_after(exc),
                                              scope=scope, reset_at=reset_at)
                        return  # provider capacity problem, not target-app defect
                    return original_error(route, exc)
                self._on_error = infrastructure_aware_error

        def run(self, method, tier, *args, **kwargs):
            wait_max = float(os.environ.get(
                "FLEXFACTOR_PROVIDER_WAIT_MAX_S", DEFAULT_WAIT_MAX_S))
            deadline = time.time() + max(0.0, wait_max)
            delay = 1.0
            while True:
                try:
                    return prior_run(self, method, tier, *args, **kwargs)
                except rotation.RotationError as exc:
                    if not _waitable_rotation_error(exc) or time.time() >= deadline:
                        raise
                    # Temporary provider exhaustion is a durable wait state. Do
                    # not burn target-app error budget and do not spin.
                    snap = _MANAGER.snapshot()
                    rt = snap.get("runtime") or {}
                    detail = rt.get("detail") or str(exc)
                    print(f"  [capacity] waiting for provider: {detail}")
                    time.sleep(min(30.0, delay) + random.random() * 0.25)
                    delay = min(30.0, delay * 2)

        rotation.RotatingProvider._provider_for = guarded_provider_for
        rotation.Rotator.report = report
        rotation.RotatingProvider.__init__ = init
        rotation.RotatingProvider._run = run
        _INSTALLED = True
