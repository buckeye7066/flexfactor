# Purpose & Acceptance Contract — Scout a Program (FlexFactor Scout)

**Version:** 2.0
**Application:** Scout a Program
**Executor:** Cursor
**Repository:** buckeye7066/flexfactor (scout mode; not a separate repo)
**Launcher:** `flexfactor_scout_launch.ps1`
**Consumes:** Repo Rewards (metadata-screened search only)

## 1. What it was created to do

Retrieve an explicitly entered public program/product URL, understand its evidenced workflows and capabilities, compare those capabilities with the target program's evidenced purpose and current behavior, and identify only the portions that would materially optimize the target. Repo Rewards then finds **commit-pinned** open-source implementations for those accepted capability deltas. Scout stops at reports/proposals unless a separate FlexFactor apply approval exists.

## 2. Production users

Owners/operators of local programs who want bounded discovery before any integration, and agents (e.g. Factory / FlexFactor apply) that consume Scout proposals.

## 3. Primary problem

Turning a program website into a cited target-versus-source capability comparison without inventing features from a name or treating repository search results as understanding of the program.

## 4. Major workflows

1. Select the target program to optimize.
2. Enter one or more public program/product website URLs. Scout does not accept a product name, local path, or source-repository URL in this field.
3. Retrieve the entered URL and bounded same-site feature, workflow, documentation, integration, and use-case pages. Record every retrieved page as cited source evidence.
4. Inspect the target independently, then profile the target and scouted URL independently.
5. Compare source capabilities against real target gaps. Each decision must cite both target and source evidence and include an adaptation and verification plan.
6. Send only accepted implementation queries to Repo Rewards; repository discovery remains Repo Rewards' responsibility.
7. Evaluate returned repository candidates in a disposable credential-stripped sandbox.
8. Write structured reports with accepted and rejected decisions explained.
9. Optional `--apply`: emit integration proposals; target mutation still requires `.flexfactor-apply-approval.json` (or explicit `--legacy-inline-apply` break-glass).

## 5–6. Inputs / outputs

**In:** target program; public scouted-program website URL; provider; optional Repo Rewards endpoint.
**Out:** target profile, URL profile, cited capability comparison, implementation-search ledger, repository candidate report, and optional proposals; never silent auto-merge.

## 7. Essential integrations

Public HTTP(S) retrieval for the entered program URL. Repo Rewards HTTP search (`/api/search`) is used only after comparison, for exact accepted capability deltas, with `scoutContract.safeToRun/safeToInstall === false`. Local RR is preferred; production Railway is the fallback when local is down.

## 8. Standards

- Metadata-screened only; never safe-to-run / safe-to-install.
- Source capability claims cite retrieved URL evidence (`S#`); target-state claims cite target evidence (`T#`).
- Search snippets and repository metadata cannot prove a scouted-program capability.
- Private, loopback, link-local, credential-bearing, unsafe-redirect, non-text, and oversized URL responses fail closed.
- Commit pins required for recommendations.
- Credentials stripped; egress poisoned; sandbox torn down.
- Scout artifacts must not poison FlexFactor dirty-tree gate.

## 9. Failure / recovery

Invalid/non-public URL, failed retrieval, or insufficient readable content → fail with a named evidence gap and no inferred capabilities. Unreachable Repo Rewards is recorded after the program comparison; it does not erase the comparison. Sandbox teardown is unconditional. Verifier/build gates remain fail-closed on the apply path.

## 10. Deployment / package

Local Windows launcher + `python flexfactor.py scout ...` against buckeye7066/flexfactor `main`.

## 11. Tests before Production Ready

- ScoutBridge94to100 (+ EndToEnd / ApplyDefault / Verdict / Policy / Eval)
- URL crawler retrieves relevant same-site pages and assigns stable evidence identifiers
- Non-URL and repository inputs are rejected from the Scout-source field
- Forged/missing `T#` or `S#` references are rejected and retained in the validation ledger
- End-to-end fixture proves the exact accepted capability delta becomes the Repo Rewards query
- Malicious fixture cannot see host credentials / host-escaping argv refused
- Proposal-only without apply approval
- Live report-only journey against production Repo Rewards
- Launcher parse + help surfaces approval gate

## 12. False substitutes

- Treating RR scores as install approval
- Treating the entered URL as a label without fetching it
- Accepting a product name/free text and inventing capabilities from it
- Sending a repository URL to Scout instead of using Repo Rewards for repository discovery
- Profiling only the target and generating generic opportunity keywords
- Returning a capability without separate target and source citations
- Auto-merge / mutate without FlexFactor apply approval
- Claiming OS AppContainer jail when only env sandbox is present
- Empty recommendations with unexplained rejections when candidates exist
