# ULTRAPLAN 3.1 — No-network verification on Windows (decision doc)

Timeboxed research spike, 2026-07-25. Goal: pick the mechanism for running
scout's build-verify gate (and later the audit build gate) WITHOUT network
access, so an adopted candidate's build step can't phone home.

## Threat model

The verify step runs the PROJECT'S OWN build/test commands after generated
files are applied and dependencies installed with `--ignore-scripts`. Residual
risk: imported candidate code executes AT BUILD/TEST TIME (not install time)
and can open sockets. We want the verify subprocess tree to have no network,
without breaking builds that legitimately need none.

## Options evaluated

| # | Mechanism | Airtightness | Effort | Blast radius / notes |
|---|---|---|---|---|
| A | Proxy-poisoning env (`HTTP(S)_PROXY`/`ALL_PROXY` -> unroutable `http://127.0.0.1:9` + `NO_PROXY=""`) + `npm_config_offline=true` + `npm_config_registry=http://127.0.0.1:9` | ~80% — honored by npm/yarn/node-fetch/undici/pip/curl; RAW sockets bypass it | LOW (env dict on the verify `_run` calls — the `env` param already exists) | Zero admin rights, zero global state, per-process only. FAILS OPEN for raw-socket code. |
| B | Windows Firewall rule per-exe (`New-NetFirewallRule -Program <node.exe> -Action Block -Direction Outbound`) | High for that exe | MED | Requires ADMIN; blocks node.exe GLOBALLY (dev servers, other tools) — unacceptable side effects; rule cleanup on crash is fragile. REJECTED. |
| C | Dedicated restricted local user + per-user firewall block (`-LocalUser` SDDL), verify via `runas` | High | HIGH | Admin + credential management + ACLs on the repo dir; heavy and brittle. REJECTED for a desktop tool. |
| D | AppContainer launch (CreateProcess + SECURITY_CAPABILITIES, no `internetClient` capability) | Airtight (OS-enforced) | HIGH (ctypes; filesystem ACLs for the container SID must be granted on the project dir) | The "right" long-term answer; no admin needed. Needs a careful ctypes module + ACL grant/cleanup. FUTURE. |
| E | Docker `--network none` when Docker Desktop is present | Airtight inside the container | MED | Only when Docker is installed AND the project builds in linux; changes build environment (paths, node version). OPT-IN niche. |
| F | WFP filter via native driver/API | Airtight | VERY HIGH | Driver-level work; out of scope for a single-file tool. REJECTED. |

## Decision

**Ship A now** (`--isolate-verify` defaulting ON for scout verify, with loud
disclosure in the approval card that isolation is best-effort env-level), and
**document D (AppContainer) as the airtight follow-up**. B/C/F rejected for
blast radius or effort; E can ride behind a `--isolate-verify docker` value
later if demand appears.

Rationale: A is one env dict on existing `_run(env=...)` plumbing, needs no
admin rights, degrades nothing (a build that needs the network to VERIFY was
already suspect — `--no-isolate-verify` is the opt-out), and closes the
common exfil paths (HTTP(S) via standard clients). The honest-disclosure
convention (`_verify_disclosure`) must state the isolation level per run, the
same way script-blocking is disclosed today.

## Implementation sketch for 3.2 (one slice)

1. `_no_network_env() -> dict`: os.environ + `HTTP_PROXY`/`HTTPS_PROXY`/
   `ALL_PROXY`/`http_proxy`/`https_proxy`/`all_proxy` = `http://127.0.0.1:9`,
   `NO_PROXY`/`no_proxy` = `""`, `npm_config_offline=true`,
   `npm_config_registry=http://127.0.0.1:9`, `npm_config_fund=false`,
   `npm_config_audit=false`.
2. Verify/build-gate `_run` calls pass `env=_no_network_env()` when
   `args.isolate_verify` (default True for scout apply; audit later).
3. `_verify_disclosure` adds the isolation state line.
4. Tests: env dict contents pinned; a verify command that curls localhost:9
   fails; disclosure text; opt-out flag restores inherited env.

## Non-goals (this slice)

- Audit-mode build gate isolation (same helper, separate slice — the audit
  loop's verify call sites are hotter and deserve their own review round).
- AppContainer (D): tracked as the airtight successor; prerequisite is a
  ctypes `CreateProcess` wrapper + per-SID ACL grant on the project dir.
