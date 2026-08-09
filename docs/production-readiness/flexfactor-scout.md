# Scout a Program (FlexFactor Scout) — Production Readiness Report

**Program:** Scout a Program (FlexFactor Scout mode)  
**Agent:** Cursor portfolio executor  
**Branch:** `cursor/production-ready/flexfactor-scout`  
**Worktree:** `C:\Users\firer\flexfactor`  
**Repo:** buckeye7066/flexfactor (mode, not a separate repo)  
**Launcher:** `C:\Users\firer\flexfactor\flexfactor_scout_launch.ps1`  
**Updated:** 2026-08-09  
**Consumed FlexFactor core / main SoT at wave start:** `283aa507931410dc6d14088dbcdf9ea14975977b`  
**Repo Rewards consumed:** PRODUCTION READY at Railway `https://web-production-d7db7.up.railway.app`

## Purpose contract

See `docs/purpose-contract-scout.md` (v1.0). Bounded discovery-and-evaluation mode that proposes source-pinned components with licensing, maintenance, security, compatibility, benefit, cost, and rejection evidence. Never installs or executes candidate code outside a disposable sandbox. Never merges automatically. Proposal-only default; target mutation requires separate FlexFactor apply approval. Repo Rewards results are metadata-screened candidates only.

## Phase A — Source of truth

| Item | Evidence |
|------|----------|
| GitHub | `buckeye7066/flexfactor` private; Scout is a mode in this repo |
| FlexFactor core on main (wave start) | `283aa507931410dc6d14088dbcdf9ea14975977b` |
| Repo Rewards | Railway production; `/api/version` + search proven PRODUCTION READY |
| Launcher | `flexfactor_scout_launch.ps1` defaults to ollama; prefers local RR `:3000` when healthy else production; passes `--no-auto-start` |

## Ready criteria (98–100)

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 98 | Malicious candidate fixture cannot access host credentials, unrelated files, or unrestricted network | **PASS (env sandbox)** | `ScoutBridge94to100Tests.test_98_*`: canary secrets stripped, HOME redirected, HTTPS_PROXY poisoned, host-escaping eval argv refused. OS AppContainer not claimed. |
| 99 | Every recommendation is commit-pinned and includes license, security, maintenance, compatibility, benefit, integration cost, rejection reason | **PASS (fail-closed)** | `scout-report-v1` via `build_scout_structured_report`. Unpinned SHA → `safe_to_integrate=false` with rejection reasons. Live report shows explained CONSIDER + rejections. |
| 100 | Scout cannot modify or merge into a target app without a separate approved FlexFactor apply run | **PASS** | `scout_may_mutate_target` requires `.flexfactor-apply-approval.json` (or `--legacy-inline-apply`). Live `--apply --yes` without approval: target file hash unchanged; proposals written; mutation message printed. |

## Bridge (94–97)

| # | Bridge item | Done |
|---|-------------|------|
| 94 | Separate command / config / risk model / report schema | **Yes** — `flexfactor_scout_contract.py`; Scout artifacts excluded from dirty-tree gate |
| 95 | Repo Rewards as metadata-only; pin SHA; record license/provenance/activity/advisories/transitive risk/compat | **Yes** — `pin_fields_from_evidence`, `metadata_screened_only=True` |
| 96 | Disposable sandbox: egress controls, no user credentials, bounded resources, clean teardown | **Yes** — env/credential/egress sandbox |
| 97 | Integration proposal; explicit owner approval before target mutation | **Yes** — proposals always emitted as evidence; mutation gated |

## This-wave change

1. **Production RR fallback** in `flexfactor.py`: when local `localhost:3000` / `127.0.0.1:3000` is down and URL was not an explicit non-local override, fall back to `PRODUCTION_REPO_REWARDS_URL` (default Railway). Auto-start still local-only.
2. **Launcher** prefers env → local health → production; defaults provider to ollama; always `--no-auto-start` with resolved URL.
3. **Purpose Contract** written (`docs/purpose-contract-scout.md`).
4. **Unit:** `test_production_rr_fallback_when_local_down`.

## Verification evidence (this session)

```text
python -m unittest flexfactor_tests.ScoutBridge94to100Tests \
  flexfactor_tests.ScoutEndToEndTests \
  flexfactor_tests.ScoutApplyDefaultTests -v
# Ran 19 tests ... OK
```

Live journeys (Ollama `phi3:latest` + production RR):

| Journey | Fixture | Result |
|---------|---------|--------|
| Report-only | `ff-scout-journey-20260809094452` | RR hits (e.g. 30 results); CONSIDER + explained rejections; `index.js` unchanged; Scout artifacts only |
| `--apply --yes` without approval | `ff-scout-apply-20260809095329` | `HASH_UNCHANGED=True`; proposals written; "Target mutation requires separate FlexFactor apply approval" |

Evidence copies: `docs/evidence/scout/`.

## False substitutes rejected

- safe-to-install / safe-to-run labels from metadata  
- host credential access during candidate eval  
- auto-merge into target  
- treating unpinned CONSIDER as integrate-safe  

## Residual / external

- Full OS filesystem jail (AppContainer) remains the airtight successor; current gate is env/credential/egress sandbox (same residual as FlexFactor core).  
- Profiling quality with small local models (phi3) can be noisy; fail-closed integrate verdicts still hold.
