# Managed Refactor Test Authorization Implementation Plan

**Goal:** Let an owner-confirmed mobile Refactor execute its selected repository tests through the existing trust gate.
**Architecture:** Supply the exact target checkout to the existing process-local trusted-repository configuration for the Refactor invocation only. Do not change the broker, sandbox, publication policy, other modes, persistent policy, or unrelated repositories.
**Tech stack:** Existing Bash workflow, Python trust gate and unittest regressions.
**Evidence:** Released 3.5.12 target run 35450746019 reached the test gate, which refused the untrusted target; the candidate was restored and exit code was 1.

## Constraints and review focus
Trust only the selected target and its contained paths, not its siblings, the engine, or the runner workspace. Preserve the parent environment. Preserve refusal without explicit trust. Keep project tests and publication checks mandatory. Existing Scout proof 35450820727 must finish before a cloud activation changes its deployed source identity.

## Implementation
- [x] Add an executable Bash-wiring regression that captures the actual Refactor invocation environment and confirms containment decisions for target versus siblings.
- [x] Observe the existing workflow fail that regression.
- [x] Add one command-scoped FLEXFACTOR_TRUSTED_REPOS assignment; run the new regression and existing trust, sandbox, Android and release tests.
- [ ] Stage 3.5.13 through the existing one-patch tag-first release mechanism; get independent review and all hosted checks, then merge normally.
- [ ] Publish signed 3.5.13, activate matching cloud only after prior proof terminates, and repeat remaining-mode acceptance with a new correlated request.
- [ ] Verify actual source repairs, result retrieval and request-scoped cleanup; preserve the separate human GitHub approval prerequisite.
