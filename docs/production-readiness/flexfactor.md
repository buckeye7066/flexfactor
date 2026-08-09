# FlexFactor production readiness

**Executor:** Cursor  
**ACTIVE_APP wave:** 2026-08-09  
**Repository:** `buckeye7066/flexfactor`  
**Verified default branch:** `main`  
**Baseline SHA (wave start):** `808ce68decdcfea6b859c455654d8bfb4c42bb64`  
**Final default-branch SHA:** `bd00de667e608e625e6c59be709e63078cf624ff`  
**Launcher:** `C:\Users\firer\flexfactor\flexfactor_launch.ps1`  
**Status:** PRODUCTION READY  

## Purpose Contract

See `docs/purpose-contract.md`. Trustworthy local auditor/refactorer: fail-closed on verifier loss, contained reads/writes, reproducible manifests, deterministic rollback; report-only by default.

## Phase A — Source of truth

| Item | Evidence |
|------|----------|
| GitHub | `buckeye7066/flexfactor` private; default `main` |
| Wave baseline | `808ce68` |
| PR #10 | Merged 2026-08-09T10:59:47Z → `bd00de6` |
| Post-merge CI | production-readiness success on `main` (run 31309692247) |
| Open production PRs | none required after evidence packet |

## Current-state findings (wave)

| Class | Finding | Resolution |
|-------|---------|------------|
| Purpose blocker | Linux CI false fail-closed (`os.lstat` not in `supports_dir_fd` on CPython 3.12) | Fixed: detect via `os.stat`; `/proc` rename when replace lacks dir_fd |
| High | Windows parent-swap test flaky vs empty-dir identity fallback | Test hardened; production residual documented |
| External | Full OS network/job-object sandbox | Accepted known limitation (path/cmdpolicy/`--ignore-scripts` containment in place) |

## Implemented this wave

- `.github/workflows/production-readiness.yml` (windows-latest + ubuntu-latest + capability probe)
- `docs/purpose-contract.md`
- Containment detection fix in `flexfactor.py`
- Containment test hardening in `flexfactor_tests.py`

## Tests

| Suite | Result |
|-------|--------|
| Local full suite | 363 OK, 7 skipped |
| CI windows-tests | success |
| CI linux-containment | success (`HAS_DIR_FD True`, `POSIX_NOFOLLOW True`, `HAS_REPLACE_DIR_FD False`) |
| Verifier-outage regressions | pass (local + CI) |

## Live journeys (inspected)

### Report-only audit

- Fixture: `C:\Users\firer\AppData\Local\Temp\ff-journey-20260809070239`
- Command: `python flexfactor.py audit --program <fixture> --report-only --provider ollama --model phi3:latest --single --no-adversarial --max-files 1 --max-cost 2 ...`
- Result: 1 medium defect found; **app.py SHA unchanged**; audit report + run manifest written
- Evidence: `docs/evidence/report-only-audit-report.md`, `docs/evidence/report-only-run-manifest.json`

### Explicit apply (refactor)

- Fixture: `C:\Users\firer\AppData\Local\Temp\ff-apply-journey-20260809070617`
- Command: `python flexfactor.py --file app.py --goal "..." --provider ollama --model phi3:latest --threshold 70 --max-iterations 3`
- Result: file mutated (docstring + zero-division guard); backup `app.py.bak`; grade 80 meets_goal
- Evidence: `docs/evidence/apply-journey-result-app.py`, `docs/evidence/apply-journey.log`

## Review

`docs/reviews/SEQUENTIAL_REVIEW.md` — APPROVE; zero unresolved P0/P1.

## Release evidence packet

```
Application: FlexFactor
Executor: Cursor
Purpose and Acceptance Contract: docs/purpose-contract.md
Honest status: PRODUCTION READY
Repository: buckeye7066/flexfactor
Verified default branch: main
Baseline SHA: 808ce68decdcfea6b859c455654d8bfb4c42bb64
Final default-branch SHA: bd00de667e608e625e6c59be709e63078cf624ff
Local launcher: C:\Users\firer\flexfactor\flexfactor_launch.ps1
Deployment/package/install identity: local CLI + flexfactor_launch.ps1 @ main bd00de6
Primary journey: report-only audit + explicit refactor apply
Primary journey result: PASS (unchanged tree report-only; mutated apply with backup)
Actual output inspected: audit report + resulting app.py
Build/lint/type: N/A (pure Python script suite)
Unit/integration: 363 OK / 7 skipped; CI Windows+Linux green
Security/privacy: containment + egress gate; residual OS sandbox noted
Independent review: docs/reviews/SEQUENTIAL_REVIEW.md
Open P0/P1: none
Known limitations: OS network/job-object sandbox deferred; Windows empty-dir _same_id residual
External prerequisites: none for core FlexFactor
Rollback method: git revert bd00de6; refactor writes .bak beside target
Production-ready decision: YES
Evidence locations: docs/evidence/*, docs/reviews/SEQUENTIAL_REVIEW.md, GH Actions run 31309692247
```

## What was broken / why / fix

| What was broken | Why it failed | Why this fix works |
|-----------------|---------------|--------------------|
| Linux suite fail-closed on normal files | `_HAS_DIR_FD` required `os.lstat in supports_dir_fd`; CPython 3.12 omits it | Require `os.stat` instead; lstat(dir_fd=) still used at runtime |
| Linux rename path when replace lacks dir_fd | Requiring replace dir_fd forced fail-closed | Rename via `/proc/self/fd/{parent}` while holding O_NOFOLLOW parent fd |
| Windows CI parent-swap test false fail | Empty-dir identity fallback + fragile stat counting | Fail-closed via `_same_id` after `.tmp` present |

## Residual risks (non-blocking)

1. OS-level network/job-object isolation for installs/builds remains unimplemented.
2. Scout mode is a **separate** queue program — not certified by this packet.
