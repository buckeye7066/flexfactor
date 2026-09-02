# FlexFactor 0.5.0 migration notes

No checkpoint or target-repository migration. Behaviour changes for existing
users, launchers and scheduled tasks:

## 1. Trust gate: install/build/test REFUSED in untrusted repos on hosts without an OS sandbox

On Windows (no OS network isolation) every dependency install, build, test,
coverage run and dev-server start of a target repo now requires the repo to be
trusted. Otherwise `_run` returns rc 126 `[flexfactor-containment] REFUSED`,
the baseline gate cannot run, and the run ends BLOCKED (not clean, not fixed).
Authorize ONE of:

1. `setx FLEXFACTOR_TRUSTED_REPOS "C:\Users\firer\GrantFlow;C:\Users\firer\sermonsmith"`
   (semicolon-separated on Windows; re-open the shell / schtask picks up user env).
2. `~/.flexfactor/policy.json`:
   `{"trusted_repos": ["C:\\Users\\firer\\GrantFlow", "C:\\Users\\firer"]}` -
   a parent directory trusts everything under it (path-prefix match).
3. `flexfactor audit|prodready|scout ... --trust-repo` for one run. All three
   accept it; options 1 or 2 make the trust persistent for scheduled runs.

Launchers/schtasks that audit repos under `C:\Users\firer` need 1 or 2 before
the next scheduled run, or every overnight program will stop at bootstrap.
Lifecycle scripts stay off unless `--allow-scripts`.

## 2. `--allow-dirty` now means "orphan snapshot", not "sweep into commits"

audit/prodready: uncommitted work is captured under `refs/flexfactor-wip/*`,
the run works on HEAD, publication is refused until separation is proven, and
the WIP is restored byte-for-byte (or the ref is retained and the run says so).
The audit `--help` text still shows the old wording (known stale). Scout's
`--allow-dirty` help claims the snapshot but scout does not implement it.

## 3. `flexfactor_run.py` is a shim

Launchers resolve it next to themselves (`$PSScriptRoot`); it forwards to
`flexfactor.run_cli`. Directed orchestration is a hard import. Nothing to
change in `.lnk` shortcuts.

## 4. The `.part` files are gone

`flexfactor_prodready.py` no longer loads `.part1/.part2`; a clean wheel
installed outside the checkout no longer raises FileNotFoundError (CI
`package-artifact` job proves it). `flexfactor_prodready_engine.py` is in
`py-modules`.

## 5. `_UI_EXPLORER_JS` removed

The journey engine lives in `flexfactor_assets/flexfactor_explorer.js`
(package data). Anyone importing the constant must switch to
`flexfactor_journeys.explorer_script_path()`.

## 6. New environment variables (journeys)

| Var | Meaning |
|---|---|
| `FLEXFACTOR_E2E_ROLES` | JSON list `[{name, cookies?, localStorage?, login?:{url, fields, submit}}]`; `anonymous` implicit |
| `FLEXFACTOR_E2E_VIEWPORTS` | `"1280x800,390x844"` (default) |
| `FLEXFACTOR_E2E_MAX_PAGES` | route cap, default 500; reaching it is named in `incomplete_reasons` |
| `FLEXFACTOR_E2E_ISOLATED` | `1` = disposable env: real submissions + destructive controls |

## 7. Other visible changes

- Run manifest gains `partial_output_events`, `execution_ledger`, `containment`,
  `wip_snapshot_ref`, `wip_restore`, `purpose_confidence`,
  `purpose_mutation_authorized`, `trust_repo_override`.
- Weakly-inferred / unresolved purpose: gaps are reported, not bridged.
- Final review is chunked; `patch_truncated` is always false.
- Files > 4 MB are `analyzed-in-chunks`; > 64 MiB `blocked`.
- cmdpolicy classifies pip/poetry/uv/cargo/go/dotnet/mvn/gradle/make/... so
  those now cross the broker (and the trust gate).

## 8. Still to do before calling this 0.5.0

Bump `pyproject.toml` version, `TOOL_VERSION`, and the CI parity assertion
(done: asserts 0.5.0); `head_matches` argv fixed (`_git_argv`); decide
whether audit/prodready get `--trust-repo`; see docs/CURRENT_STATE_GAP.md.

## Runs are manual (owner decision 2026-08-22)
Do not create scheduled tasks for audit/prodready/scout. Launch from the desktop shortcuts or the CLI; pass `--trust-repo` (or set persistent trust) for repositories whose install/build/test may run.
