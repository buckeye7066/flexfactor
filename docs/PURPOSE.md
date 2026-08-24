# FlexFactor - Purpose (governing document, 0.5.0 working tree)

Machine-readable twin: `.flexfactor-purpose.json` (validated against
`docs/purpose-contract.schema.json`; `flexfactor_purpose.find_contract` resolves
it first for this repo). Source of truth for wording:
`memory/doctrine/flexfactor-governing-purpose-and-completion-contract.md`.
Status of every claim: VERIFIED = read in the working tree or observed in a run
this session; UNKNOWN/BLOCKED = labelled as such. Nothing here is aspirational
unless it sits in the "Not implemented / BLOCKED" list.

## 1. Governing purpose (doctrine wording)

FlexFactor is a trustworthy local code auditor/refactorer. Before changing
anything it creates a **complete inventory** of the target (no file silently
excluded; large files divided into reviewable sections, never truncated). It
**determines what the program was created to accomplish** from the total
evidence - code, behaviour, docs, PRs, issues, history, tests, schemas - citing
that evidence and naming contradictions instead of weakening the purpose to
match the implementation; the purpose is translated into testable acceptance
criteria before correction begins. It **inspects every relevant file line by
line**, then **runs and attempts to break** the program (every first-party
function, route, control, role, mode; mock-only evidence is not end-to-end
proof). It **corrects** what it finds, including cross-file and architectural
problems, and **every retained correction is verified** - a failed, unavailable
or unknown verification never lets a change be represented as complete.
Verification is **iterative** (round two rescans every file changed in round
one; a fourth round is an escalation that is shown, never hidden). Completion
is declared only when every file is accounted for, every function exercised,
every journey tested, all acceptance criteria pass on the exact final
revision, that revision is packaged/installed and re-tested, and no
release-blocking defect or unresolved fourth-round finding remains. Cost,
provider, service or time limits may **pause and checkpoint** the work, but
they produce a truthful incomplete or blocked status - **never a success**.

Non-negotiables (PROJECT-BRIEF.md, purpose-contract.md): never retain
unverified changes; never leak sensitive source; fail closed on verifier loss;
contain untrusted installs/builds; reproducible evidence; deterministic
rollback.

## 2. Modes

| Mode | Entry | What it does today (VERIFIED) |
|---|---|---|
| refactor | `flexfactor --file F --goal "..."` | rewrite -> grade -> accept one file |
| scout | `flexfactor scout --program P` | profile, Repo Rewards search, benefit judging, proposals; mutation only with `--apply` + `.flexfactor-apply-approval.json` (`flexfactor_scout_contract.FLEXFACTOR_APPLY_APPROVAL_FILE`) |
| audit | `flexfactor audit --program P [P2..P10]` | inventory -> purpose baseline -> competitors -> review/fix cycles -> native suite -> live UI journeys -> evidence gates -> independent final review -> commit/push/merge |
| prodready | `flexfactor prodready --program P` | audit with `--fix-severity medium`, readiness rubric on, zero questions |
| policy | `flexfactor policy init|show` | owner policy `~/.flexfactor/policy.json` |

`--report-only` / `--dry-run` do not exist in any mode (argparse exit 2).
Canonical process entry: `flexfactor.run_cli` (see ARCHITECTURE.md).

## 3. Capabilities are SEPARATE, and what governs each today

