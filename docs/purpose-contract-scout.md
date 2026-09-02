# Purpose & Acceptance Contract — Scout a Program (FlexFactor Scout)

**Version:** 1.0  
**Application:** Scout a Program  
**Executor:** Cursor  
**Repository:** buckeye7066/flexfactor (scout mode; not a separate repo)  
**Launcher:** `flexfactor_scout_launch.ps1`  
**Consumes:** public product/documentation URLs via Scout; repositories via Repo Rewards

## 1. What it was created to do

Establish what a target program exists to do, research real competitors from fetched public product/documentation pages, and propose **commit-pinned** open-source components that could improve it. Preserve licensing, maintenance, security, compatibility, benefit, cost, and rejection evidence, then stop at proposals unless a separate FlexFactor apply approval exists.

## 2. Production users

Owners/operators of local programs who want bounded discovery before any integration, and agents (e.g. Factory / FlexFactor apply) that consume Scout proposals.

## 3. Primary problem

Finding relevant competitor capabilities and repositories without confusing URL research with repository search, claiming candidates are safe to run/install, or mutating the target by default.

## 4. Major workflows

1. Point Scout at a local repository (a shortcut/URL may be used only when it resolves to local source).
2. Establish an evidence-cited purpose contract: primary users, core journeys, goals, and acceptance criteria.
3. Profile capability opportunities and create two distinct query streams.
4. Scout executes public product/documentation URL searches and fetches relevant pages; Repo Rewards separately searches repositories.
5. Glean only ideas backed by an exact fetched evidence ID, then judge them against the target's purpose.
6. Evaluate repository candidates in a disposable credential-stripped sandbox.
7. Write a structured report with recommendations, rejections, query receipts, and skipped-source reasons.
8. Optional `--apply`: emit integration proposals only.
9. Target mutation only with `.flexfactor-apply-approval.json` (or explicit `--legacy-inline-apply` break-glass).

## 5–6. Inputs / outputs

**In:** local program repository (directly or through a resolvable locator); provider; optional Repo Rewards URL.
**Out:** purpose contract, Scout URL-search and Repo Rewards query receipts, fetched-source evidence, scout report, and optional proposals; never silent auto-merge.

## 7. Essential integrations

- Scout public-web search for product and documentation URLs, followed by bounded page fetches. Search snippets alone cannot support an idea, and a fetched page must identify the named competitor.
- Repo Rewards HTTP repository search (`/api/search`) with `scoutContract.safeToRun/safeToInstall === false`. Local RR is preferred; production fallback is used when permitted and local RR is down.

## 8. Standards

- Metadata-screened only; never safe-to-run / safe-to-install.  
- Commit pins required for recommendations.  
- Credentials stripped; egress poisoned; sandbox torn down.  
- Scout artifacts must not poison FlexFactor dirty-tree gate.

## 9. Failure / recovery

Unreachable Repo Rewards is named but does not erase Scout's separate URL research. Missing local source or an incomplete evidence-cited purpose contract stops Scout before research can be treated as program-specific. Unfetched or irrelevant competitor pages cannot support an idea. Sandbox teardown always. Verifier/build gates remain fail-closed on the apply path.

## 10. Deployment / package

Local Windows launcher + `python flexfactor.py scout ...` against buckeye7066/flexfactor `main`.

## 11. Tests before Production Ready

- ScoutBridge94to100 (+ EndToEnd / ApplyDefault / Verdict / Policy / Eval)  
- Malicious fixture cannot see host credentials / host-escaping argv refused  
- Proposal-only without apply approval  
- Separate Scout URL-query and Repo Rewards repository-query execution receipts
- Irrelevant/unfetched pages cannot donate competitor ideas
- Live report-only journey against production Repo Rewards  
- Launcher parse + help surfaces approval gate  

## 12. False substitutes

- Treating RR scores as install approval  
- Sending Scout URL queries to Repo Rewards or calling repository results public-page research
- Treating a search snippet or unrelated fetched page as capability evidence
- Auto-merge / mutate without FlexFactor apply approval  
- Claiming OS AppContainer jail when only env sandbox is present  
- Empty recommendations with unexplained rejections when candidates exist  
