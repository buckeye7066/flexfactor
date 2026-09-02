# FlexFactor architecture

This document describes the 0.7.0 engine, Android 3.6.0 client, and 1.2.0
managed control plane.

## Execution topology

| Layer | Owns | Must not own |
|---|---|---|
| Desktop/CLI | Four-mode request entry, local evidence display | A second model policy or unverified success |
| Android APK | OAuth UI, encrypted sessions/keys, 30-target durable queue, run history, signed updates | Target toolchains, generic GitHub access, plaintext cloud secrets |
| FlexFactor Cloud | Device OAuth exchange/refresh, bounded repository discovery, caller installation, idempotent dispatch, status/artifact/steering proxy | Persistent token storage, target execution, provider plaintext |
| Sequential orchestrator | Target admission, pass ordering, exact delta scopes, top-three competitor boundary, durable receipts | Model/provider choice by workers |
| Ephemeral runner | Exact tagged engine, target checkout, builds/tests/browser tooling, evidence, publication | Owner OAuth session or mobile UI state |
| GitHub default branch | Authoritative landing proof for the reviewed SHA | Acceptance of unreviewed or red candidates |

Every process entry ends in `flexfactor.run_cli(argv)`. The installed
`flexfactor` command, `python -m flexfactor`, `flexfactor_run.py`, and the
PowerShell launchers all use that same dispatcher.

## Scout boundary

Scout accepts an explicit target plus one or more public program/product
website URLs. It retrieves bounded same-site evidence, builds separate cited
profiles for target and source, and requires one adopt/adapt/reject/investigate
decision for every observed source capability. Only an accepted, target-specific
implementation query crosses into Repo Rewards. Repository discovery, scoring,
and candidate metadata belong to Repo Rewards; Scout does not accept a source
repository URL as a substitute for program research.

## Orchestrator contract

`flexfactor_execution.SequentialOrchestrator` is the control authority:

1. validate an ordered queue of 1–30 targets;
2. persist every state transition atomically;
3. admit only `next_index`, with at most one active target;
4. require pass 1 to be the complete repository;
5. require the top-three competitor gate before pass 2;
6. require each later scope to equal the preceding verified edit delta;
7. reject pass 7 or any skipped/overlapping transition;
8. recover an interrupted target as a fresh attempt without losing its receipt.

Android mirrors target-level admission in `MobileRunQueue`; its stable request
UUID is also the cloud/GitHub idempotency key. Repository-level pass authority
remains in the engine so desktop and mobile cannot drift.

## One model ladder

`flexfactor_rotation` owns selection. Best-available calls walk:

1. frontier paid/subscription capacity;
2. strong paid/subscription capacity;
3. light paid/subscription capacity;
4. frontier, strong, then light free/local capacity.

Quota, credit, and retryable capacity failures cool the real account allowance
and continue down that same ladder. Legacy provider/model-mode/economy flags
cannot select another production path.

A run-scoped `RoleCoordinator` records every model family that authored any
candidate content. Reviewer calls strictly exclude that full set. If no
independent family is available, review fails; same-family review is never
represented as independent.

## Repository lifecycle

Audit and Production Ready follow this order:

1. resolve, contain, and lock the target;
2. require Git, `origin`, a named branch, push/merge enabled, a resolvable
   remote default branch, and an exact baseline SHA;
3. snapshot owner WIP to an orphan ref when explicitly allowed;
4. create baseline inventory, purpose, code index, and executable gate status;
5. run semantic pass 1 across the entire repository;
6. commit only build/suite-verified checkpoints locally;
7. research and attempt the top three corroborated, purpose-compatible,
   licence-safe competitor capabilities;
8. run up to five exact-delta follow-up passes;
9. execute native tests, coverage, browser journeys, rescan, blast-radius,
   secret, purpose, and readiness gates;
10. review the complete exact-commit patch in content-addressed chunks through
    an independent model family;
11. rerun the publication gate and land the SHA directly or through a normal
    protected-branch PR;
12. fetch the remote default branch and prove the reviewed SHA is its ancestor.

Intermediate checkpoints never push. A local commit or open PR is recoverable
work, not completion.

## Main chokepoints

| Chokepoint | Guarantee |
|---|---|
| `_run` / `_spawn` | Command policy, target-code trust/containment, bounded execution ledger |
| Contained read/write helpers | No symlink traversal outside the target; atomic replacement |
| Provider adapters | Budget guard and secret/PII egress scan |
| `_judge` | Typed output; partial/salvaged responses cannot authorize clean/approve |
| `_commit_and_sync` | Local checkpoint only after real project verification |
| `_independent_final_review` | Complete chunk ledger bound to baseline and candidate SHAs |
| `_publish_verified_head` | Fresh project gate, clean/WIP guard, no force push, exact remote-default proof |
| Evidence writer | Immutable index, coverage, gate, SARIF, review, and publication receipts |

## Persistent state

Machine-local state is under `~/.flexfactor/`: `queues/`, `runs/`,
`evidence/`, `events/`, `brain.json`, and `status.json`. Target
repositories contain only intentional product/test changes and bounded run
manifests. Evidence generated by FlexFactor is not treated as independent
target evidence.

## Managed mobile request

The cloud validates the request, resolves the selected ref, preflights sealed
credentials, searches at most GitHub's 1,000 filtered workflow runs for the
request UUID, and atomically creates a repository-variable claim before it
installs the exact pinned caller, writes sealed phone credentials, and
dispatches. If history cannot be proven absent or the UUID is already claimed,
it refuses rather than risk a duplicate. Owner-managed provider secrets are
never overwritten; phone-supplied secrets and the request claim are deleted at
terminal status, and cleanup failure blocks queue advancement.

The reusable workflow checks out Android's exact release tag and the requested
target. Its final result is successful only when the engine exits 0 and any
changed SHA is proven on the authoritative remote default branch.

## Residual host limits

Linux uses the strongest available `bwrap`/namespace/rlimit mechanism.
Windows provides process-tree and resource controls, but network isolation is
best-effort proxy poisoning. The capability report says so explicitly. A host
that cannot contain untrusted target execution requires explicit repository
trust or refuses the command.
