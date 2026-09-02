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

Best-available mode is an explicit product policy, not an accidental retry.
It consumes the strongest usable paid/subscription capacity first, descends
through weaker paid tiers only when that capacity is unavailable, and reaches
free/local capacity last. Quota and credit refusals cool their real allowance,
so the next attempt continues down the ladder instead of hammering it.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import threading
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
# Authentication failures normally persist until a credential is refreshed.
# Bench the credential ledger long enough to avoid re-testing every model that
# shares it, while still allowing the same call to continue on another backend.
AUTH_FAILURE_COOLDOWN = 3600.0
# A route whose own TRANSPORT is dead here (a CLI binary that this account
# cannot drive, one that has to be killed at its deadline) is not having a bad
# minute -- it cannot serve this machine at all. MEASURED 2026-08-24 in
# `local-ai-factory-20260824-005448-500119-21424`: `cli/codex` was selected and
# failed 30 times with "The 'gpt-5.6-sol' model is not supported when using
# Codex with a ChatGPT account", and `cli/claude-code` 23 times, each after
# burning its full 600-second deadline. At ROUTE_ERROR_COOLDOWN that route is
# back in the draw half a minute later; that run reviewed 2 of 287 files.
TRANSPORT_DEAD_COOLDOWN = 3600.0

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


class ReviewerSeparationError(RotationError):
    """Independent review is impossible under the current route identities."""


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
    # Purpose sight. Empty = unknown (never a disqualifier). Source is
    # "measured" (bench_battery.py, local routes) or "declared" (tier/family,
    # cloud routes) so a consumer can tell evidence from assertion.
    capabilities: Tuple[str, ...] = ()
    capabilities_source: str = ""

    @property
    def is_free(self) -> bool:
        return self.cost_class in FREE_COST_CLASSES

    @property
    def uses_paid_capacity(self) -> bool:
        """Whether this route consumes an account the owner pays for.

        Subscription routes have zero marginal token price, but they still
        consume a paid account's allowance and belong above genuinely free
        tiers in the owner's best-available ladder.
        """
        return self.cost_class in (SUBSCRIPTION, PAID_METERED)

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
            # Older catalogs have neither field; that is "unknown", not empty-
            # on-purpose, and the rotator treats it as such.
            capabilities=tuple(str(c) for c in (raw.get("capabilities") or [])
                               if isinstance(c, str)),
            capabilities_source=str(raw.get("capabilities_source") or ""),
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


def catalog_staleness_note(catalog: Optional[Catalog]) -> Optional[str]:
    """One actionable sentence when the route catalog is stale, else None.

    The warning is deliberately NOT suppressed and deliberately NOT acted on.
    Not suppressed, because a stale catalog can still be offering a route whose
    quota died hours ago -- silence would turn that into an unexplained error
    tour. Not acted on, because the catalog belongs to AI Time: regenerating
    another program's state behind the owner's back is not FlexFactor's call, so
    this names the exact command and stops there.

    Says WHICH file, HOW old, and WHAT to run -- "stale catalog" on its own told
    the reader nothing they could do.
    """
    if catalog is None or not catalog.is_stale:
        return None
    hours = catalog.age_seconds / 3600.0
    return (f"route catalog is STALE: {catalog.path} is {hours:.1f}h old "
            f"(limit {CATALOG_MAX_AGE_S / 3600.0:.0f}h), so a route whose quota "
            f"has since died can still be selected. "
            f"Refresh with: python -m aitime.catalog  (run it from the "
            f"AITime checkout -- the bare module name does NOT resolve from "
            f"another directory, it exits ModuleNotFoundError: No module "
            f"named 'aitime')")


def _rotation_extensions_enabled() -> bool:
    """True unless extensions are explicitly disabled. See flexfactor_flags."""
    try:
        from flexfactor_flags import rotation_extensions_enabled
        return rotation_extensions_enabled()
    except ImportError:
        # Standalone use (this module never hard-depends on the rest of the
        # tool). Mirror the shared default rather than the old exact-"1" rule,
        # or the fallback silently reintroduces the drift it replaced.
        return os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS", "").strip().lower() \
            not in ("0", "false", "no", "off")


def _merge_auto_routes(routes: List[Route]) -> List[Route]:
    """Append auto-discovered Cursor/ai-time routes when extensions are on.

    Safe to call unconditionally: returns *routes* unchanged when the feature
    flag is absent or when the discovery module cannot be imported.
    """
    if not _rotation_extensions_enabled():
        return routes
    try:
        import flexfactor_discovery as _disc  # noqa: PLC0415
        extra_dicts = _disc.load_auto_catalog()
        if not extra_dicts:
            return routes
        existing_ids = {r.id for r in routes}
        merged = list(routes)
        for entry in extra_dicts:
            try:
                r = Route.from_json(entry)
            except (KeyError, TypeError):
                continue
            if r.id not in existing_ids:
                merged.append(r)
                existing_ids.add(r.id)
        return merged
    except ImportError:
        return routes


