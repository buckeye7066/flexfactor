# FlexFactor - Execution containment (0.5.0 working tree)

Source: `flexfactor_sandbox.py` (`capability_report`, `_claim_sentence`,
`prepare`, `run_contained`, `require_containment_or_trust`),
`flexfactor_trust.py`, `flexfactor.py:_run_target_code/_spawn`.

## 1. Per-platform capability table (semantics of `capability_report()`)

Values: `os-enforced` | `best-effort` | `best-effort-env` | `none` | `off`.

| Platform / mechanism | network_isolation | process_tree | memory | process_count | cpu_time | credential_scrub |
|---|---|---|---|---|---|---|
| Windows, Job Object probe OK (`win32-job-object`) - THIS HOST | best-effort-env (proxy poison) | os-enforced | os-enforced | os-enforced | os-enforced | applied |
| Windows, Job Object probe fails | best-effort-env | none | none | none | none | applied |
| Windows AppContainer | listed `available: false` ("not implemented") | - | - | - | - | - |
| Linux, `bwrap` probe OK | os-enforced (net ns) | os-enforced (dies with bwrap) | os-enforced if rlimit | os-enforced if rlimit | os-enforced if rlimit | applied |
| Linux, `unshare -rn` OK, no bwrap | os-enforced | best-effort (process group, escapable via setsid) | rlimit | rlimit | rlimit | applied |
| Linux, neither | best-effort-env | best-effort | rlimit | rlimit | rlimit | applied |
| Other OS | best-effort-env | none | none | none | none | applied |

Linux rows are code-read only; no Linux host ran this session (BLOCKED).

## 2. Claim sentence

`_claim_sentence` emits "Third-party build/test code is contained by <mechanism>
..." ONLY when `network_isolation == os-enforced AND process_tree == os-enforced`.
Otherwise: "NOT an OS sandbox: process tree <x> via <mech>, memory <x>, process
count <x>, network isolation <x>; raw-socket egress is NOT prevented and
third-party code can still reach the network. Credentials are scrubbed from the
environment." The sentence lands in the run manifest as `containment.claim` and
in each broker basis as `claim` (trusted-repo basis appends "Execution
authorized by owner trust: <reason>").

`flexfactor_trust.containment_claim()` is NOT a second answer: it delegates to
`flexfactor_sandbox.capability_report()["claim"]`, and only falls back to its own
truthful "could not probe" sentence if the import fails. There is exactly one
containment claim in this product, and it is the measured one.

### `claim_headline` - and why slicing `claim` is forbidden (2026-08-25)

The long claim names the OS-enforced mechanisms FIRST and what is not contained
LAST. Two surfaces were cutting it to fit - the dashboard evidence record at
`[:160]` and the v2 dashboard row at `[:110]` - and on this host (claim length
279) both cuts landed inside "raw-socket e|gress is NOT prevented". A reader saw
every guarantee and none of the holes, which is precisely the i-5 failure the
claim exists to prevent.

`capability_report()` therefore also returns `claim_headline`: short by
construction (78 chars measured here), built NEGATIVE-FIRST -
`NOT contained: network NOT OS-enforced (strongest mechanism: win32-job-object)`
- so a caller that needs one row renders it instead of truncating the claim.
`flexfactor_invariant_sweep_tests.ContainmentClaimSingleSourceTests` fails the
build on any new slice of `claim`, and on any divergence between the trust
module's answer and the sandbox probe's.

Measured on this host, 2026-08-25 (`win32`): `win32-job-object` AVAILABLE -
process-tree kill, memory, process count and CPU time are OS-enforced (the probe
ran a job-assigned child to exit 0); `win32-appcontainer` NOT implemented;
network isolation is `best-effort-env` only. So: process/memory/CPU containment
is real and OS-enforced here; NETWORK containment is not, and is named
best-effort everywhere it appears.

## 3. Authorization rule (`require_containment_or_trust`)

```
if os_sandbox_sufficient(report):   # process_tree, memory, network all os-enforced
    allowed, basis = os-sandbox
elif trust_decision.allowed:         # flexfactor_trust
    allowed, basis = trusted-repo
else:
    raise ContainmentUnavailable(... names missing enforcement + how to authorize)
```
On Windows `os_sandbox_sufficient` is always False (network), so EVERY
install/build/test on this host requires trust. A refusal returns rc 126,
`flexfactor_launch_error`, `flexfactor_containment_blocked=True`, message
`[flexfactor-containment] REFUSED: ...`, and an `execution_ledger` row with
`refused: true`.

## 4. How to authorize a repository

| Method | Scope | Verified |
|---|---|---|
| `FLEXFACTOR_TRUSTED_REPOS=C:\path\a;C:\path\b` (`;` on Windows; `;` or `:` on POSIX) | all runs in that env; wins over policy.json | `load_trusted_repo_rules` |
| `~/.flexfactor/policy.json` `{"trusted_repos": ["C:\\Users\\me\\repo", "~/src"]}` (also accepts `trusted_repositories`) | persistent; path-prefix match after realpath/normcase | `load_trusted_repo_rules` |
| `flexfactor audit|prodready|scout ... --trust-repo` | this run, this repo; `trust_repo_override: true` in manifest | accepted by all three parsers |
| `--allow-untrusted-exec` | former name in `trust_decision` text | **removed**; the text now names `--trust-repo` |

## 5. Limits defaults (`flexfactor_sandbox.Limits`)

| Field | Default | Broker override |
|---|---|---|
| timeout_s | 900 | `_run` timeout argument (1800 for verify/e2e/coverage, 2400 for suite) |
| memory_bytes | 2 GiB | - |
| max_processes | 256 | - |
| cpu_seconds | None (off) | - |
| network | False | `install` class -> True; `_spawn` dev server -> True |
| writable_dirs | [] | `source_root=cwd` passed (POSIX bind) |

Output is capped at 8 MiB per stream (`OUTPUT_CAP_BYTES`); the rest is drained
and counted.

## 6. Network rule

- install => network ON (registry needed), still credential-scrubbed.
- build / test / coverage / explorer => network OFF: `poison_network_env`
  (proxies -> `http://127.0.0.1:9`, `NO_PROXY` emptied, npm offline +
  dead registry, `PIP_NO_INDEX=1`); on Linux with bwrap/unshare the net
  namespace enforces it.
- dev server (`_spawn`) => network ON (serves on loopback).
- Scout verify additionally passes the legacy `_no_network_env()` (duplicate).

## 7. AppContainer follow-up (Windows, not built)

Per `ISOLATION_SPIKE.md` option D and the sandbox docstring: `CreateProcess`
with `STARTUPINFOEX` + `SECURITY_CAPABILITIES` lacking `internetClient` /
`privateNetworkClientServer`; needs `CreateAppContainerProfile`, an ACL grant
for the container SID on the project dir and toolchain dirs, and cleanup of
both. When built, `_build_report` should add `win32-appcontainer available:
true` with `network` in `enforces`, flip `network_isolation` to `os-enforced`,
and `os_sandbox_sufficient` will then authorize untrusted repos on Windows
without a trust entry. Until then: trust entries are the only Windows path.

## 8. Tests (run by me)

`test_flexfactor_sandbox.py`: 20 tests, OK, 2 skipped on Windows (Linux
mechanisms) - the skips are BLOCKED evidence, not passes. CI ubuntu job prints
`capability_report()` and is labelled "never a pass by itself".
