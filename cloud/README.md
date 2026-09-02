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
