# Scout a Program (FlexFactor Scout) — Production Readiness Report

**Program:** Scout a Program (FlexFactor Scout mode)  
**Agent:** production-agent-flexfactor-scout (`9fa9536e-b5c9-4265-a62b-392bb0065d78`)  
**Branch:** `production-ready/flexfactor-scout`  
**Worktree:** `C:\Users\firer\flexfactor-wt-scout`  
**Repo:** buckeye7066/flexfactor (mode, not a separate repo)  
**Launcher:** `C:\Users\firer\flexfactor\flexfactor_scout_launch.ps1`  
**Updated:** 2026-08-08  

## Purpose contract

Bounded discovery-and-evaluation mode that proposes source-pinned components with licensing, maintenance, security, compatibility, and integration evidence. Never installs or executes candidate code outside a disposable sandbox. Never merges automatically. Repo Rewards results are metadata-screened candidates only.

## Ready criteria (98–100)

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 98 | Malicious candidate fixture cannot access host credentials, unrelated files, or unrestricted network | **PASS (env sandbox)** | `malicious_fixture_escape_probe` + `ScoutBridge94to100Tests.test_98_*`: canary secrets stripped, HOME redirected to temp, HTTPS_PROXY poisoned to `127.0.0.1:9`, host-escaping eval argv refused. OS AppContainer not claimed (ISOLATION_SPIKE option A). |
| 99 | Every recommendation is commit-pinned and includes license, security, maintenance, compatibility, benefit, integration cost, rejection reason | **PASS** | Structured report schema `scout-report-v1` via `build_scout_structured_report` / `_scout_report.json`. Required fields enforced in `SCOUT_RECOMMENDATION_REQUIRED_FIELDS`. Test `test_99_*`. |
| 100 | Scout cannot modify or merge into a target app without a separate approved FlexFactor apply run | **PASS** | `scout_may_mutate_target` requires `.flexfactor-apply-approval.json` (or explicit `--legacy-inline-apply` break-glass). `--apply` alone emits proposals. E2E: apply without approval → proposal-only; with committed approval → local branch commit. Merge remains opt-in and unreachable without mutation gate. |

## Bridge (94–97)

| # | Bridge item | Done |
|---|-------------|------|
| 94 | Separate command / config / risk model / report schema | **Yes** — `flexfactor_scout_contract.py` (`SCOUT_MODE`, `SCOUT_RISK_MODEL`, `scout-report-v1`); `scout` CLI + launcher |
| 95 | Repo Rewards as metadata-only; pin SHA; record license/provenance/activity/advisories/transitive risk/compat | **Yes** — `pin_fields_from_evidence`, `confirm_pin_from_clone`, `metadata_screened_only=True`, `safe_to_install=False` |
| 96 | Disposable sandbox: egress controls, no user credentials, bounded resources, clean teardown | **Yes** — `strip_credential_env`, `sandbox_eval_env`, `disposable_sandbox`, `run_sandboxed_candidate_eval`; clone path strips credentials |
| 97 | Integration proposal (delta / conflict / rollback); explicit owner approval before target mutation | **Yes** — `build_integration_proposal`, artifacts `.flexfactor-scout-proposals.json`; mutation gated by FlexFactor apply approval |

## Coordination

- **FlexFactor core:** Shared repo; Scout changes are additive (`flexfactor_scout_contract.py` + scout-path wiring). Does not alter FlexFactor audit/verifier apply paths except consuming shared helpers.
- **Repo Rewards:** Consumed as metadata-screened search only (`agent-status/repo-rewards.json` was AUDITING). No install/safe-to-run claims from Scout.

## Verification evidence (this session)

```text
python -m unittest flexfactor_tests.ScoutBridge94to100Tests \
  flexfactor_tests.ScoutEndToEndTests \
  flexfactor_tests.ScoutApplyDefaultTests \
  flexfactor_tests.ApplyPhaseInspectionGateTests -v
# Ran 20 tests ... OK
```

Launcher help surface:

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
- Live end-to-end scout against a running Repo Rewards instance + cloud LLM was not required for 98–100 (unit/e2e fixtures prove the gates).  
- Sync launcher copy under `C:\Users\firer\flexfactor\flexfactor_scout_launch.ps1` after merge if desktop shortcut points at main checkout.
