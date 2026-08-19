# Verification Fail-Closed Proof

This document describes how FlexFactor's provider rotation layer handles
verification failures and guarantees that a failing provider never silently
receives coverage assignments.

---

## What "verification" and "coverage assignment" mean here

**Verification** is any call — `complete()`, `structured()`, `ping()` — that
contacts a backing provider route to confirm it can serve work.

**Coverage assignment** is a successful return from one of those calls, meaning
work was routed to and handled by a specific provider.

**Fail-closed** means: every verification failure raises an exception.  It
never silently returns a result as if the call succeeded through a broken
route, and it never returns `None` as a stand-in for "no provider available".

---

## The invariant

The implementation lives in `flexfactor_rotation.py`:

```
RotatingProvider._run()
    for each pool in catalog:
        try:
            result = provider.complete(...)
        except Exception as exc:
            rotator.report(route, classify(exc))   # records the failure
            if not _is_retryable(exc):
                raise                              # non-transient: fail immediately
            continue                              # transient: try the next pool
    raise RotationError(...)                       # all pools exhausted: fail loudly
```

No code path returns `None`, returns a fabricated result, or swallows an
exception silently.

---

## Failure modes and their fail-closed behavior

### 1. Network timeout

A `503` / `504` response or a timeout string in the exception is **retryable**.
The rotator records a per-route cooldown and tries the next pool.

- **Single pool times out**: the call rotates to the next healthy pool and the
  work is delivered there.  The timed-out route is marked cooling so it is not
  reused on the immediately following call.
- **All pools time out**: a `RotationError` is raised naming the last error.
  `complete()` never returns `None`.

### 2. Invalid provider response

A `ValueError` (e.g. "unexpected token in response") is **not retryable**.
`_is_retryable()` returns `False` for `ValueError` because a malformed
response is equally malformed on every backend — rotating would waste every
pool reproducing the same bug.

The original `ValueError` propagates immediately.  The call count shows exactly
one attempt was made.

### 3. Verification service unreachable

A connection-refused `OSError` is **retryable** — another pool may be reachable
even when one is not.  After the failed pool's route enters cooldown the next
`next_route()` call skips it.

- **One pool unreachable**: rotates to a healthy backup; the work arrives at a
  real, live provider.
- **All pools unreachable**: a `RotationError` is raised.  `complete()` never
  returns `None`.

After a connection error the failing route stays in `cooldowns["route:<id>"]`
for `ROUTE_ERROR_COOLDOWN` (30 s), preventing it from being silently recycled.

### 4. Credential mismatch (401 / 403)

A `401 Unauthorized` or `403 Forbidden` is **not retryable**.  The HTTP status
code is checked explicitly in `_is_retryable()`:

```python
if isinstance(status, int) and status in (400, 401, 403, 404, 422):
    return False   # a bad request stays bad on every backend
```

The original `Boom` exception propagates on the **first** attempt.  The call
count confirms that exactly one pool was tried — a bad API key does not trigger
rotation through the full pool list.

---

## Test coverage

All four failure modes are pinned in `VerificationFailClosedTests` inside
`flexfactor_rotation_tests.py`.  The tests are written so that relaxing any
of the fail-closed invariants causes them to fail:

| Test | Failure mode | Invariant pinned |
|------|-------------|-----------------|
| `test_network_timeout_on_single_provider_routes_to_healthy_backup` | Timeout | Rotation delivers to live pool; timed-out route enters cooldown |
| `test_network_timeout_on_all_providers_raises_rotation_error` | Timeout | `RotationError` raised, not `None` returned |
| `test_network_timeout_never_returns_none` | Timeout | `complete()` must raise, not return `None` |
| `test_invalid_response_raises_immediately_not_silently_swallowed` | Parse error | `ValueError` propagates unchanged |
| `test_invalid_response_is_not_rotated_to_a_second_pool` | Parse error | Exactly one pool attempted |
| `test_structured_call_fails_closed_on_parse_error` | Parse error | `structured()` is equally fail-closed |
| `test_service_unreachable_on_all_pools_raises` | Unreachable | Exception raised, not `None` |
| `test_service_unreachable_single_pool_rotates_to_backup` | Unreachable | Work delivered to live pool |
| `test_ping_failure_on_all_routes_raises_not_returns_false` | Unreachable | `ping()` raises, not returns `False` |
| `test_unreachable_route_is_cooled_off_not_silently_retried` | Unreachable | Failed route stays in cooldown |
| `test_credential_mismatch_blocks_assignment` | Credential (401) | Original exception propagates |
| `test_credential_mismatch_is_not_retried_across_pools` | Credential (401) | Exactly one pool attempted |
| `test_forbidden_response_also_blocks_without_rotation` | Credential (403) | Exactly one pool attempted |
| `test_failed_call_never_silently_returns_a_result` | Total failure | `RotationError` raised, not a result returned |
| `test_rotation_error_message_names_the_failure` | Total failure | Error message carries the root cause |

Run with:

```bash
python flexfactor_rotation_tests.py -v
```

All tests run offline with no credentials and no network.
