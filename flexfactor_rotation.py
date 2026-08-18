"""Pool-first rotation across every model this machine can call.

Implements `C:\\Users\\firer\\AITime\\docs\\rotation-contract.md` v1. AI Time
publishes the route catalog; this module decides which route goes next and
records what happened. Factory Deck has a TypeScript twin of this file -- if the
policy changes here it changes there, in the same commit.

Stdlib only, and it never imports flexfactor. Same shape as the other
flexfactor_* modules: the caller injects everything provider-shaped, so this
stays unit-testable without credentials, without a network, and without paying
for a single token.

The one idea worth holding on to
--------------------------------
Rotating across MODELS does not spread quota. Rotating across POOLS does.
`gpt-4o` and `gpt-4o-2024-08-06` are two model ids drawing on one OpenAI bucket;
alternating between them exhausts it at exactly the same rate as hammering one.
So selection walks POOLS, and only picks a model once a pool is chosen.

The other idea worth holding on to
----------------------------------
A $0 call must never silently become a paid one. `allow_paid` defaults False,
and when a tier runs dry the router demotes DOWN a capability tier -- it never
promotes UP a cost class to keep working. Spending money stays a decision the
caller's budget gate makes explicitly.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SCHEMA = 1

# Cost classes, cheapest first. Mirrors aitime.catalog.
LOCAL_UNLIMITED = "local-unlimited"
SUBSCRIPTION = "subscription"
FREE_TIER = "free-tier"
PAID_METERED = "paid-metered"

FREE_COST_CLASSES = (LOCAL_UNLIMITED, SUBSCRIPTION, FREE_TIER)
COST_ORDER = {LOCAL_UNLIMITED: 0, SUBSCRIPTION: 1, FREE_TIER: 2, PAID_METERED: 3}

# Capability tiers, strongest first. Demotion walks this list left to right.
FRONTIER = "frontier"
STRONG = "strong"
LIGHT = "light"
TIER_CHAIN = (FRONTIER, STRONG, LIGHT)

# Cooldowns, in seconds.
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
DEFAULT_QUOTA_COOLDOWN = 3600.0
ROUTE_ERROR_COOLDOWN = 30.0
POOL_STRIKE_COOLDOWN = 300.0
STRIKES_BEFORE_POOL_COOLDOWN = 3

# A catalog older than this is stale. Consumers warn and fall back rather than
# blocking a build on a refresh -- a 4-hour-old catalog is still overwhelmingly
# correct, and a hard stop here would take the whole factory down.
CATALOG_MAX_AGE_S = 3 * 3600.0

LOCK_STALE_S = 30.0


class RotationError(RuntimeError):
    """No route could be produced. Carries why each pool was skipped."""

    def __init__(self, message: str, reasons: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.reasons = reasons or {}


class PinUnavailable(RotationError):
    """The operator pinned a target and it cannot serve.

    Deliberately its own exception, and deliberately fatal by default. Silently
    routing a pinned job somewhere else is the "reported success, did something
    else" failure this codebase keeps re-learning; if the operator said Grok,
    either Grok runs or the run stops and says why.
    """


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def state_dir() -> str:
    return os.environ.get("AITIME_STATE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AITime")


def catalog_path() -> str:
    return os.environ.get("AI_ROTATE_CATALOG") or os.path.join(state_dir(), "routes.json")


def rotation_state_path() -> str:
    return os.environ.get("AI_ROTATE_STATE") or os.path.join(
        state_dir(), "rotation-state.json")


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Route:
    id: str
    backend: str
    backend_label: str
    model: str
    wire_model: str
    api: str
    base_url: str
    pool: str
    auth_env: str = ""
    auth_kind: str = "bearer"
    cost_class: str = PAID_METERED
    tier: str = LIGHT
    enabled: bool = True
    disabled_reason: str = ""
    quota_status: str = "unknown"
    resets_at: Optional[str] = None
    note: str = ""

    @property
    def is_free(self) -> bool:
        return self.cost_class in FREE_COST_CLASSES

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "Route":
        return cls(
            id=raw["id"], backend=raw.get("backend", ""),
            backend_label=raw.get("backend_label", ""),
            model=raw.get("model", ""), wire_model=raw.get("wire_model", ""),
            api=raw.get("api", "openai"), base_url=raw.get("base_url", ""),
            # A route with no pool would look like its own private ledger and
            # win every least-recently-used race. Fall back to the backend,
            # which is the coarsest correct grouping.
            pool=raw.get("pool") or f"{raw.get('backend', 'unknown')}:pool",
            auth_env=raw.get("auth_env", ""), auth_kind=raw.get("auth_kind", "bearer"),
            cost_class=raw.get("cost_class", PAID_METERED),
            tier=raw.get("tier", LIGHT), enabled=bool(raw.get("enabled", True)),
            disabled_reason=raw.get("disabled_reason", ""),
            quota_status=raw.get("quota_status", "unknown"),
            resets_at=raw.get("resets_at"), note=raw.get("note", ""),
        )


@dataclass
class Catalog:
    routes: List[Route]
    generated_at: str = ""
    age_seconds: float = 0.0
    path: str = ""

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > CATALOG_MAX_AGE_S

    def enabled(self) -> List[Route]:
        return [r for r in self.routes if r.enabled]


def load_catalog(path: Optional[str] = None) -> Optional[Catalog]:
    """Read the catalog, or None when it is missing or unreadable.

    None is a normal answer, not an error: the caller falls back to its existing
    provider chain. Rotation is an optimisation, never a dependency.
    """
    target = path or catalog_path()
    try:
        stat = os.stat(target)
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return None
    routes: List[Route] = []
    for entry in raw.get("routes") or []:
        try:
            routes.append(Route.from_json(entry))
        except (KeyError, TypeError):
            continue  # one malformed row must not void the catalog
    return Catalog(routes=routes, generated_at=raw.get("generated_at", ""),
                   age_seconds=max(0.0, time.time() - stat.st_mtime), path=target)


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #

def _empty_state() -> Dict[str, Any]:
    return {"schema": SCHEMA, "cursor": {}, "pools": {},
            "cooldowns": {}, "strikes": {}, "pin": {}}


REPLACE_RETRIES = 12


def _replace_with_retry(src: str, dst: str) -> None:
    """os.replace, tolerating Windows' refusal to rename over an OPEN file.

    On POSIX the rename is unconditional. On Windows, if any process has `dst`
    open for reading at that instant, MoveFileEx fails with
    PermissionError(13). Three apps share this file and some reads deliberately
    happen without the lock, so the collision is routine rather than
    exceptional -- it showed up as `PermissionError: Access is denied` under a
    four-thread test. The readers hold the file open for well under a
    millisecond, so a short backoff is enough; only a persistent failure
    propagates.
    """
    delay = 0.001
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 0.05)


class StateStore:
    """The rotation cursor, per-pool counters and cooldowns, shared on disk.

    Factory Deck, Purpose Foundry and FlexFactor all write this file, so a Grok
    call spent by one is visible to the next pick of another. Writes take a
    sidecar lock and land via os.replace, so a reader never sees a half file.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or rotation_state_path()

    # -- io ----------------------------------------------------------------
    def read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return _empty_state()
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return _empty_state()
        for key, default in (("cursor", {}), ("pools", {}), ("cooldowns", {}),
                             ("strikes", {}), ("pin", {})):
            if not isinstance(data.get(key), dict):
                data[key] = default
        return data

    def _lock_path(self) -> str:
        return self.path + ".lock"

    def _acquire(self, timeout: float = 5.0) -> bool:
        """Take the sidecar lock, backing off exponentially from 1ms.

        The backoff floor matters more than it looks. An earlier version slept
        20-50ms per attempt, which turned four concurrent workers doing 15 calls
        each into a 65-second test -- roughly a second of pure lock latency on
        every routed call. The critical section is one small read plus one
        os.replace, so the wait should be measured against that, not against
        human patience.
        """
        lock = self._lock_path()
        deadline = time.time() + timeout
        delay = 0.001
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                # Break a lock left behind by a process that died mid-write,
                # otherwise one crash wedges rotation for every app forever.
                try:
                    if time.time() - os.stat(lock).st_mtime > LOCK_STALE_S:
                        os.unlink(lock)
                        continue
                except OSError:
                    pass
            except OSError:
                return False
            if time.time() >= deadline:
                return False
            # Jitter breaks the lock-step convoy that forms when several
            # workers wake, collide, and sleep for the same interval.
            time.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 0.02)

    def _release(self) -> None:
        try:
            os.unlink(self._lock_path())
        except OSError:
            pass

    def update(self, mutate: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Read-modify-write under the lock. Returns the written state.

        If the lock cannot be taken we still apply the mutation in memory and
        return it, but skip the write. Losing a cursor tick is survivable;
        refusing to route because a lock is busy is not.
        """
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        got = self._acquire()
        try:
            data = self.read()
            mutate(data)
            if got:
                self._write(data)
            return data
        finally:
            if got:
                self._release()

    def _write(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".rotation-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            _replace_with_retry(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- pins --------------------------------------------------------------
    def set_pin(self, target: Optional[str], app: str = "global") -> None:
        def mutate(data: Dict[str, Any]) -> None:
            if target:
                data["pin"][app] = target
            else:
                data["pin"].pop(app, None)
        self.update(mutate)

    def get_pin(self, app: str = "global") -> Optional[str]:
        pins = self.read().get("pin", {})
        return pins.get(app) or pins.get("global")


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

@dataclass
class Selection:
    route: Route
    pool: str
    tier: str
    requested_tier: str
    demoted_from: Optional[str] = None
    pinned: bool = False
    catalog_stale: bool = False
    considered_pools: int = 0

    @property
    def demoted(self) -> bool:
        return self.demoted_from is not None

    def describe(self) -> str:
        bits = [f"{self.route.id} [{self.route.cost_class}/{self.tier}]"]
        if self.pinned:
            bits.append("pinned")
        if self.demoted:
            bits.append(f"demoted from {self.demoted_from}")
        if self.catalog_stale:
            bits.append("stale catalog")
        return " ".join(bits)


def _cooling(state: Dict[str, Any], key: str, now: float) -> bool:
    until = state.get("cooldowns", {}).get(key)
    return bool(until) and float(until) > now


def _pin_matches(route: Route, pin: str) -> bool:
    """A pin may name a route id, a backend, a pool, or a bare model id."""
    pin = pin.strip()
    return pin in (route.id, route.backend, route.pool, route.model, route.wire_model)


@dataclass
class Rotator:
    """Picks the next route and records what the call did.

    Holds no clients and issues no calls -- the caller maps a Selection onto its
    own provider classes. That keeps this whole file testable offline.
    """

    catalog: Catalog
    store: StateStore = field(default_factory=StateStore)
    app: str = "flexfactor"

    # -- the main entry point ---------------------------------------------
    def next_route(self, tier: str = FRONTIER, allow_paid: bool = False,
                   pin: Optional[str] = None, pin_strict: bool = True,
                   now: Optional[float] = None) -> Selection:
        """Choose the next route, and stamp the choice in the same breath.

        Read, select and stamp happen inside ONE held lock. Splitting them --
        read unlocked, select, then stamp under the lock -- lets two workers
        read the same "least recently used" pool and both pick it, so a burst of
        concurrent calls stampedes one ledger while the state file stays
        perfectly well-formed and the whole thing looks balanced.
        """
        now = time.time() if now is None else now
        requested = tier if tier in TIER_CHAIN else LIGHT
        reasons: Dict[str, str] = {}
        outcome: Dict[str, Any] = {}

        def transaction(state: Dict[str, Any]) -> None:
            resolved_pin = (pin or os.environ.get("AI_ROTATE_PIN")
                            or state.get("pin", {}).get(self.app)
                            or state.get("pin", {}).get("global"))
            if resolved_pin:
                outcome["selection"] = self._resolve_pin(
                    resolved_pin, state, now, pin_strict, tier, allow_paid, reasons)
                if outcome.get("selection") is not None:
                    self._stamp(state, outcome["selection"], now)
                return

            start = TIER_CHAIN.index(requested)
            for depth, candidate_tier in enumerate(TIER_CHAIN[start:]):
                selection = self._pick_in_tier(
                    candidate_tier, allow_paid, state, now, reasons)
                if selection is None:
                    continue
                selection.requested_tier = requested
                if depth:
                    selection.demoted_from = requested
                selection.catalog_stale = self.catalog.is_stale
                self._stamp(state, selection, now)
                outcome["selection"] = selection
                return

        try:
            self.store.update(transaction)
        except PinUnavailable:
            raise

        selection = outcome.get("selection")
        if selection is None:
            raise RotationError(
                self._no_route_message(requested, allow_paid, reasons), reasons)
        return selection

    # -- pin ---------------------------------------------------------------
    def _resolve_pin(self, pin: str, state: Dict[str, Any], now: float,
                     strict: bool, tier: str, allow_paid: bool,
                     reasons: Dict[str, str]) -> Optional[Selection]:
        """Runs INSIDE the state lock, so it must never call back into
        next_route -- that would re-enter the lock and deadlock. The non-strict
        fallback therefore inlines a tier walk instead of recursing."""
        matches = [r for r in self.catalog.routes if _pin_matches(r, pin)]
        if not matches:
            raise PinUnavailable(
                f"pinned target {pin!r} matches no route in the catalog "
                f"({len(self.catalog.routes)} routes known). Refresh with "
                f"`python -m aitime.catalog`, or clear the pin.")

        usable = [r for r in matches if r.enabled
                  and not _cooling(state, r.pool, now)
                  and not _cooling(state, f"route:{r.id}", now)]
        if not usable:
            if strict:
                why = "; ".join(
                    f"{r.id}: {r.disabled_reason or 'cooling down'}" for r in matches[:4])
                raise PinUnavailable(
                    f"pinned target {pin!r} cannot serve right now -- {why}. "
                    f"Unset the pin to let rotation choose, or wait for the reset.")
            start = TIER_CHAIN.index(tier if tier in TIER_CHAIN else LIGHT)
            for candidate_tier in TIER_CHAIN[start:]:
                fallback = self._pick_in_tier(
                    candidate_tier, allow_paid, state, now, reasons)
                if fallback is not None:
                    fallback.catalog_stale = self.catalog.is_stale
                    return fallback
            return None

        usable.sort(key=lambda r: self._route_last_used(state, r))
        return Selection(route=usable[0], pool=usable[0].pool,
                         tier=usable[0].tier, requested_tier=tier,
                         pinned=True, catalog_stale=self.catalog.is_stale)

    # -- pool-first selection ---------------------------------------------
    def _pick_in_tier(self, tier: str, allow_paid: bool, state: Dict[str, Any],
                      now: float, reasons: Dict[str, str]) -> Optional[Selection]:
        candidates: List[Route] = []
        for route in self.catalog.routes:
            if route.tier != tier:
                continue
            if not route.enabled:
                reasons.setdefault(route.pool, route.disabled_reason or "disabled")
                continue
            if not allow_paid and not route.is_free:
                reasons.setdefault(route.pool, "paid-metered, and allow_paid is off")
                continue
            if _cooling(state, route.pool, now):
                reasons[route.pool] = "pool cooling down"
                continue
            if _cooling(state, f"route:{route.id}", now):
                continue
            candidates.append(route)

        if not candidates:
            return None

        # Group by the ledger each route actually drains. THIS is the rotation.
        pools: Dict[str, List[Route]] = {}
        for route in candidates:
            pools.setdefault(route.pool, []).append(route)

        # Least-recently-used ordering IS the rotation, and it is sufficient on
        # its own: picking the oldest pool and stamping it moves that pool to
        # the back of the queue, so the next call necessarily lands elsewhere.
        #
        # An earlier draft also advanced a per-tier cursor and indexed into this
        # list. That double-rotated -- the stamp moved the chosen pool to the
        # end AND the cursor stepped past it, landing back on the same pool
        # every time. Two rotation mechanisms are one too many; the cursor
        # survives in state as a monotonic call counter for diagnostics only.
        ordered = sorted(
            pools.keys(),
            key=lambda p: (self._pool_last_used(state, p),
                           self._pool_calls(state, p), p))
        pool = ordered[0]

        routes = sorted(pools[pool], key=lambda r: (self._route_last_used(state, r),
                                                    COST_ORDER.get(r.cost_class, 9),
                                                    r.id))
        return Selection(route=routes[0], pool=pool, tier=tier,
                         requested_tier=tier, considered_pools=len(ordered))

    # -- state helpers -----------------------------------------------------
    @staticmethod
    def _pool_last_used(state: Dict[str, Any], pool: str) -> float:
        return float((state.get("pools", {}).get(pool) or {}).get("last_used_at") or 0.0)

    @staticmethod
    def _pool_calls(state: Dict[str, Any], pool: str) -> int:
        return int((state.get("pools", {}).get(pool) or {}).get("calls") or 0)

    @staticmethod
    def _route_last_used(state: Dict[str, Any], route: Route) -> float:
        return float((state.get("pools", {}).get(f"route:{route.id}") or {})
                     .get("last_used_at") or 0.0)

    @staticmethod
    def _stamp(state: Dict[str, Any], selection: Selection, now: float) -> None:
        """Mark the pool and route as just-used, in the caller's held lock.

        Stamped at SELECTION time, not on success: a call that is still in
        flight has already committed that pool's capacity, and waiting for the
        result would let every concurrent worker choose the same idle pool.
        """
        tier_cursor = state.setdefault("cursor", {})
        tier_cursor[selection.tier] = int(tier_cursor.get(selection.tier, 0)) + 1
        pools = state.setdefault("pools", {})
        entry = pools.setdefault(selection.pool, {"calls": 0, "last_used_at": 0.0})
        entry["last_used_at"] = now
        # Count the call here, optimistically, rather than waiting for
        # report("ok"). Measured on this machine every filesystem operation
        # costs 11-70ms (real-time AV scanning), so a full locked transaction is
        # ~68ms; doing two per routed call doubled that for no benefit. Counting
        # at selection lets the success path -- overwhelmingly the common one --
        # finish without a second write. A call that then fails is corrected by
        # report(), which has to write anyway to record the cooldown.
        entry["calls"] = int(entry.get("calls", 0)) + 1
        route_entry = pools.setdefault(f"route:{selection.route.id}",
                                       {"calls": 0, "last_used_at": 0.0})
        route_entry["last_used_at"] = now

    # -- feedback ----------------------------------------------------------
    def report(self, route: Route, outcome: str,
               retry_after_seconds: Optional[float] = None,
               now: Optional[float] = None) -> None:
        """Record what a call did so the next pick is better informed.

        outcome: ok | rate_limited | quota_exhausted | error
        """
        now = time.time() if now is None else now

        # A clean success on a route with no history changes nothing: the call
        # was already counted and stamped at selection. Skipping the locked
        # write here removes one ~68ms transaction from the common path. The
        # unlocked pre-check can race, but only ever costs a redundant write.
        if outcome == "ok":
            snapshot = self.store.read()
            has_strikes = route.id in (snapshot.get("strikes") or {})
            has_cooldown = f"route:{route.id}" in (snapshot.get("cooldowns") or {})
            if not has_strikes and not has_cooldown:
                return

        def mutate(data: Dict[str, Any]) -> None:
            pools = data.setdefault("pools", {})
            cooldowns = data.setdefault("cooldowns", {})
            strikes = data.setdefault("strikes", {})
            entry = pools.setdefault(route.pool, {"calls": 0, "last_used_at": 0.0})

            if outcome == "ok":
                entry["last_used_at"] = now
                strikes.pop(route.id, None)
                cooldowns.pop(f"route:{route.id}", None)
                return

            if outcome == "rate_limited":
                cooldowns[route.pool] = now + float(
                    retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN)
                return

            if outcome == "quota_exhausted":
                cooldowns[route.pool] = now + float(
                    retry_after_seconds or _seconds_until(route.resets_at, now)
                    or DEFAULT_QUOTA_COOLDOWN)
                return

            # Plain error: blame the route first. Only after it keeps failing do
            # we assume the whole pool is sick -- one bad model id must not take
            # a healthy provider out of rotation.
            cooldowns[f"route:{route.id}"] = now + ROUTE_ERROR_COOLDOWN
            count = int(strikes.get(route.id, 0)) + 1
            strikes[route.id] = count
            if count >= STRIKES_BEFORE_POOL_COOLDOWN:
                cooldowns[route.pool] = now + POOL_STRIKE_COOLDOWN
                strikes.pop(route.id, None)

        self.store.update(mutate)

    # -- diagnostics -------------------------------------------------------
    def _no_route_message(self, tier: str, allow_paid: bool,
                          reasons: Dict[str, str]) -> str:
        if not self.catalog.routes:
            return ("rotation has no routes: the catalog is empty. Run "
                    "`python -m aitime.catalog` to build it.")
        paid_waiting = [r for r in self.catalog.routes
                        if r.enabled and not r.is_free and r.tier == tier]
        head = (f"no {tier} route available "
                f"({len(self.catalog.enabled())} enabled routes in catalog)")
        if not allow_paid and paid_waiting:
            head += (f"; {len(paid_waiting)} paid-metered {tier} routes were held "
                     f"back because allow_paid is off -- the budget gate decides "
                     f"that, not rotation")
        if reasons:
            head += ". Pools skipped: " + "; ".join(
                f"{p} ({why})" for p, why in sorted(reasons.items())[:6])
        return head


def _seconds_until(iso: Optional[str], now: float) -> Optional[float]:
    if not iso:
        return None
    try:
        import datetime as _dt
        moment = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_dt.timezone.utc)
        delta = moment.timestamp() - now
        return delta if delta > 0 else None
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Construction helpers
# --------------------------------------------------------------------------- #

def rotation_enabled() -> bool:
    """Rotation is the default; AI_ROTATE=off restores prior behaviour exactly."""
    return (os.environ.get("AI_ROTATE") or "on").strip().lower() not in (
        "off", "0", "false", "no")


def build_rotator(app: str = "flexfactor",
                  catalog_file: Optional[str] = None,
                  state_file: Optional[str] = None) -> Optional[Rotator]:
    """Return a Rotator, or None when rotation is off or unusable.

    None is the honest answer when there is nothing to rotate over -- the caller
    keeps its existing provider selection. It is never a silent no-op: callers
    log the reason via `unavailable_reason`.
    """
    if not rotation_enabled():
        return None
    catalog = load_catalog(catalog_file)
    if catalog is None or not catalog.enabled():
        return None
    return Rotator(catalog=catalog, store=StateStore(state_file), app=app)


class RotatingProvider:
    """A FlexFactor provider whose backing model changes on every call.

    Duck-typed against the existing providers (`complete`, `grade`,
    `structured`, `ping`, plus `model` / `judge_model` / `meter`), so callers
    and the cost meter need no changes.

    Follows this repo's injection convention: the provider FACTORY is passed in
    rather than imported, so this module still never imports flexfactor and
    still tests offline. `factory(route)` must return an object with the
    provider surface, already pointed at that route's backend and model.
    """

    def __init__(self, rotator: Rotator, factory: Callable[[Route], Any],
                 tier: str = FRONTIER, judge_tier: str = LIGHT,
                 allow_paid: bool = False, meter: Any = None,
                 on_route: Optional[Callable[[Selection], None]] = None):
        self.rotator = rotator
        self._factory = factory
        self._tier = tier
        self._judge_tier = judge_tier
        self._allow_paid = allow_paid
        self.meter = meter
        self._on_route = on_route
        self._cache: Dict[str, Any] = {}
        # Callers read `.model` for logging and pricing. It reflects the LAST
        # route used, and is seeded with something truthful rather than a
        # hardcoded guess that would misprice the first call.
        self.model = "rotating"
        self.judge_model = "rotating"

    # -- plumbing ----------------------------------------------------------
    def _provider_for(self, route: Route) -> Any:
        provider = self._cache.get(route.id)
        if provider is None:
            provider = self._factory(route)
            if hasattr(provider, "meter"):
                provider.meter = self.meter
            self._cache[route.id] = provider
        return provider

    def _run(self, method: str, tier: str, *args, **kwargs) -> Any:
        """One rotated attempt per healthy pool, then give up honestly.

        Bounded by the number of distinct pools rather than a fixed retry count:
        retrying the same ledger cannot help, and every other ledger deserves a
        turn before the call is declared impossible.
        """
        attempts = max(1, len({r.pool for r in self.catalog_routes(tier)}))
        last_error: Optional[BaseException] = None
        for _ in range(attempts):
            selection = self.rotator.next_route(
                tier=tier, allow_paid=self._allow_paid)
            route = selection.route
            self.model = route.model
            if self._on_route:
                self._on_route(selection)
            try:
                result = getattr(self._provider_for(route), method)(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - classified, then re-raised
                self.rotator.report(route, _classify(exc), _retry_after(exc))
                last_error = exc
                if not _is_retryable(exc):
                    raise
                continue
            self.rotator.report(route, "ok")
            return result
        raise RotationError(
            f"every {tier} pool failed this call; last error was "
            f"{type(last_error).__name__}: {last_error}") from last_error

    def catalog_routes(self, tier: str) -> List[Route]:
        return [r for r in self.rotator.catalog.routes
                if r.tier == tier and r.enabled
                and (self._allow_paid or r.is_free)]

    # -- provider surface --------------------------------------------------
    def complete(self, *args, **kwargs):
        return self._run("complete", self._tier, *args, **kwargs)

    def structured(self, *args, **kwargs):
        return self._run("structured", self._tier, *args, **kwargs)

    def grade(self, *args, **kwargs):
        # Grading is classification, not authoring: it belongs on the cheap
        # tier, exactly as JUDGE_MODELS already does for the fixed providers.
        return self._run("grade", self._judge_tier, *args, **kwargs)

    def ping(self, *args, **kwargs):
        return self._run("ping", self._judge_tier, *args, **kwargs)


_RETRYABLE_MARKERS = (
    "rate limit", "rate_limit", "429", "overloaded", "capacity",
    "timeout", "timed out", "502", "503", "504", "529",
    "insufficient", "quota", "credit", "billing", "connection",
)


def _classify(exc: BaseException) -> str:
    """Map a provider exception onto a rotation outcome."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    blob = f"{type(exc).__name__} {exc}".lower()
    if status == 429 or "rate limit" in blob or "rate_limit" in blob:
        return "rate_limited"
    if any(m in blob for m in ("quota", "insufficient", "credit", "billing")):
        return "quota_exhausted"
    return "error"


def _retry_after(exc: BaseException) -> Optional[float]:
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    headers = getattr(exc, "headers", None) or {}
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        return float(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    """Whether another pool is worth trying.

    A KeyboardInterrupt or a programming error must surface immediately --
    rotating past a TypeError would burn every pool reproducing the same bug and
    report it as "all providers failed".
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError)):
        return False
    if isinstance(exc, (TypeError, AttributeError, NameError, ImportError,
                        SyntaxError, IndentationError, AssertionError)):
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and status in (400, 401, 403, 404, 422):
        return False   # a bad request stays bad on every backend
    blob = f"{type(exc).__name__} {exc}".lower()
    return bool(status) or any(m in blob for m in _RETRYABLE_MARKERS)


def unavailable_reason(catalog_file: Optional[str] = None) -> str:
    """Human-readable explanation for why build_rotator returned None."""
    if not rotation_enabled():
        return "AI_ROTATE=off"
    target = catalog_file or catalog_path()
    catalog = load_catalog(target)
    if catalog is None:
        if not os.path.exists(target):
            return (f"no route catalog at {target} -- run `python -m aitime.catalog`")
        return f"route catalog at {target} is unreadable or has the wrong schema"
    if not catalog.enabled():
        return (f"route catalog at {target} has {len(catalog.routes)} routes but "
                f"none are enabled")
    return ""
