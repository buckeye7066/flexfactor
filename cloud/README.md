# FlexFactor Cloud

FlexFactor Cloud is the managed control plane for the Android product. The APK talks to this
service for sign-in, token rotation, repository discovery, workflow installation, run dispatch,
status, bounded result artifacts, and steering. GitHub Actions remains the ephemeral multi-toolchain
compute substrate; it is no longer the product API exposed to the phone.

## Production configuration

Deploy this directory as the Vercel project root with Node.js 22. The registered FlexFactor Mobile
OAuth client ID is the checked-in production default. `GITHUB_OAUTH_CLIENT_ID` is an optional public
override. GitHub's device authorization and device-token refresh grants require the client ID but do
not require a client secret.

`GET /api/health` returns HTTP 200 only when the device OAuth identity is configured.

The service is intentionally stateless. GitHub OAuth access and refresh tokens remain encrypted in
Android Keystore and are supplied only for the request that needs them. Provider keys stay on the
phone until dispatch, when the APK seals each one to the target repository's current Actions public
key; FlexFactor Cloud receives only the sealed value and key ID.

## API

| Route | Purpose | Authentication |
|---|---|---|
| `GET /api/health` | Exact service/engine readiness | None |
| `POST /api/oauth/device` | Start GitHub device sign-in with rotating-token scope | None |
| `POST /api/oauth/token` | Poll a device authorization | Device code |
| `POST /api/oauth/refresh` | Rotate an expiring GitHub session | Refresh token |
| `POST /api/configure` | Validate the signed-in account and required scopes | Bearer |
| `GET /api/repositories?page=N` | Page through administrable public/private repositories | Bearer |
| `GET /api/provider-key` | Fetch a repository's Actions sealing key | Bearer |
| `POST /api/runs/dispatch` | Validate, install the pinned caller, seal secrets, and start a mode | Bearer |
| `GET /api/runs/status` | Read the correlated run and active step | Bearer |
| `GET /api/runs/details` | Proxy one bounded, GitHub-signed phone artifact | Bearer |
| `POST /api/runs/steer` | Append a bounded owner steering instruction | Bearer |

All API responses are `no-store`, HTTPS-only in production, bounded, and fail closed. The service
calls only fixed GitHub HTTPS origins; user input cannot select an upstream host. A result-artifact
redirect is accepted only from GitHub's signed storage host families, and
the bearer token is never forwarded to that signed URL.

Status and details both require `repository`, `run_id`, and the canonical
`request_id` UUID. Details first verifies the run's numeric ID and FlexFactor
request title, then selects only `mobile-phone-<request_id>` from that run.
Missing or mismatched identity fails before artifact listing or download.

## Interrupted requests and cleanup

Dispatch retries read the durable request claim first. A recorded run ID is
recovered directly, even if the target ref was deleted or repository history is
large. Claims without a run ID are searched from their creation time. A missing
claim requires repository-wide unfiltered history, bounded to 100 pages; this
avoids GitHub's 1,000-result cap on filtered searches. Incomplete or saturated
history never authorizes another dispatch.

An unresolved claim returns `dispatch_pending` for up to 15 minutes, then
`dispatch_recovery_required`. An invalid creation time, missing recorded run, or
saturated recovery search also requires recovery. Inspect the request UUID in
the repository's GitHub Actions history and retry the same request to recover a
visible run. If it remains unresolved, the service operator must investigate
acceptance and execution before any replacement is authorized. Do not delete a
claim or regenerate its UUID solely because it is old: GitHub repository
variables offer no documented compare-and-swap operation, and the original
invocation could still be dispatching. Transport failures and dispatch HTTP 5xx
retain the claim and credentials.

Phone-supplied provider keys use secret names containing the full request UUID.
The generated caller passes those secrets under the two names expected by the
unchanged pinned engine. An explicitly supplied phone key takes precedence for
that request; canonical owner-managed repository secrets are never overwritten
and remain the fallback when no phone key is supplied. A delayed cleanup from
an earlier run therefore cannot delete a later request's uploaded credential.
Legacy claims naming canonical secrets remain readable for cleanup.

Cleanup requires a successfully fetched, matching, completed run. A 404 can
mean lost access or visibility and never proves completion. Cleanup removes
steering, then phone-supplied secrets, then the claim, accepting already-missing
resources on retry. Any failure is returned to the phone so it retains the
queue entry. Terminal status also removes legacy steering whose claim was
already deleted. Steering checks the active claimed run before and after its
write; a concurrent completion removes the late instruction before rejecting it.

OAuth endpoints share an in-memory budget of 120 admitted requests per minute
and at most eight concurrent upstream exchanges per process. Excess requests
receive HTTP 429 with `Retry-After`. This is a per-instance resource bound, not
distributed rate limiting: separate serverless instances have separate budgets.
No credentials or client IP address records are retained by this limiter.

## Verification and release

```bash
npm ci
npm test
```

The Android and production-readiness workflows both run this suite. An Android release cannot
publish unless the build job (cloud contract, Java unit tests, lint, APK/AAB builds, and hosted-model
proof) succeeds. The cloud engine pin, Android version, reusable-workflow checkout, and release tag
are checked for exact agreement.

Before promoting a deployment:

1. Confirm `/api/health` is HTTP 200 and names the expected `android-v*` engine.
2. Complete one fresh device sign-in and one forced token refresh.
3. Run Refactor, Scout, Audit, and Production Ready against the release test repository.
4. Confirm each correlated run completes, its in-app artifact opens, and steering is consumed by an
   active Audit or Production Ready run.
5. Scan deployment runtime errors, then promote the already-tested deployment without rebuilding.

Rollback by restoring the previous production deployment alias. The Android release stays pinned to
its exact engine tag, so rolling back the control plane cannot silently change the engine source.
