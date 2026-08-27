# Live Cursor/AI-Time production proof

The ordinary `rotation-extensions` workflow is deliberately offline. It proves
catalog discovery and provider wiring without sending credentials or inference
traffic. It is not evidence of a live Cursor call.

The manual `hardened-live-cursor-aitime-proof` workflow is the live gate. It
runs only on a protected Linux runner labelled:

```text
self-hosted, linux, x64, flexfactor-hardened
```

The runner must make `flexfactor_sandbox.os_sandbox_sufficient()` return true.
That requires OS-enforced network isolation, process-tree containment, and
memory limits. The supported repository-controlled path is Linux `bwrap` with
user/network/PID namespaces enabled. Hosted runners that fall back to `rlimit`
are rejected.

Provision a dedicated, ephemeral Ubuntu runner under a non-root service account;
do not attach the label to a runner that accepts workflows from forks. Install
`bubblewrap`, register the runner with all four labels above, and verify the
kernel permits the exact namespace operation FlexFactor requires:

```text
sudo apt-get update
sudo apt-get install --yes bubblewrap
bwrap --unshare-all --ro-bind / / --dev /dev --proc /proc --die-with-parent -- /bin/true
```

The final command must exit zero as the runner service account. If the host's
user-namespace or AppArmor policy rejects it, use a dedicated VM image whose
policy permits this bounded command; do not weaken a shared host globally. The
workflow records the resulting capability report and exercises process-tree,
memory, process-count, timeout, and raw-socket boundaries before any live call.

## GitHub environment

Create an Actions environment named `production-live`. Limit deployment access
to the protected `main` branch and add:

| Kind | Name | Purpose |
|---|---|---|
| Secret | `FLEXFACTOR_CURSOR_BASE_URL` | Authenticated OpenAI-compatible Cursor endpoint, ending in `/v1` |
| Secret | `FLEXFACTOR_CURSOR_API_KEY` | Bearer token enforced by that endpoint |
| Variable | `AI_ROTATE_CATALOG` | Absolute path to the runner's current AI-Time `routes.json` |
| Variable | `FLEXFACTOR_CURSOR_ROUTE_ID` | Exact enabled Cursor route ID to prove |

Do not store Cursor state databases, API keys, or the route catalog in the
repository or workflow artifacts.

Remote endpoints must use HTTPS. Plain HTTP is accepted only for loopback
addresses, for a Cursor daemon reached on the runner itself. Scope the bearer
token to inference only and rotate it after any suspected runner compromise.

## What the gate proves

1. The AI-Time catalog is schema 1, no more than 24 hours old, and contains
   exactly the requested enabled Cursor route.
2. Its endpoint matches `FLEXFACTOR_CURSOR_BASE_URL`.
3. The endpoint rejects a completion request without a bearer token with
   HTTP 401 or 403.
4. The same endpoint accepts the configured bearer token.
5. The selected live model returns a new random sentinel exactly.
6. The uploaded JSON contains route/model identity, catalog and response
   hashes, statuses, and duration, but no endpoint or credential.

The artifact also contains `containment-report.json`. Live inference necessarily
uses the network, so the live request is not run in the no-network bubblewrap
namespace; instead, credentials are injected only into that single proof step.
The containment tests separately prove that FlexFactor can launch untrusted
build/test processes with network, process-tree, and memory enforcement.

Run the workflow manually from GitHub Actions after the environment secrets,
variables, and hardened runner are present. A queued run is not evidence; every
step and the redacted artifact must complete successfully.