def load_catalog(path: Optional[str] = None) -> Optional[Catalog]:
    """Read the catalog, or None when it is missing or unreadable.

    None is a normal answer, not an error: the caller falls back to its existing
    provider chain. Rotation is an optimisation, never a dependency.

    When `FLEXFACTOR_ROTATION_EXTENSIONS=1` is set, routes from the
    auto-discovered `catalog.auto.json` (written by `flexfactor_discovery`) are
    merged in after the primary catalog so Cursor and ai-time models participate
    in pool-first rotation.
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
    routes = _merge_auto_routes(routes)
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

# --------------------------------------------------------------------------- #
# Purpose sight: what a call is FOR, so selection can fit the model to the job
# --------------------------------------------------------------------------- #

# Capability names. Local routes carry these MEASURED (bench_battery.py);
# cloud routes carry them DECLARED by tier/family. A route with an empty list
# is "unknown" and is never excluded on capability grounds -- the absence of a
# measurement must not masquerade as a failed one.
CAP_CODE_AUTHOR = "code_author"       # executed a planted-defect repair
CAP_STRUCTURED_JSON = "structured_json"
CAP_CODE_REVIEW = "code_review"       # found planted review defects
CAP_HONEST = "honest"                 # admitted it had not seen a file
CAP_VISION = "vision"

ROLE_AUTHOR = "author"
ROLE_REVIEWER = "reviewer"
ROLE_JUDGE = "judge"
ROLE_VISION = "vision"


@dataclass(frozen=True)
class CallIntent:
    """What this call is for. The rotator fits the route to it.

    role          -- author | reviewer | judge | vision
    needs         -- capabilities the route MUST have (when its list is known)
    avoid_family  -- optional soft exclusion used by legacy callers.
    avoid_families -- strict exclusions. Production reviewer calls carry every
                      family that authored any part of the candidate; if no
                      different family is available, review fails closed.
    purpose       -- short slug of the program purpose this call serves; it is
                     recorded on the selection so the journal can answer "what
                     was this model working toward?".
    """
    role: str = ROLE_AUTHOR
    needs: Tuple[str, ...] = ()
    avoid_family: Optional[str] = None
    purpose: str = ""
    avoid_families: Tuple[str, ...] = ()

    def with_purpose(self, purpose: str, extra_needs: Sequence[str] = ()) -> "CallIntent":
        needs = tuple(dict.fromkeys(tuple(self.needs) + tuple(extra_needs)))
        return CallIntent(self.role, needs, self.avoid_family,
                          purpose or self.purpose, self.avoid_families)


@dataclass
class RoleCoordinator:
    """Run-scoped author/reviewer identity shared by all ladder instances."""

    last_family: Dict[str, str] = field(default_factory=dict)
    last_selection: Dict[str, "Selection"] = field(default_factory=dict)
    author_families: set[str] = field(default_factory=set)
    lock: Any = field(default_factory=threading.Lock, repr=False)


_FAMILY_PATTERNS = (
    ("claude", "anthropic"), ("gpt-oss", "gpt-oss"), ("gpt-", "openai"),
    ("codex", "openai"), ("o1", "openai"),
    ("o3", "openai"), ("o4", "openai"), ("gemma", "gemma"), ("gemini", "gemini"),
    ("qwen", "qwen"), ("llama", "llama"), ("mistral", "mistral"), ("mixtral", "mistral"),
    ("codestral", "mistral"), ("deepseek", "deepseek"), ("phi", "phi"), ("grok", "xai"),
    ("glimmer", "muse"), ("muse", "muse"), ("kimi", "kimi"), ("glm", "glm"),
    ("nemotron", "nvidia"), ("command", "cohere"),
)


def model_family(model_id: str) -> str:
    """Coarse family of a model id, for author/reviewer independence.

    Looks at the LAST path segment so 'openrouter/qwen/qwen3.6-27b' and
    'ollama/qwen3-coder:30b' both say 'qwen'. Unknown deployment aliases are
    opaque: distinct labels do not prove distinct underlying model families.
    """
    seg = str(model_id or "").lower().split("/")[-1]
    for needle, fam in _FAMILY_PATTERNS:
        if needle in seg:
            return fam
    # An arbitrary deployment alias is not model-family evidence. Treat every
    # unrecognized wire identity as opaque so `author-prod` and `review-prod`
    # cannot falsely certify two aliases of the same underlying model as
    # independent families.
    return "unknown"


def route_model_family(route: Route) -> str:
    """Family of the concrete model identity actually sent to a provider.

    ``model`` may be a display/catalog label while ``wire_model`` is what the
    provider factory executes.  Independence decisions must follow the latter
    whenever it is present; otherwise a mislabeled route can evade both author
    recording and reviewer exclusion.
    """
    return model_family(route.wire_model or route.model)


# These labels describe a routing decision, not the concrete model that served
# the call.  They cannot establish cross-family independence.  Ordinary work
# may still use such routes, but a semantic authorization that explicitly
# requires a non-author family must fail closed until the route is pinned or
# reports its actual model identity.
_OPAQUE_MODEL_FAMILIES = frozenset({
    "auto", "automatic", "best", "default", "latest", "recommended", "unknown",
})


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
    # Purpose sight: why this route, for what.
    intent_role: str = ""
    purpose: str = ""
    fit: str = ""              # measured | declared | unknown
    family_note: str = ""      # set when avoid_family could not be honoured

    @property
    def demoted(self) -> bool:
        return self.demoted_from is not None

    def describe(self) -> str:
        bits = [f"{self.route.id} [{self.route.cost_class}/{self.tier}]"]
        if self.pinned:
            bits.append("pinned")
        if self.demoted:
            bits.append(f"demoted from {self.demoted_from}")
        if self.intent_role:
            bits.append(f"as {self.intent_role}" + (f" ({self.fit})" if self.fit else ""))
        if self.purpose:
            bits.append(f"for {self.purpose}")
        if self.family_note:
            bits.append(self.family_note)
        # `catalog_stale` is DELIBERATELY not rendered here. Staleness is a fact
        # about the CATALOG FILE, not about this route, and the caller prints one
        # line per distinct route -- so a long run repeated "stale catalog" once
        # per rotated route (measured 2026-08-19: ~30 lines in a single live
        # run), burying everything else in the log. The field stays on the
        # Selection because consumers still need the answer; the one-per-run
        # warning is `catalog_staleness_note()` below.
        return " ".join(bits)


def allowance_key(route: "Route") -> str:
    """The ledger a route ACTUALLY drains, when its pool name does not say so.

    MEASURED from `%LOCALAPPDATA%\\AITime\\routes.json`, 2026-08-24: the catalog
    carries 19 enabled OpenRouter free routes under 18 DIFFERENT pool names, one
    per model (`openrouter:free:cohere/north-mini-code:free`, ...). OpenRouter's
    free tier is ONE account-wide daily allowance, so pool-first rotation - whose
    entire premise is that a pool IS a ledger - was handed eighteen ledgers where
    there is one. 574 of the 898 error entries across that day's two 10-program
    audits are that single exhausted allowance, re-tried per synthetic pool.

    Backend + cost class, because they are different allowances on the same
    backend: exhausting `openrouter:free-tier` must never bench the paid
    `openrouter:paid-metered` credits, and vice versa.
    """
    return f"{route.backend}:{route.cost_class}"


def credential_key(route: "Route") -> str:
    """The backend-scoped credential shared by one or more model routes."""
    identity = route.auth_env or route.auth_kind or route.cost_class
    return f"{route.backend}:{identity}"


# A 429/quota refusal whose SCOPE is the whole account, not this model. Only
# shapes measured in this toolchain's own ledgers; a guess here would bench a
# healthy backend.
_ACCOUNT_WIDE_LIMIT_MARKERS = (
    # OpenRouter, verbatim: "Rate limit exceeded: free-models-per-day" with
    # "limit_source": "openrouter_free_tier_daily".
    "free-models-per-day", "_free_tier_daily", "free_tier_daily",
    # Google Gemini free tier: "Quota exceeded for metric:
    # generativelanguage.googleapis.com/generate_content_free_tier_requests".
    "free_tier_requests",
)

_RESET_EPOCH_RE = re.compile(r"x-ratelimit-reset['\"]?\s*[:=]\s*['\"]?(\d{9,16})")


def limit_scope(exc: BaseException) -> Tuple[str, Optional[float]]:
    """("account"|"pool", reset epoch seconds or None) for a rate/quota refusal.

    The provider's own body carries both answers and neither was ever read:
    `limit_source: openrouter_free_tier_daily` says the allowance is account-
    wide, and `X-RateLimit-Reset: 1787616000000` says exactly when it comes
    back (epoch ms - the next UTC midnight). Contrast Groq's per-model
    "tokens per minute (TPM) ... try again in 10.132s", which is correctly a
    short cooldown on one pool.
    """
    blob = f"{type(exc).__name__} {exc}".lower()
    scope = "account" if any(m in blob for m in _ACCOUNT_WIDE_LIMIT_MARKERS) else "pool"
    until: Optional[float] = None
    match = _RESET_EPOCH_RE.search(blob)
    if match:
        raw = float(match.group(1))
        # Providers send seconds or milliseconds; both appear in the wild.
        candidate = raw / 1000.0 if raw > 1e11 else raw
        # Only trust a reset that is in the future and inside a day - a bogus
        # far-future stamp must not bench an allowance for a decade.
        if 0 < candidate - time.time() <= 25 * 3600:
            until = candidate
    return scope, until


def _cooling(state: Dict[str, Any], key: str, now: float) -> bool:
    until = state.get("cooldowns", {}).get(key)
    return bool(until) and float(until) > now


def _yield(entry: Dict[str, Any]) -> float:
    """Laplace-smoothed share of attempts whose work was verified.

    (verified + 1) / (attempts + 2): a route with no history scores 0.5 --
    neither trusted nor punished -- and a single bad result cannot sink it.
    """
    v = int(entry.get("verified", 0) or 0)
    attempts = v + sum(int(entry.get(s, 0) or 0) for s in ("rejected", "noop", "build_failed"))
    return (v + 1.0) / (attempts + 2.0)


def _route_yield(state: Dict[str, Any], route: Route, purpose: str) -> float:
    q = (state.get("quality") or {}).get(route.id) or {}
    entry = q.get(purpose or "*")
    if entry is None and purpose:
        entry = q.get("*")
    return _yield(entry or {})


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
                   now: Optional[float] = None,
                   intent: Optional[CallIntent] = None,
                   paid_first: bool = False) -> Selection:
        """Choose the next route, and stamp the choice in the same breath.

        Read, select and stamp happen inside ONE held lock. Splitting them --
        read unlocked, select, then stamp under the lock -- lets two workers
        read the same "least recently used" pool and both pick it, so a burst of
        concurrent calls stampedes one ledger while the state file stays
        perfectly well-formed and the whole thing looks balanced.
        """
        now = time.time() if now is None else now
        requested = tier if tier in TIER_CHAIN else LIGHT
        if intent is not None and intent.avoid_families:
            permitted_tiers = set(TIER_CHAIN[TIER_CHAIN.index(requested):])
            excluded = set(intent.avoid_families) | set(_OPAQUE_MODEL_FAMILIES)
            permanent_alternatives = [
                route for route in self.catalog.routes
                if route.enabled
                and route.tier in permitted_tiers
                and (allow_paid or route.is_free)
                and route_model_family(route) not in excluded
                and not (intent.needs and route.capabilities
                         and any(need not in route.capabilities
                                 for need in intent.needs))
            ]
            if not permanent_alternatives:
                raise ReviewerSeparationError(
                    "independent reviewer family unavailable: no enabled route "
                    "has a recognized non-author model family"
                )
        reasons: Dict[str, str] = {}
        outcome: Dict[str, Any] = {}

        def transaction(state: Dict[str, Any]) -> None:
            # A best-available call is policy-owned: an old environment or
            # state-file pin may not jump ahead of the strongest unexhausted
            # paid route. Pins remain available to non-product/legacy rotator
            # clients that do not request the paid-first ladder.
            resolved_pin = None if paid_first else (
                pin or os.environ.get("AI_ROTATE_PIN")
                or state.get("pin", {}).get(self.app)
                or state.get("pin", {}).get("global")
            )
            if resolved_pin:
                outcome["selection"] = self._resolve_pin(
                    resolved_pin, state, now, pin_strict, tier, allow_paid, reasons)
                if outcome.get("selection") is not None:
                    self._stamp(state, outcome["selection"], now)
                return

            start = TIER_CHAIN.index(requested)
            tiers = TIER_CHAIN[start:]
            # Best-available is a single descending ladder: every paid or
            # subscription tier, strongest first, followed by every free tier.
            # The ordinary rotator keeps its original per-tier behavior.
            cost_phases = (True, False) if paid_first and allow_paid else (None,)
            for paid_capacity in cost_phases:
                for depth, candidate_tier in enumerate(tiers):
                    selection = self._pick_in_tier(
                        candidate_tier, allow_paid, state, now, reasons, intent,
                        paid_first=paid_first,
                        paid_capacity=paid_capacity)
                    if selection is None:
                        continue
                    selection.requested_tier = requested
                    if depth:
                        selection.demoted_from = requested
                    selection.catalog_stale = self.catalog.is_stale
                    if intent is not None:
                        selection.intent_role = intent.role
                        selection.purpose = intent.purpose
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

    def has_usable_route(self, tier: str = FRONTIER, *,
                         allow_paid: bool = False,
                         intent: Optional[CallIntent] = None,
                         paid_first: bool = False,
                         paid_capacity: Optional[bool] = None,
                         now: Optional[float] = None) -> bool:
        """Non-mutating availability probe using the real selection gates.

        This deliberately delegates to ``_pick_in_tier`` rather than maintaining
        a second, inevitably drifting definition of "available".  It therefore
        observes pool, allowance, credential, route and purpose cooldowns as
        well as capability and reviewer-family constraints, without stamping a
        selection or consuming a rotation turn.
        """
        moment = time.time() if now is None else now
        requested = tier if tier in TIER_CHAIN else LIGHT
        state = self.store.read()
        reasons: Dict[str, str] = {}
        start = TIER_CHAIN.index(requested)
        return any(
            self._pick_in_tier(
                candidate_tier, allow_paid, state, moment, reasons, intent,
                paid_first=paid_first, paid_capacity=paid_capacity,
            ) is not None
            for candidate_tier in TIER_CHAIN[start:]
        )

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
                f"`python -m aitime.catalog` run FROM the AITime checkout "
                f"(the bare module name does not resolve elsewhere), or "
                f"clear the pin.")

        # A pin must clear the same cost and account-wide gates as rotation:
        # the pin can come from the SHARED state file (another app's "global"
        # pin), so honoring it blind here let a $0 call silently go paid and
        # re-selected an exhausted daily allowance on every call.
        usable = [r for r in matches if r.enabled
                  and (allow_paid or r.is_free)
                  and not _cooling(state, r.pool, now)
                  and not _cooling(state, f"route:{r.id}", now)
                  and not _cooling(state, f"credential:{credential_key(r)}", now)
                  and not _cooling(state, f"allowance:{allowance_key(r)}", now)]
        if not usable:
            if strict:
                def _why(r: Route) -> str:
                    if not r.enabled:
                        return r.disabled_reason or "disabled"
                    if not allow_paid and not r.is_free:
                        return "paid-metered, and allow_paid is off"
                    if _cooling(state, f"allowance:{allowance_key(r)}", now):
                        return (f"{allowance_key(r)} allowance exhausted "
                                "(account-wide)")
                    if _cooling(state, f"credential:{credential_key(r)}", now):
                        return f"{credential_key(r)} credential rejected"
                    return "cooling down"
                why = "; ".join(f"{r.id}: {_why(r)}" for r in matches[:4])
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
                      now: float, reasons: Dict[str, str],
                      intent: Optional[CallIntent] = None,
                      paid_first: bool = False,
                      paid_capacity: Optional[bool] = None) -> Optional[Selection]:
        candidates: List[Route] = []
        for route in self.catalog.routes:
            if route.tier != tier:
                continue
            if not route.enabled:
                reasons.setdefault(route.pool, route.disabled_reason or "disabled")
                continue
            paid_when_forbidden = (
                route.uses_paid_capacity if paid_first else not route.is_free
            )
            if not allow_paid and paid_when_forbidden:
                reasons.setdefault(route.pool, "paid-metered, and allow_paid is off")
                continue
            if (paid_capacity is not None
                    and route.uses_paid_capacity is not paid_capacity):
                continue
            if _cooling(state, route.pool, now):
                reasons[route.pool] = "pool cooling down"
                continue
            # The ACCOUNT-WIDE allowance behind this pool. A catalog that names
            # one ledger per model (see allowance_key) would otherwise let one
            # exhausted daily quota be re-tried eighteen times per call.
            if _cooling(state, f"allowance:{allowance_key(route)}", now):
                reasons[route.pool] = (f"{allowance_key(route)} allowance "
                                       "exhausted (account-wide)")
                continue
            if _cooling(state, f"credential:{credential_key(route)}", now):
                reasons[route.pool] = (f"{credential_key(route)} credential "
                                       "rejected")
                continue
            if _cooling(state, f"route:{route.id}", now):
                continue
            # Chronically off-purpose for THIS program (see report_quality):
            # skipped here, for this purpose only, with the reason visible.
            # The read mirrors the WRITE key (`purpose or "*"`): a cooldown
            # recorded under "*" (empty purpose) was written to shared state
            # and never consulted, and a purposed call also honors the "*"
            # fallback exactly as _route_yield already does.
            if intent is not None and (
                    _cooling(state, f"route:{route.id}@{intent.purpose or '*'}", now)
                    or (intent.purpose
                        and _cooling(state, f"route:{route.id}@*", now))):
                reasons.setdefault(route.pool, f"{route.id} cooled down: low yield for "
                                               f"'{intent.purpose or '*'}'")
                continue
            # PURPOSE FIT, before pool selection. A route whose capability
            # list is KNOWN and lacks a hard need is not a candidate for this
            # call -- it would be picked first (cheapest) and then do the wrong
            # job. A route with NO capability data is kept (unknown is not
            # failed) and ranked after known-fit routes inside its pool.
            if intent is not None and intent.needs and route.capabilities:
                missing = [n for n in intent.needs if n not in route.capabilities]
                if missing:
                    reasons.setdefault(
                        route.pool, f"lacks {','.join(missing)} for role {intent.role}")
                    continue
            candidates.append(route)

        if not candidates:
            return None

        family_note = ""
        strict_families = set(intent.avoid_families) if intent is not None else set()
        soft_families = ({intent.avoid_family}
                         if intent is not None and intent.avoid_family else set())
        if strict_families:
            strict_others = [
                r for r in candidates
                if route_model_family(r) not in strict_families
                and route_model_family(r) not in _OPAQUE_MODEL_FAMILIES
            ]
            if strict_others:
                candidates = strict_others
            else:
                label = ",".join(sorted(strict_families))
                reasons.setdefault(
                    f"reviewer-family:{label}",
                    "no model family independent from every candidate author",
                )
                return None
        if soft_families:
            soft_others = [r for r in candidates
                           if route_model_family(r) not in soft_families]
            if soft_others:
                candidates = soft_others
            else:
                family_note = (f"no alternative to family '{intent.avoid_family}' "
                               f"for {intent.role}; independence NOT achieved")

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
        # Best-available mode ranks by observed verified yield and then by the
        # catalog's declared order. It deliberately does not rotate a healthy
        # top route to the back: the owner asked to keep using the best paid
        # model until its allowance is unavailable, then work down to free.
        def _paid_pool(p: str) -> bool:
            return all(r.uses_paid_capacity for r in pools[p])

        catalog_order = {route.id: index for index, route in enumerate(self.catalog.routes)}
        purpose = intent.purpose if intent is not None else ""

        def _best_pool_yield(pool_name: str) -> float:
            return max(_route_yield(state, route, purpose) for route in pools[pool_name])

        def _pool_catalog_order(pool_name: str) -> int:
            return min(catalog_order.get(route.id, len(catalog_order))
                       for route in pools[pool_name])

        if paid_first:
            def pool_key(p: str) -> tuple:
                return (0 if _paid_pool(p) else 1,
                        _pool_catalog_order(p), -_best_pool_yield(p), p)
        else:
            def pool_key(p: str) -> tuple:
                return (self._pool_last_used(state, p),
                        self._pool_calls(state, p), p)
        ordered = sorted(pools.keys(), key=pool_key)
        pool = ordered[0]

        def _fit_rank(r: Route) -> int:
            # Known-fit (measured beats declared) ahead of unknown, inside the
            # pool that LRU already chose. Pool order is never changed by fit.
            if intent is None or not intent.needs:
                return 0
            if not r.capabilities:
                return 2
            return 0 if r.capabilities_source == "measured" else 1

        if paid_first:
            def route_key(r: Route) -> tuple:
                return (_fit_rank(r), catalog_order.get(r.id, len(catalog_order)),
                        -_route_yield(state, r, purpose), r.id)
        else:
            def route_key(r: Route) -> tuple:
                return (_fit_rank(r), -_route_yield(state, r, purpose),
                        self._route_last_used(state, r),
                        COST_ORDER.get(r.cost_class, 9), r.id)
        routes = sorted(pools[pool], key=route_key)
        chosen = routes[0]
        fit = ("" if intent is None or not intent.needs else
               (chosen.capabilities_source or "declared") if chosen.capabilities else "unknown")
        return Selection(route=chosen, pool=pool, tier=tier,
                         requested_tier=tier, considered_pools=len(ordered),
                         fit=fit, family_note=family_note)

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
    # -- purpose effectiveness ----------------------------------------------
    # `report` says whether a call was SERVED. This says whether the work the
    # model produced HELPED -- the signal the callers already compute (a fix
    # landed and was verified; the independent reviewer rejected it; the model
    # produced a no-op; the build gate failed on its edit) attributed to the
    # route and the program purpose. Two uses in selection:
    #   * inside the LRU-chosen pool, prefer the route with the better yield
    #     for this purpose (pool order is untouched, so quota still spreads);
    #   * a route that is chronically off-purpose for THIS program is cooled
    #     down for this program only, with the reason on the selection.
    # Shared state, so FlexFactor and Factory Deck learn from each other.
    QUALITY_SIGNALS = ("verified", "rejected", "noop", "build_failed")
    QUALITY_MIN_ATTEMPTS = 5
    QUALITY_FLOOR = 0.25            # yield below this, after enough tries -> cooldown
    QUALITY_COOLDOWN_S = 1800.0

    def report_quality(self, route: Route, signal: str, purpose: str = "",
                       now: Optional[float] = None) -> Optional[str]:
        """Record that this route's work helped (or did not) for a purpose.

        Returns the cooldown note when this report tipped the route into a
        purpose-scoped cooldown, else None. Never raises on a bad signal: an
        unknown signal is recorded under "other" so it is still visible.
        """
        now = time.time() if now is None else now
        signal = signal if signal in self.QUALITY_SIGNALS else "other"
        key = purpose or "*"
        note: Dict[str, Optional[str]] = {"cooldown": None}

        def mutate(data: Dict[str, Any]) -> None:
            q = data.setdefault("quality", {})
            per_route = q.setdefault(route.id, {})
            entry = per_route.setdefault(key, {"verified": 0, "rejected": 0, "noop": 0,
                                               "build_failed": 0, "other": 0,
                                               "last_at": 0.0})
            entry[signal] = int(entry.get(signal, 0)) + 1
            entry["last_at"] = now
            attempts = sum(int(entry.get(s, 0)) for s in self.QUALITY_SIGNALS)
            if attempts >= self.QUALITY_MIN_ATTEMPTS and _yield(entry) < self.QUALITY_FLOOR:
                data.setdefault("cooldowns", {})[f"route:{route.id}@{key}"] = now + self.QUALITY_COOLDOWN_S
                note["cooldown"] = (f"{route.id} cooled down {int(self.QUALITY_COOLDOWN_S // 60)} min "
                                    f"for '{key}': yield {_yield(entry):.2f} over {attempts} "
                                    f"attempt(s) is below {self.QUALITY_FLOOR}")

        self.store.update(mutate)
        return note["cooldown"]

    def quality_for(self, route: Route, purpose: str = "") -> Dict[str, Any]:
        """The recorded entry (copy) for a route+purpose, or an empty dict."""
        q = (self.store.read().get("quality") or {}).get(route.id) or {}
        return dict(q.get(purpose or "*") or {})

    def report(self, route: Route, outcome: str,
               retry_after_seconds: Optional[float] = None,
               now: Optional[float] = None,
               scope: str = "pool",
               reset_at: Optional[float] = None) -> None:
        """Record what a call did so the next pick is better informed.

        outcome: ok | rate_limited | quota_exhausted | auth_failed |
                 transport_dead | error
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

            if outcome in ("rate_limited", "quota_exhausted"):
                if outcome == "rate_limited":
                    span = float(retry_after_seconds or DEFAULT_RATE_LIMIT_COOLDOWN)
                else:
                    span = float(retry_after_seconds
                                 or _seconds_until(route.resets_at, now)
                                 or DEFAULT_QUOTA_COOLDOWN)
                if scope == "account":
                    # ACCOUNT-WIDE: every route on this backend's allowance is
                    # spent, whatever the catalog calls their pools. The
                    # provider named its own reset - honour it instead of
                    # re-testing a dead daily quota every 60 seconds.
                    until = reset_at if reset_at and reset_at > now else now + max(
                        span, DEFAULT_QUOTA_COOLDOWN)
                    cooldowns[f"allowance:{allowance_key(route)}"] = until
                    return
                cooldowns[route.pool] = now + span
                return

            if outcome == "transport_dead":
                # BENCH THE ROUTE, NEVER THE POOL. The transport cannot serve
                # this machine, so re-drawing it in thirty seconds only spends
                # another deadline; but a broken CLI entry must not take a
                # backend that has other working routes out with it. Strikes are
                # cleared for the same reason: this is a verdict about one
                # route, not evidence that its provider is sick.
                cooldowns[f"route:{route.id}"] = now + float(
                    retry_after_seconds or TRANSPORT_DEAD_COOLDOWN)
                strikes.pop(route.id, None)
                return

            if outcome == "auth_failed":
                # Credentials are backend-scoped, not global. A rejected OpenAI
                # key says nothing about an authenticated Anthropic, ChatGPT,
                # local, or free-tier route. Cool the shared credential so every
                # model behind it is skipped, then let this call try another
                # backend instead of declaring the entire ladder unavailable.
                cooldowns[f"credential:{credential_key(route)}"] = (
                    now + float(retry_after_seconds or AUTH_FAILURE_COOLDOWN))
                strikes.pop(route.id, None)
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


