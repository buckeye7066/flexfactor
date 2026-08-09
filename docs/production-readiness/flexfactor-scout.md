# Scout a Program (FlexFactor Scout) — Production Readiness Report

**Program:** Scout a Program (FlexFactor Scout mode)  
**Agent:** production-agent-flexfactor-scout (`c643ca56-4e4f-4717-a577-4a975d0cef14`)  
**Branch:** `production-ready/flexfactor-scout`  
**Worktree:** `C:\Users\firer\flexfactor-wt-scout`  
**Repo:** buckeye7066/flexfactor (mode, not a separate repo)  
**Launcher:** `C:\Users\firer\flexfactor\flexfactor_scout_launch.ps1`  
**Updated:** 2026-08-08  
**Consumed FlexFactor core:** `main@5ed5f5f` (software merge `4ef4e8b`)  
**Scout wave merged main:** `0d72f75ec508b5a0eedad4e7e6665f9ec2bb07ce` (PR #8)

## Purpose contract

Bounded discovery-and-evaluation mode that proposes source-pinned components with licensing, maintenance, security, compatibility, and integration evidence. Never installs or executes candidate code outside a disposable sandbox. Never merges automatically. Repo Rewards results are metadata-screened candidates only.

## Phase A — Source of truth

| Item | Evidence |
|------|----------|
| GitHub | `buckeye7066/flexfactor` private; Scout is a mode in this repo |
| FlexFactor core on main | `5ed5f5f1aa70b29443041b9a41182feb6ba37a69` (SOFTWARE COMPLETE) |
| Prior Scout merge | PR [#4](https://github.com/buckeye7066/flexfactor/pull/4) already on main |
| This wave worktree | Fast-forwarded to `origin/main@5ed5f5f`, then additive dirty-tree fix |
| Launcher | `flexfactor_scout_launch.ps1` identical desktop ↔ worktree; `python flexfactor.py scout --help` surfaces `--apply` / `--legacy-inline-apply` / approval gate |

## Ready criteria (98–100)

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 98 | Malicious candidate fixture cannot access host credentials, unrelated files, or unrestricted network | **PASS (env sandbox)** | `ScoutBridge94to100Tests.test_98_*`: canary secrets stripped, HOME redirected to temp, HTTPS_PROXY poisoned to `127.0.0.1:9`, host-escaping eval argv refused. OS AppContainer not claimed. |
| 99 | Every recommendation is commit-pinned and includes license, security, maintenance, compatibility, benefit, integration cost, rejection reason | **PASS** | `scout-report-v1` via `build_scout_structured_report` / `_scout_report.json`. Required fields in `SCOUT_RECOMMENDATION_REQUIRED_FIELDS`. `test_99_*`. |
| 100 | Scout cannot modify or merge into a target app without a separate approved FlexFactor apply run | **PASS** | `scout_may_mutate_target` requires `.flexfactor-apply-approval.json` (or `--legacy-inline-apply`). `--apply` alone = proposals. E2E: without approval → proposal-only; with committed approval → local branch commit. |

## Bridge (94–97)

| # | Bridge item | Done |
|---|-------------|------|
| 94 | Separate command / config / risk model / report schema | **Yes** — `flexfactor_scout_contract.py`; `scout` CLI + launcher; Scout artifacts excluded from dirty-tree gate |
| 95 | Repo Rewards as metadata-only; pin SHA; record license/provenance/activity/advisories/transitive risk/compat | **Yes** — `pin_fields_from_evidence`, `metadata_screened_only=True`, `safe_to_install=False` |
| 96 | Disposable sandbox: egress controls, no user credentials, bounded resources, clean teardown | **Yes** — `strip_credential_env`, `disposable_sandbox`, `run_sandboxed_candidate_eval` |
| 97 | Integration proposal (delta / conflict / rollback); explicit owner approval before target mutation | **Yes** — `build_integration_proposal`, `.flexfactor-scout-proposals.json`; mutation gated by FlexFactor apply approval |

## This-wave change (additive; no FlexFactor core rewrite)

Prior Scout report/proposal files (`_scout_report.json`, `.flexfactor-scout-proposals.json`) were missing from `_is_flexfactor_artifact`, so a second Scout apply could fail the dirty-tree gate after a report-only run. Extended the same helper FlexFactor core uses for run manifests; added `test_94_scout_artifacts_do_not_poison_dirty_tree_gate`.

## Coordination

- **FlexFactor core:** Consumed as-is at `5ed5f5f`. Scout-only additive surfaces only.
- **Repo Rewards:** `agent-status/repo-rewards.json` still `AUDITING` (wave B relaunch). Scout treats RR as metadata-screened search only; no install/safe-to-run claims.

## Verification evidence (this session)

```text
python -m unittest flexfactor_tests.ScoutBridge94to100Tests \
  flexfactor_tests.ScoutEndToEndTests \
  flexfactor_tests.ScoutApplyDefaultTests \
  flexfactor_tests.ApplyPhaseInspectionGateTests -v
# Ran 21 tests (incl. new dirty-tree artifact test) ... OK
```

Also previously confirmed broader Scout suite (39 tests incl. verdict/policy/eval/fencing) OK on `5ed5f5f` before the artifact patch.

Launcher help:

```text
python flexfactor.py scout --help
# --apply emits proposals; --legacy-inline-apply break-glass; mutation needs approval file
```

## False substitutes rejected

- safe-to-install / safe-to-run labels from metadata  
- host credential access during candidate eval  
- auto-merge into target  

## Residual / external

- Full OS filesystem jail (AppContainer) remains the airtight successor per `ISOLATION_SPIKE.md`; current gate is env/credential/egress sandbox.  
- Live end-to-end scout against a PRODUCTION READY Repo Rewards instance + cloud LLM is an external dependency (RR still AUDITING). Fixture/unit gates prove 94–100 without that live path.  
- Owner action: merge this branch PR; after merge, desktop checkout already uses `C:\Users\firer\flexfactor\flexfactor.py` — pull main so launcher picks up the artifact fix.