| Capability | Governed by (VERIFIED in the working tree) |
|---|---|
| Inspect (read files, git log, gh) | Always on. Reads go through the contained no-follow helpers; `gather_purpose_evidence` uses `_git` for git and **raw `subprocess.run` for `gh`** (gap g-5) |
| Contact external services (search, Repo Rewards, GitHub API) | Competitor research ON by default (`--no-competitors`); Repo Rewards remote by default (`--no-remote-repo-rewards`, `FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS=0`) |
| Send source to a cloud provider | Egress gate `flexfactor_egress` on every repo-derived payload; `--redact`, `--allow-sensitive`, `FLEXFACTOR_ALLOW_EGRESS`, policy.json `allow_egress`; `--model-mode free` (the default) removes paid credentials and filters every billable route out of the catalog |
| Mutate the local target | audit/prodready always apply (no review-only); scout needs `--apply` + approval file; gap-driven (purpose) mutation additionally requires `purpose_confidence` in {owner-authored, strongly-inferred} (`mutation_authorized_by_purpose`) |
| Install dependencies | Command class `install`; broker `_run_target_code`; network ON for installs; lifecycle scripts OFF unless `--allow-scripts`; frozen-install helpers exist in `flexfactor_trust` |
| Run target code (build/test/dev server) | Command classes `build`/`test` and `_spawn`; broker; network OFF (proxy-poisoned); authorized by OS sandbox OR trust: `FLEXFACTOR_TRUSTED_REPOS`, `~/.flexfactor/policy.json {"trusted_repos": [...]}`, or **scout-only** `--trust-repo` |
| Destructive / credentialed / deploy commands | `flexfactor_cmdpolicy` refuses (rc 126) unless `FLEXFACTOR_ALLOW_CLASSES` or policy.json `allow_classes` |
| Push | audit/prodready default ON (`--no-push`); scout `--push` opt-in; gated on `final_ok is True` AND `_wip_publish_guard` |
| Merge | audit/prodready default ON (`--no-merge`); same gates; protected trunk falls back to `flexfactor/land-<sha8>` + PR auto-merge; never force-push |
| Deploy | Never performed by FlexFactor (command class `deploy` refused by default) |

## 4. Status vocabulary (exact meanings)

Only `flexfactor_purpose.STATUS_VOCABULARY` may be reported; `DONE` raises.
`production_ready_status()` maps the 20 `PRODUCTION_READY_CONDITIONS` (each
`pass|fail|na|unknown`) onto:

| Status | Meaning |
|---|---|
| QUEUED | Work accepted, nothing has run (never emitted by `production_ready_status`; used by supervisors) |
| IN PROGRESS | No failing condition and no blocker, but open purpose gaps OR a critical condition still `unknown` OR non-release conditions unmet |
| BLOCKED | A `blocked_reason` was given, or any condition is `fail`. Also the truthful end state for cost/provider/verifier/containment stops |
| RELEASE CANDIDATE | Every software condition passes; only release-side proof (`merged`, `ci_on_sha`, `sha_deployed`, `release_identity`) is still `unknown`. NOT a synonym for done |
| PRODUCTION READY | Every applicable condition is `pass` (or `na`). A critical condition that is `unknown` blocks: "an unevaluated property is not evidence of safety" |

`forbidden_claims()` tripwires "build passes", "tests pass", "deployed",
"works locally", "health endpoint returns 200", etc. as readiness claims.

## 5. Principal user journeys

1. Owner double-clicks the desktop shortcut -> `flexfactor_launch.ps1` -> `flexfactor_run.py` (shim) -> `run_cli` -> audit/prodready of up to 10 programs, unattended overnight, `--allow-dirty --auto-clean --yes`.
2. Owner runs `flexfactor audit --program <repo>` on a dirty checkout: WIP goes to an orphan ref, the run works on HEAD, the WIP comes back byte-for-byte.
3. Owner points FlexFactor at a repo that is not trusted on a Windows host: every install/build/test is REFUSED with the authorization steps; the run reports BLOCKED, not clean.
4. A supervisor reads exit code (0/1/2/3), the run manifest and `~/.flexfactor/evidence/<project>/<run>/manifest.json` to decide what happened.
5. Owner runs `scout --program P` and later `scout --apply` with the approval file present.

## 6. Release-blocking acceptance criteria (A-T), required evidence, false substitutes

Lettered to mirror the owner's acceptance tests A-T. I was not handed the
owner's own text of that list; these are derived from doctrine sections 1-8,
purpose-contract.md items 1-5 and the facts established this session. Verify
the mapping against the owner's list before treating the letters as canonical.