# Sentinels callers may read from `.model` / `.judge_model` and hand back as a
# `model=` keyword (flexfactor's `_judge` does exactly that). They are TIER
# REQUESTS, not model ids: `structured()` translates them into a tier choice and
# strips them so the literal string can never reach a provider's wire call.
# `judge_model` stays FIXED at its sentinel (unlike `model`, which mutates to
# the last route's real id for logging/pricing) so the translation is
# unambiguous — a mutating judge sentinel could collide with a real model id.
ROTATING_MODEL = "rotating"
ROTATING_JUDGE_MODEL = "rotating-judge"


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
                 on_route: Optional[Callable[[Selection], None]] = None,
                 on_error: Optional[Callable[[Route, BaseException], None]] = None,
                 paid_first: bool = False,
                 role_coordinator: Optional[RoleCoordinator] = None):
        self.rotator = rotator
        self._factory = factory
        self._tier = tier
        self._judge_tier = judge_tier
        self._allow_paid = allow_paid
        # Best-available mode keeps the descending paid-to-free ladder active
        # for every bounded retry. A quota refusal cools its allowance, so the
        # next attempt chooses the next usable paid tier before reaching free.
        # Preserve the ordering policy even when metered spend is forbidden.
        # Subscription routes report is_free=True while still consuming paid
        # capacity; paid_first must exclude them until genuinely free/local
        # capacity is reached.
        self._paid_first = bool(paid_first)
        self.meter = meter
        self._on_route = on_route
        # Called for EVERY route failure, retryable or not, so the run's error
        # ledger sees provider errors the rotator would otherwise absorb by
        # moving to the next pool.
        self._on_error = on_error
        self._cache: Dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        # Callers read `.model` for logging and pricing. It reflects the LAST
        # route used, and is seeded with something truthful rather than a
        # hardcoded guess that would misprice the first call.
        self.model = ROTATING_MODEL
        self.judge_model = ROTATING_JUDGE_MODEL
        # Purpose sight. `set_purpose` is called once per program by the audit
        # with the program's purpose slug and any capability the purpose
        # itself demands (a UI-heavy purpose adds `vision`). Every call's
        # intent is then completed with it, so the journal can say which
        # program goal each route served. `_last_family` remembers who
        # authored last so a reviewer intent can avoid that family
        # automatically -- the author must never be the only judge of its
        # own work.
        self._purpose: str = ""
        self._purpose_needs: Tuple[str, ...] = ()
        self.role_coordinator = role_coordinator or RoleCoordinator()
        self._last_family = self.role_coordinator.last_family
        self._last_selection = self.role_coordinator.last_selection
        self._author_families = self.role_coordinator.author_families
        self._family_lock = self.role_coordinator.lock

    def set_purpose(self, purpose: str, needs: Sequence[str] = ()) -> None:
        self._purpose = str(purpose or "")[:80]
        self._purpose_needs = tuple(dict.fromkeys(str(n) for n in needs if n))

    def report_quality(self, role: str, signal: str) -> Optional[str]:
        """Attribute a work result to the route that last served `role`.

        Callers know whether a fix landed, was rejected, was a no-op, or broke
        the build; they do not know which route authored it. This provider
        does. Returns the cooldown note when one was triggered, else None.
        """
        with self._family_lock:
            sel = self._last_selection.get(role)
        if sel is None:
            return None
        return self.rotator.report_quality(sel.route, signal, sel.purpose)

    def _complete_intent(self, intent: Optional[CallIntent]) -> Optional[CallIntent]:
        if intent is None:
            if not self._purpose:
                return None
            intent = CallIntent()
        # Purpose-derived needs attach to the VISION role only. The first live
        # run (IPlay, 2026-08-23) showed why: a program that PRODUCES video
        # said "needs vision", and that need was stamped onto every code
        # author and reviewer call -- narrowing the authoring pool to
        # image-capable models for work that never looks at an image. A UI
        # reviewer that must see screenshots asks with ROLE_VISION.
        intent = intent.with_purpose(
            self._purpose, self._purpose_needs if intent.role == ROLE_VISION else ())
        if intent.role == ROLE_REVIEWER:
            with self._family_lock:
                author_families = tuple(sorted(self._author_families))
            opaque_authors = set(author_families).intersection(
                _OPAQUE_MODEL_FAMILIES
            )
            if opaque_authors:
                raise ReviewerSeparationError(
                    "independent review cannot prove separation from opaque "
                    "author model identity: "
                    + ", ".join(sorted(opaque_authors))
                )
            # Every reviewer authorization—not only grade_independent—must use
            # a concrete family outside every author family. FINAL_REVIEW_SCHEMA
            # is sent through structured(), so enforcing the role here covers
            # each exact-commit review chunk as well as adversarial reviews.
            intent = CallIntent(
                intent.role,
                intent.needs,
                intent.avoid_family,
                intent.purpose,
                tuple(dict.fromkeys(
                    intent.avoid_families
                    + author_families
                    + tuple(sorted(_OPAQUE_MODEL_FAMILIES))
                )),
            )
        return intent

    # -- plumbing ----------------------------------------------------------
    def _provider_for(self, route: Route) -> Any:
        # Locked: parallel reviews call through one RotatingProvider, and an
        # unlocked check-then-build would construct the same route's provider
        # twice (harmless for stateless clients, wasteful for heavy ones).
        with self._cache_lock:
            provider = self._cache.get(route.id)
            if provider is None:
                provider = self._factory(route)
                # Sharing the meter must never COST US THE ROUTE. CliProvider and
                # CursorProvider exposed a read-only `meter` property (a cost
                # LABEL, not a CostMeter), so this line raised
                #   AttributeError: property 'meter' ... has no setter
                # and every frontier subscription route died the moment it was
                # selected - silently, inside the caller's error handling. The
                # name collision is fixed at the source (`cost_label`); this is
                # the belt: an un-attachable meter is a LOUD warning, not a
                # disqualification. Worst case that route bills at the
                # fail-closed default instead of not running at all.
                if hasattr(provider, "meter"):
                    try:
                        provider.meter = self.meter
                    except AttributeError as exc:
                        print(f"  [rotation] WARNING {route.id}: could not attach "
                              f"the shared cost meter ({exc}); the route still "
                              "runs, its spend is not metered here", file=sys.stderr)
                self._cache[route.id] = provider
            return provider

    def _run(self, method: str, tier: str, *args, **kwargs) -> Any:
        """One rotated attempt per healthy pool, then give up honestly.

        Bounded by the number of distinct pools rather than a fixed retry count:
        retrying the same ledger cannot help, and every other ledger deserves a
        turn before the call is declared impossible.
        """
        intent = self._complete_intent(kwargs.pop("intent", None))
        # Budget attempts across EVERY tier this call may actually be served
        # from: next_route demotes DOWN TIER_CHAIN when the requested tier has
        # no candidate, so counting only the requested tier's pools starved a
        # demoted call (frontier has few pools, light has many) of the serving
        # tier's rotation.
        tiers = TIER_CHAIN[TIER_CHAIN.index(tier if tier in TIER_CHAIN else LIGHT):]
        attempts = max(1, len({r.pool for t in tiers for r in self.catalog_routes(t)}))
        last_error: Optional[BaseException] = None
        allow_paid_for_call = self._allow_paid
        for attempt in range(attempts):
            # Only name the optional kwargs when they apply: test doubles and
            # older Rotator shapes take the original signature.
            extra: Dict[str, Any] = {}
            if intent is not None:
                extra["intent"] = intent
            free_only_fallback = self._allow_paid and not allow_paid_for_call
            if self._paid_first or free_only_fallback:
                extra["paid_first"] = True
            try:
                # The ladder remains active on every attempt. Failed and
                # exhausted routes are excluded by the rotator's cooldowns;
                # silently jumping straight to free would violate the policy.
                try:
                    selection = self.rotator.next_route(
                        tier=tier,
                        allow_paid=allow_paid_for_call,
                        **extra)
                except TypeError as exc:
                    # Compatibility with injected/test rotators that implement
                    # the original two-argument protocol. Never mask a TypeError
                    # raised by a current implementation: retry only for the
                    # precise signature-drift message and only when optional
                    # policy metadata was supplied.
                    if (not extra or "unexpected keyword argument" not in str(exc)
                            or self._paid_first
                            or (intent is not None and intent.avoid_families)):
                        raise
                    selection = self.rotator.next_route(
                        tier=tier, allow_paid=allow_paid_for_call)
            except RotationError as exc:
                if last_error is None:
                    raise
                # Rotation ran dry AFTER a real provider failure: a bare
                # "no route available" here would DISCARD last_error and hand
                # the caller a confidently wrong diagnosis. Same class so a
                # PinUnavailable stays fatal for its catchers.
                raise type(exc)(
                    f"{exc}; last provider error was "
                    f"{type(last_error).__name__}: {last_error}",
                    getattr(exc, "reasons", None)) from last_error
            route = selection.route
            self.model = route.model
            if self._on_route:
                self._on_route(selection)
            try:
                result = getattr(self._provider_for(route), method)(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - classified, then re-raised
                # The shared USD meter can refuse a paid route after the call was
                # selected. That is a policy boundary, not a provider failure.
                # Continue the SAME call on genuinely free/local capacity when
                # present; subscription allowance is deliberately not called
                # "free" here merely because its marginal token price is zero.
                if (type(exc).__name__ == "BudgetExceededError"
                        and route.uses_paid_capacity
                        and allow_paid_for_call
                        and self.has_genuine_free_capacity(tier, intent=intent)):
                    last_error = exc
                    allow_paid_for_call = False
                    continue
                # A PAYLOAD refusal is not this route's doing. Reporting it here
                # would strike an innocent route and, three payloads later, cool
                # its whole pool -- see _PAYLOAD_FAULT_MARKERS for the measured
                # case. The ledger hook still fires: not charging the route must
                # never make the failure invisible.
                payload_fault = is_payload_fault(exc)
                if not payload_fault:
                    scope, reset_at = limit_scope(exc)
                    self.rotator.report(route, _classify(exc), _retry_after(exc),
                                        scope=scope, reset_at=reset_at)
                if self._on_error is not None:
                    try:
                        self._on_error(route, exc)
                    except Exception:  # noqa: BLE001 - a ledger must never break a call
                        pass
                last_error = exc
                if payload_fault or not _is_retryable(exc):
                    raise
                continue
            self.rotator.report(route, "ok")
            if intent is not None and intent.role:
                family = route_model_family(route)
                with self._family_lock:
                    self._last_family[intent.role] = family
                    self._last_selection[intent.role] = selection
                    if intent.role == ROLE_AUTHOR:
                        self._author_families.add(family)
            return result
        raise RotationError(
            f"every {tier} pool failed this call; last error was "
            f"{type(last_error).__name__}: {last_error}") from last_error

    def catalog_routes(self, tier: str) -> List[Route]:
        return [r for r in self.rotator.catalog.routes
                if r.tier == tier and r.enabled
                and (self._allow_paid or r.is_free)]

    def has_genuine_free_capacity(self, tier: Optional[str] = None,
                                  intent: Optional[CallIntent] = None) -> bool:
        """Whether this provider can demote to non-paid capacity.

        This intentionally uses ``uses_paid_capacity`` rather than ``is_free``:
        subscription routes have zero marginal price but still consume an
        allowance the owner pays for.
        """
        requested = tier if tier in TIER_CHAIN else self._tier
        completed_intent = self._complete_intent(intent)
        probe = getattr(self.rotator, "has_usable_route", None)
        if callable(probe):
            return bool(probe(
                requested,
                allow_paid=False,
                intent=completed_intent,
                paid_first=True,
                paid_capacity=False,
            ))
        # Compatibility for injected legacy rotators. Production Rotator always
        # takes the path above, where every selection constraint is enforced.
        start = TIER_CHAIN.index(requested if requested in TIER_CHAIN else LIGHT)
        allowed_tiers = set(TIER_CHAIN[start:])
        return any(route.enabled and route.tier in allowed_tiers
                   and not route.uses_paid_capacity
                   for route in self.rotator.catalog.routes)

    # -- provider surface --------------------------------------------------
    def complete(self, *args, **kwargs):
        kwargs.setdefault(
            "intent", CallIntent(ROLE_AUTHOR, (CAP_CODE_AUTHOR,))
        )
        return self._run("complete", self._tier, *args, **kwargs)

    def structured(self, *args, **kwargs):
        # flexfactor's `_judge()` requests the cheap tier by passing
        # `model=provider.judge_model` — for a fixed provider that is a real
        # model id, for this one it is the ROTATING_JUDGE_MODEL sentinel. Honor
        # the intent (route the CALL to the judge tier) and strip the sentinel:
        # the literal string "rotating-judge" must never reach a wire call.
        tier = self._tier
        requested = kwargs.get("model")
        if requested in (ROTATING_MODEL, ROTATING_JUDGE_MODEL):
            kwargs.pop("model")
            if requested == ROTATING_JUDGE_MODEL:
                tier = self._judge_tier
        kwargs.setdefault(
            "intent", CallIntent(ROLE_AUTHOR, (CAP_CODE_AUTHOR, CAP_STRUCTURED_JSON))
        )
        return self._run("structured", tier, *args, **kwargs)

    def grade(self, *args, **kwargs):
        # Grading is classification, not authoring: it belongs on the cheap
        # tier, exactly as JUDGE_MODELS already does for the fixed providers.
        # It is also an independent reviewer role, so the rotator avoids the
        # family that produced the immediately preceding author candidate when
        # any alternative family is usable.
        kwargs.setdefault(
            "intent",
            CallIntent(ROLE_REVIEWER, (CAP_CODE_REVIEW, CAP_STRUCTURED_JSON)),
        )
        return self._run("grade", self._judge_tier, *args, **kwargs)

    def grade_independent(self, *args, **kwargs):
        """Grade only when a different model family certifies the result.

        Reviewer routing already carries strict exclusions for every family
        that authored the candidate.  This method makes that production
        contract explicit at call sites that use a semantic grade as the final
        authorization (notably a verified no-op), and checks the postcondition
        instead of trusting routing metadata alone.
        """
        with self._family_lock:
            author_families = frozenset(self._author_families)
        if not author_families:
            raise ReviewerSeparationError(
                "independent grading requires a recorded candidate author family"
            )
        opaque_authors = author_families.intersection(_OPAQUE_MODEL_FAMILIES)
        if opaque_authors:
            raise ReviewerSeparationError(
                "independent grading cannot prove separation from opaque author "
                "model identity: " + ", ".join(sorted(opaque_authors))
            )
        result = self.grade(*args, **kwargs)
        with self._family_lock:
            selection = self._last_selection.get(ROLE_REVIEWER)
        if selection is None:
            raise ReviewerSeparationError(
                "independent grading did not record a reviewer route"
            )
        reviewer_family = route_model_family(selection.route)
        if reviewer_family in _OPAQUE_MODEL_FAMILIES:
            raise ReviewerSeparationError(
                "independent grading cannot prove the reviewer family from "
                f"opaque model identity '{reviewer_family}'"
            )
        if reviewer_family in author_families:
            raise ReviewerSeparationError(
                f"reviewer family '{reviewer_family}' also authored the candidate"
            )
        return result

    def ping(self, *args, **kwargs):
        return self._run("ping", self._judge_tier, *args, **kwargs)


_RETRYABLE_MARKERS = (
    "rate limit", "rate_limit", "429", "overloaded", "capacity",
    "timeout", "timed out", "502", "503", "504", "529",
    "insufficient", "quota", "credit", "billing", "connection",
)

# A 400 that describes THIS ROUTE'S CAPABILITY, not a malformed request.
#
# WHY THIS EXISTS (live overnight run 2026-08-20/21, 8 hours, 5 repos, ONE
# one-line fix): `groq/compound` caps output at 4096 tokens. FlexFactor asked
# for 16000, so every semantic review call came back
#   400 `max_tokens` must be less than or equal to `4096`
# `_is_retryable` blanket-rejected status 400 as "a bad request stays bad on
# every backend", so `_run` re-raised WITHOUT giving any other pool a turn.
# Every file returned INCOMPLETE, three consecutive zero-completion batches
# tripped flexfactor's provider-outage circuit breaker, the run rolled the tree
# back and aborted -- and the owner's log said "provider outage" when the truth
# was "this one route is too small and we never tried the other 640".
#
# These messages are the OPPOSITE of universal: they are the strongest possible
# evidence that a DIFFERENT route would work. They are retryable.
_ROUTE_CAPABILITY_MARKERS = (
    # OBSERVED LIVE 2026-08-22 (FlexFactor self-dogfood): OpenRouter answers
    # 403 "<model> is only available on agentic harnesses. Try plugging it into
    # a coding agent ..." for inkling:free. That is a property of the ROUTE,
    # not of the credential - it burned all three purpose samples because 403
    # was read as "wrong key, stop".
    "only available on agentic harnesses", "not available on this endpoint",
    "max_tokens", "max_new_tokens", "maxtokens",
    "context length", "context_length", "context window", "context_window",
    "too many tokens", "reduce the length", "input is too long",
    "string too long", "maximum context",
    "unsupported model", "model_not_found", "model not found",
    "does not exist or you do not have access",
    "does not support", "unsupported parameter", "unsupported value",
    "response_format", "json_object", "json mode",
    # OBSERVED LIVE 2026-08-21 on the espectre proof run, both 404s, both
    # per-route and both previously fatal to the call:
    #   openai_api/babbage-002 -> "This is not a chat model and thus not
    #       supported in the v1/chat/completions endpoint" (a completions-only
    #       base model that the NAME blocklist has no way to recognise)
    #   cloudflare              -> "Function '<uuid>': Not found for account
    #       '<acct>'" (the route exists in the catalog, not on the account)
    "not a chat model", "v1/completions", "not supported in the v1",
    "not found for account", "no such model", "no endpoints found",
)


# An exception type whose failure can ONLY be a property of the route that
# raised it. A CLI provider *is* the route: the binary, its login, its account
# entitlements and its deadline all belong to that one entry in the catalog, so
# `codex.CMD` exiting 1 or `claude.EXE` being killed says nothing whatever about
# the payload or about any of the other 640 routes.
_TRANSPORT_FAULT_TYPES = ("CliUnavailable", "CrossFamilyRescueRequired")

# A refusal of the REQUEST BODY. No route can accept it, so rotating is pure
# waste -- and, worse, `Rotator.report` would charge the refusal to whichever
# innocent route happened to be selected. MEASURED 2026-08-24: one
# `payload contains ['private_key']` struck three healthy routes in two seconds
# (repo-rewards run 21424: openrouter/anthropic/claude-opus-4.8-fast,
# nvidia_nim/nvidia/cosmos-reason2-8b, gemini/gemini-3.1-pro-preview), and
# three strikes cool a whole POOL for five minutes. A secret in the audited repo
# must never bench a provider.
_PAYLOAD_FAULT_MARKERS = ("flexfactor_egress_blocked",)


def is_transport_dead_error(exc: BaseException) -> bool:
    """True when the ROUTE'S OWN transport failed, implicating no other route."""
    return type(exc).__name__ in _TRANSPORT_FAULT_TYPES


def is_payload_fault(exc: BaseException) -> bool:
    """True when the request BODY was refused before any backend saw it.

    Never retryable (the next route gets the same bytes and the same verdict)
    and never chargeable to a route (the route did nothing wrong).
    """
    if type(exc).__name__ == "EgressBlockedError":
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(m in blob for m in _PAYLOAD_FAULT_MARKERS)


def is_route_capability_error(exc: BaseException) -> bool:
    """True when a 4xx names a limit/capability of THIS route specifically.

    Such a call is worth retrying on a different route; a genuinely malformed
    request is not. Authentication is classified separately because it is
    backend-credential scoped rather than a route capability.
    """
    blob = f"{type(exc).__name__} {exc}".lower()
    if type(exc).__name__ == "RouteCapabilityError":
        return True
    if is_transport_dead_error(exc):
        return True
    return any(m in blob for m in _ROUTE_CAPABILITY_MARKERS)


def _classify(exc: BaseException) -> str:
    """Map a provider exception onto a rotation outcome."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    blob = f"{type(exc).__name__} {exc}".lower()
    if status == 429 or "rate limit" in blob or "rate_limit" in blob:
        return "rate_limited"
    if status in (401, 403) and not is_route_capability_error(exc):
        return "auth_failed"
    if any(m in blob for m in ("quota", "insufficient", "credit", "billing")):
        return "quota_exhausted"
    # Checked AFTER the two allowance outcomes on purpose: a CLI that reports a
    # usage limit still has a real reset time worth honouring, and those
    # branches carry it. Everything else from a CLI is its transport being dead
    # here, which a 30-second route cooldown does not describe.
    if is_transport_dead_error(exc):
        return "transport_dead"
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
    if is_payload_fault(exc):
        # The next route gets the identical bytes and refuses them identically.
        return False
    if isinstance(exc, (TypeError, AttributeError, NameError, ImportError,
                        SyntaxError, IndentationError, AssertionError)):
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and status in (400, 404, 422):
        # A ROUTE-CAPABILITY 400/404 (output ceiling, context window, an
        # unsupported parameter, a model this backend does not serve) is bad on
        # THIS route and fine on the next one -- see _ROUTE_CAPABILITY_MARKERS
        # for the 8-hour zero-work run that proved it. Anything else really is a
        # malformed request and stays malformed everywhere.
        return is_route_capability_error(exc)
    if isinstance(status, int) and status in (401, 403):
        # Authentication is scoped to the selected backend/credential, not the
        # whole ladder. ``report(auth_failed)`` benches every model sharing that
        # credential, so retrying proceeds to a genuinely different route rather
        # than touring hundreds of models with the same rejected key.
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    if is_route_capability_error(exc):
        return True
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
