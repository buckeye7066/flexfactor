# Troubleshooting

## A writing mode refuses before calling a model

Refactor, Scout apply, Audit, and Production Ready require a Git repository with
an `origin`, a named branch, a resolvable remote default branch, and mandatory
push/merge. Configure the correct remote and retry. Local-only or
`--no-push`/`--no-merge` mutation is intentionally unsupported.

## The strongest model is unavailable

Do not choose another route. The single best-available ladder records quota,
credit, rate, or transport failure and continues through lower paid capacity
before free/local capacity. If every route is unavailable, the run checkpoints
and reports blocked. Add or restore optional credentials, then rerun the same
request.

## Independent review is unavailable

The reviewer must be from a family that authored none of the candidate.
Restoring capacity in a different model family is required; same-family review
cannot be used as a substitute.

## A run is quiet or interrupted

Inspect `~/.flexfactor/status.json`, `~/.flexfactor/queues/`, and the matching
`~/.flexfactor/runs/` checkpoint. Repeat the identical request or reopen the
Android app. SHA-matching work resumes; stale or changed entries rerun. The
orchestrator will not start the next queued target while one remains active.

## A green build still does not complete

Read the run's `quality-gates.json` and publication record. Common blockers
include a red strongest suite, zero tests collected, incomplete changed-file
rescan, missing direct behavior evidence, unexecuted UI controls, secret
findings, partial reviewer output, same-family review, moved HEAD, or the exact
SHA not yet merged to the remote default branch.

## A protected branch leaves a PR open

FlexFactor never bypasses checks or approvals. It waits for a normal merge up to
`FLEXFACTOR_PUBLISH_WAIT_SECONDS`; an unmerged PR remains incomplete. Satisfy
the branch rule and retry/resume so the final remote ancestry proof can run.

## Android sign-in or dispatch fails

Confirm the installed build is the current signed release, complete GitHub's
device flow, and ensure the selected repository is writable. A stale selected
ref is rejected before workflow or secret mutation. Reopening the app safely
reuses the stable request UUID; the cloud recovers an existing workflow rather
than dispatching twice.

## Android shows an incomplete result

Open the correlated run details and bounded error ledger. A GitHub Actions job
being green is not by itself publication proof: a changed run also needs its
reviewed SHA on the repository's authoritative default branch.

## Playwright behavior evidence is blocked

Install the target's declared browser dependency and provide a working local
start command. Destructive controls require a disposable environment with
`FLEXFACTOR_E2E_ISOLATED=1`. Give controls accessible names; skipped or
untargetable controls block completeness.

## Launcher does not open on Windows

Run `flexfactor_launch.ps1` directly in Windows PowerShell 5.1 to expose
parsing or interpreter errors. Set `FLEXFACTOR_PYTHON` when the checkout's
virtual environment is not the intended Python 3.12 installation.
