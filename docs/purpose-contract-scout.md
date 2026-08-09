# Purpose & Acceptance Contract — Scout a Program (FlexFactor Scout)

**Version:** 1.0  
**Application:** Scout a Program  
**Executor:** Cursor  
**Repository:** buckeye7066/flexfactor (scout mode; not a separate repo)  
**Launcher:** `flexfactor_scout_launch.ps1`  
**Consumes:** Repo Rewards (metadata-screened search only)

## 1. What it was created to do

Propose **commit-pinned** open-source components that could improve a target program, with licensing, maintenance, security, compatibility, benefit, cost, and rejection evidence — then stop at proposals unless a separate FlexFactor apply approval exists.

## 2. Production users

Owners/operators of local programs who want bounded discovery before any integration, and agents (e.g. Factory / FlexFactor apply) that consume Scout proposals.

## 3. Primary problem

Finding relevant repos without claiming they are safe to run/install, and without mutating the target by default.

## 4. Major workflows

1. Point Scout at a program (folder / file / URL / description).  
2. Profile opportunities; search Repo Rewards.  
3. Evaluate candidates in a disposable credential-stripped sandbox.  
4. Write structured report (recommendations + rejections explained).  
5. Optional `--apply`: emit integration proposals only.  
6. Target mutation only with `.flexfactor-apply-approval.json` (or explicit `--legacy-inline-apply` break-glass).

## 5–6. Inputs / outputs

**In:** program path/description; provider; optional Repo Rewards URL.  
**Out:** scout report + optional proposals; never silent auto-merge.

## 7. Essential integrations

Repo Rewards HTTP search (`/api/search`) with `scoutContract.safeToRun/safeToInstall === false`. Local RR preferred; production Railway fallback when local is down.

## 8. Standards

- Metadata-screened only; never safe-to-run / safe-to-install.  
- Commit pins required for recommendations.  
- Credentials stripped; egress poisoned; sandbox torn down.  
- Scout artifacts must not poison FlexFactor dirty-tree gate.

## 9. Failure / recovery

Unreachable RR → fail with clear error (after local auto-start attempt and production fallback). Sandbox teardown always. Verifier/build gates remain fail-closed on apply path.

## 10. Deployment / package

Local Windows launcher + `python flexfactor.py scout ...` against buckeye7066/flexfactor `main`.

## 11. Tests before Production Ready

- ScoutBridge94to100 (+ EndToEnd / ApplyDefault / Verdict / Policy / Eval)  
- Malicious fixture cannot see host credentials / host-escaping argv refused  
- Proposal-only without apply approval  
- Live report-only journey against production Repo Rewards  
- Launcher parse + help surfaces approval gate  

## 12. False substitutes

- Treating RR scores as install approval  
- Auto-merge / mutate without FlexFactor apply approval  
- Claiming OS AppContainer jail when only env sandbox is present  
- Empty recommendations with unexplained rejections when candidates exist  
