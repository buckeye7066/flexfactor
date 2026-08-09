# Scout a Program — sequential review (Cursor executor)

**Date:** 2026-08-09  
**Branch:** `cursor/production-ready/flexfactor-scout`  
**Base main SoT:** `283aa507931410dc6d14088dbcdf9ea14975977b`

## Scope

Production RR fallback + launcher defaults; Purpose Contract; unit gates 94–100; live report-only and apply-without-approval journeys against disposable fixtures using PRODUCTION READY Repo Rewards.

## Product

- Purpose matches Appendix A / `docs/purpose-contract-scout.md`: propose commit-pinned components with full evidence; never auto-merge; proposal-only default.
- Report-only and `--apply` without `.flexfactor-apply-approval.json` do not mutate target files (live hash proof).
- Consumes Repo Rewards as metadata-screened search only.

## Architecture / implementation

- `DEFAULT_REPO_REWARDS_URL` / `PRODUCTION_REPO_REWARDS_URL` / `LOCAL_REPO_REWARDS_URLS` with logged fallback when local is down.
- Explicit non-local `--repo-rewards-url` is not rewritten to production.
- Launcher resolves RR URL (env → local health → production) and passes `--no-auto-start`.

## Security / privacy

- Env/credential/egress sandbox for candidate eval remains the claimed boundary.
- Unpinned `commit_sha` fails closed on `safe_to_integrate`.
- Residual: OS AppContainer not claimed.

## QA

| Gate | Result |
|------|--------|
| ScoutBridge94to100 + EndToEnd + ApplyDefault | 19 OK |
| `test_production_rr_fallback_when_local_down` | PASS |
| Live report-only vs Railway RR | PASS (results + artifacts; target unchanged) |
| Live `--apply --yes` without approval | PASS (`HASH_UNCHANGED=True`) |

## Accessibility / UX

Launcher prompts provider (default ollama) and report/apply; apply path warns that mutation still needs FlexFactor approval file.

## Findings

| Sev | Finding | Disposition |
|-----|---------|-------------|
| P2 | OS AppContainer not claimed | Accepted residual (same as FlexFactor) |
| P3 | Small local models may profile noisily | Accepted; integrate gate remains fail-closed |

**P0/P1 unresolved:** none  

**Review decision:** APPROVE for Scout PRODUCTION READY after CI green and merge to main.
