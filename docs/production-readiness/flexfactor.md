# FlexFactor production readiness

**Agent:** production-agent-flexfactor  
**Branch:** `production-ready/flexfactor`  
**Worktree:** `C:\Users\firer\flexfactor-wt-flexfactor`  
**Source of truth:** `buckeye7066/flexfactor` @ `main`  
**Launcher:** `C:\Users\firer\flexfactor\flexfactor_launch.ps1`  
**Updated:** 2026-08-08  

## Purpose

Trustworthy local auditor/refactorer; verifier outage must fail closed. Scout is a separate mode/agent — this report covers FlexFactor core only.

## Phase A — Source of truth

| Item | Evidence |
|------|----------|
| GitHub | `buckeye7066/flexfactor` private; default `main` |
| Baseline SHA at relaunch | `04dd785769bb20da3415f524e9b9346ef4e7c458` |
| Merged main SHA (this wave) | `6db4811fde683c5322158d6009376b3d822fc695` |
| PRs | [#3](https://github.com/buckeye7066/flexfactor/pull/3) fail-closed MERGED; [#5](https://github.com/buckeye7066/flexfactor/pull/5) run-manifest artifact MERGED |
| Open PRs after merge | none for FlexFactor core |
| Shared with Scout | yes — Scout-only surfaces not edited by this agent |

## Bridge plan (83–87)

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 83 | Verifier unavailability fail-closed: restore pre-change tree, label failed, no UNVERIFIED commit/score | **DONE** | `_fix_files` rolls back + rejects on empty residual; audit labels `FAILED: adversarial verifier unavailable` |
| 84 | Classify source before cloud; default local for sensitive; explicit approval for exceptions | **DONE** | `flexfactor_egress` fail-closed gate; `--redact` / `--allow-sensitive` / policy; `--provider ollama` zero-egress |
| 85 | Installs/builds/tests in constrained sandbox (resource, network, path, process, time) | **PARTIAL** | Path containment, `flexfactor_cmdpolicy`, `--ignore-scripts`, `_run` timeouts. OS-level network/job-object sandbox still deferred (prior owner decision) |
| 86 | Batch/project budgets, immutable run manifests, command/evidence capture, rollback; report-only default | **DONE** | `CostMeter` + `_budget_guard`; `_write_run_manifest` (`flexfactor.run_manifest.v1`); manifests ignored by dirty-tree gate; audit report-only unless `--apply` |
| 87 | Resolve backward-compat mismatch + full suite (outage, dirty worktree, cancel, timeout, partial-failure) | **DONE (Windows)** | UNVERIFIED-keep path removed; `python flexfactor_tests.py` → **352 tests OK** (7 skipped) |

## Ready criteria (88–90)

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 88 | Forced verifier outage → byte-for-byte pre-run tree, no success commit | **DONE** | `AdversarialFixLoopTests.test_c_transport_failure_rolls_back_fail_closed`; `AuditDirtyAbortCommitGuardTests.test_verifier_outage_skips_success_commit` |
| 89 | Sensitive-source + untrusted-build containment on Windows and Linux | **PARTIAL** | Windows: egress + path/cmdpolicy/ignore-scripts tests green. Linux not executed in this session. Full OS network sandbox deferred |
| 90 | Audit/apply unambiguous; every mod tied to manifest, evidence, budget, commit, rollback | **DONE** | Report-only default; apply gated; timestamped run manifest; build gate + rollback paths |

## Local prove (this session)

| Check | Result |
|-------|--------|
| `python flexfactor_tests.py` | OK — 352 passed, 7 skipped |
| Critical outage + manifest tests | OK (re-run after merge) |
| `python flexfactor_dashboard.py --selftest` | OK |
| `python flexfactor.py --help` | OK |
| `flexfactor_launch.ps1` parse (worktree + installed) | OK |

## What was broken / why / fix

| What was broken | Why it failed | Why this fix works |
|-----------------|---------------|--------------------|
| Adversarial verifier transport failure kept the fix as `accepted UNVERIFIED` and allowed it into the applied set | Fail-closed meant “not a clean pass” rather than “restore and reject” | On empty residual (outage), `_replace_contained` restores original bytes, outcome is `reject`, audit aborts with no success commit |
| New run manifests could poison the dirty-tree gate | `_is_flexfactor_artifact` did not recognize `*_run_manifest_*.json` | Artifact matcher includes `_run_manifest_`; unit test locks it |

## Residual blockers (not PRODUCTION READY)

1. **Item 89 Linux:** suite not run on Linux in this wave — owner: run CI / Linux host suite.  
2. **Item 85/89 OS sandbox:** network/job-object isolation for installs/builds remains deferred — owner: accept residual or authorize OS sandbox work.

## Status recommendation

`SOFTWARE COMPLETE, EXTERNAL RELEASE BLOCKER` — fail-closed path merged to `main@6db4811`; Windows prove complete; Linux CI + deferred OS sandbox need owner action before claiming full Ready 88–90.