| # | Criterion | Required evidence | False substitutes |
|---|---|---|---|
| A | Complete inventory; no silent exclusion | `code-index.json` totals; every source file `analyzed` / `analyzed-in-chunks` / `blocked`+reason | "Files reviewed: N" without a denominator |
| B | Purpose cited, contradictions named, confidence graded; weak purpose never drives gap mutation | `purpose_confidence`, `purpose_mutation_authorized`, evidence block with `path:line` | README paraphrase; model impression |
| C | Every file reviewed line by line in numbered chunks; partial never = clean | `review_ledger`, `partial_output_events`, `PartialOutputError` on empty salvage | truncated JSON read as empty findings |
| D | Every target-code execution crosses the broker | `execution_ledger` rows with `mechanism`, `basis` | a subprocess call outside `_run`/`_spawn` |
| E | Untrusted repo on a no-sandbox host is REFUSED rc 126 | ledger row `refused: true` + reason naming env/policy | silently running the install |
| F | Claim sentence never says "contained" unless OS-enforced net + tree | `containment.claim` in manifest | "sandboxed" in prose |
| G | Verifier outage leaves target byte-for-byte unchanged, no success commit | CI step "Forced verifier-outage regression"; `DirtyTreeError` path | `[unverified]` keep |
| H | WIP orphan ref, never in commits, publish refused until proven, restored with fingerprint | `wip_snapshot_ref`, `wip_restore`, `refs/flexfactor-wip/*` | committing WIP as the first sandbox commit |
| I | Snapshot secret-scanned before publish | `wip_secret_findings`, `publish_allowed` refusal | scanning only the diff |
| J | Push/merge only on build + strongest suite green; never force | `commit_status` "pushed"/"PUSH REFUSED - ..."; argv has no `--force*` | "Final build gate: passed" with a red suite |
| K | Final review covers the COMPLETE patch in chunks; mismatch/moved HEAD blocks | `review_ledger.complete`, `chunk_count`, `patch_truncated: false`, `head_matches` | 180k-char truncation |
| L | Large files chunk-indexed, never "too-large" skipped | record `status: analyzed-in-chunks`, `chunk_ledger_complete` | `too-large-for-structural-parser` |
| M | DIRECT function coverage or blocked-with-reason; basis labelled | `coverage-ledger.json` `direct_gate`, `function_coverage_basis` | `module-executed` counted as proven |
| N | Journeys per role/viewport; unexercised items named; destructive only isolated | `journeys`, `authorization_matrix`, `incomplete_reasons`, `isolated` | `complete: true` with skipped forms |
| O | Manifest records the new keys | `*_run_manifest_*.json` | console text only |
| P | Owner vocabulary only; critical unknown blocks; phrases tripwired | `release_status`, `release_status_unmet` | "ready except for" |
| Q | Exit codes carry the truth; ledger identity holds | exit 3 on zero work; `review_ledger` balanced | exit 0 on a 6-hour no-op |
| R | Clean wheel outside the checkout == source runtime | CI `package-artifact` job; `--runtime-manifest` parity | importable in the checkout only |
| S | Windows AND Linux containment evidence, or a named blocker packet | CI matrix probe output on both OSes + this doc's BLOCKED list | Windows-only without saying so |
| T | Every doc/report claim VERIFIED or labelled UNKNOWN/BLOCKED | this file's section 7 | docs-only claims |

## 7. Not implemented / BLOCKED (as of this working tree)

- Windows OS network isolation (AppContainer) - not built; Windows network is best-effort proxy poisoning (`capability_report()`).
- Linux `bwrap` / `unshare` / `rlimit` paths - written, 2 sandbox tests skip on this Windows host; CI ubuntu job only RECORDS the probe ("never a pass by itself").
- `--trust-repo` is accepted by audit, prodready and scout (help output verified); persistent trust is `FLEXFACTOR_TRUSTED_REPOS` or `~/.flexfactor/policy.json` `trusted_repos` (`flexfactor policy init` writes the key).
- Criterion K cannot pass on the audit path: `head_matches` is called with `_git` and builds its own `["git", ...]` argv, so the race guard always revokes (failing test `test_head_moving_after_review_revokes_the_approval`: "git: 'git' is not a git command"). Fail-closed, but the gate is unreachable.
- Criterion S: no Linux evidence on this host.
- Dashboard (`flexfactor_dashboard.py`) has no chunk-ledger / direct-coverage / journey panels (grep: zero references).
- Scout mutation path does not use the orphan-WIP transaction (byte backups only).
- Direct-coverage `blocked` reasons cannot be declared from the audit path (`_direct_coverage_evidence` returns `blocked: {}`), so criterion M completes only at 100% direct coverage.
- Version is 0.5.0 (`pyproject.toml`, `TOOL_VERSION`, CI parity assertion).
- Main unit suite on this tree: 887 collected, **9 failures**, 8 skipped (names in CURRENT_STATE_GAP.md). Several are source-fragment greps whose windows moved under concurrent edits; at least one (`head_matches`) is a real defect.
