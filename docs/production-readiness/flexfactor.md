# FlexFactor production readiness

**Agent:** production-agent-flexfactor  
**Branch:** `production-ready/flexfactor`  
**Worktree:** `C:\Users\firer\flexfactor-wt-flexfactor`  
**Source of truth:** `buckeye7066/flexfactor` @ `main`  
**Launcher:** `C:\Users\firer\flexfactor\flexfactor_launch.ps1`  
**Date:** 2026-08-08  

## Purpose

Trustworthy local auditor/refactorer; verifier outage must fail closed. Scout is a separate mode/agent — this report covers FlexFactor core only.

## Phase A — Source of truth

| Item | Evidence |
|------|----------|
| GitHub main SHA (start) | `04dd785769bb20da3415f524e9b9346ef4e7c458` |
| Open PRs at start | none |
| Shared with Scout | yes — Scout-only surfaces not edited |

## Bridge plan (83–87)

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 83 | Verifier unavailability fail-closed: restore pre-change tree, label failed, no UNVERIFIED commit/score | **DONE** | `_fix_files` rolls back + rejects on empty residual; audit labels `FAILED: adversarial verifier unavailable` |
| 84 | Classify source before cloud; default local for sensitive; explicit approval for exceptions | **DONE** | `flexfactor_egress` fail-closed gate; `--redact` / `--allow-sensitive` / policy; `--provider ollama` zero-egress |
| 85 | Installs/builds/tests in constrained sandbox (resource, network, path, process, time) | **PARTIAL** | Path containment, `flexfactor_cmdpolicy`, `--ignore-scripts`, `_run` timeouts. OS-level network/job-object sandbox still deferred (prior owner decision) |
| 86 | Batch/project budgets, immutable run manifests, command/evidence capture, rollback; report-only default | **DONE** | `CostMeter` + `_budget_guard`; `_write_run_manifest` (`flexfactor.run_manifest.v1`); audit report-only unless `--apply` |
| 87 | Full suite incl. verifier outage, dirty worktree, cancellation, timeout, partial-failure | **DONE (Windows)** | `python flexfactor_tests.py` → **351 tests OK** (7 skipped) |

## Ready criteria (88–90)

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 88 | Forced verifier outage → byte-for-byte pre-run tree, no success commit | **DONE** | `AdversarialFixLoopTests.test_c_transport_failure_rolls_back_fail_closed`; `AuditDirtyAbortCommitGuardTests.test_verifier_outage_skips_success_commit` |
| 89 | Sensitive-source + untrusted-build containment on Windows and Linux | **PARTIAL** | Windows: egress + path/cmdpolicy/ignore-scripts tests green. Linux not executed in this session. Full OS network sandbox deferred |
| 90 | Audit/apply unambiguous; every mod tied to manifest, evidence, budget, commit, rollback | **DONE** | Report-only default; apply gated; timestamped run manifest; build gate + rollback paths |

## Local prove

| Check | Result |
|-------|--------|
| `python flexfactor_tests.py` | OK — 351 passed, 7 skipped |
| `python flexfactor_dashboard.py --selftest` | OK |
| `python flexfactor.py --help` | OK |
| `flexfactor_launch.ps1` parse (worktree + installed) | OK |
| Critical outage + manifest tests | OK |

## What was broken / why / fix

| What was broken | Why it failed | Why this fix works |
|-----------------|---------------|--------------------|
| Adversarial verifier transport failure kept the fix as `accepted UNVERIFIED` and allowed it into the applied set | Fail-closed was defined as “not a clean pass” rather than “restore and reject” | On empty residual (outage signal), `_replace_contained` restores the original bytes, outcome is `reject`, notes carry `fail-closed`, audit aborts with no success commit |

## Residual blockers (not PRODUCTION READY)

1. **Item 89 Linux:** suite not run on Linux in this wave — owner: run CI / Linux host suite.  
2. **Item 85/89 OS sandbox:** network/job-object isolation for installs/builds remains deferred — owner: accept residual or authorize OS sandbox work.

## Status recommendation

`SOFTWARE COMPLETE, EXTERNAL RELEASE BLOCKER` — software fail-closed path and Windows prove complete; Linux CI + deferred OS sandbox need owner action before claiming full Ready 88–90.
