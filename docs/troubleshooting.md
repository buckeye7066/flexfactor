# Troubleshooting

## A writing mode refuses before calling a model

Refactor, Scout apply, Audit, and Production Ready require a Git repository with
an `origin`, a named branch, a resolvable remote default branch, and mandatory
push/merge. Configure the correct remote and retry. Local-only or
`--no-push`/`--no-merge` mutation is intentionally unsupported.

## The strongest model is unavailable

Do not choose another route. The single best-available ladder records quota,
credit, rate, or transport failure and continues through lower-paid capacity
before free/local capacity. If every route is unavailable, the run checkpoints
and reports blocked. Add or restore optional credentials, then rerun the same
request.

For a ChatGPT/Codex subscription, sign in with the official `codex` client and
refresh AI Time. AI Time records the concrete account-default model (for
example `gpt-5.6-sol`), not a guessed `codex` alias. FlexFactor uses an
account-bound OAuth transport when the local Codex auth file contains an access
token and account ID, then falls back to the bounded official CLI on older
installations. `FLEXFACTOR_CODEX_AUTH_FILE` may point to a nonstandard auth-file
location; it is never copied into a report or catalog.

ChatGPT Work Mode keeps its real credential in the parent service and exposes
only broker placeholders to child processes. A FlexFactor process launched
inside that managed session therefore refuses the Codex route during preflight
instead of starting a nested agent that cannot create a thread. Run FlexFactor
from the ordinary desktop shell for that subscription, or provide another live
AI Time route.

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
than dispatching twice. If GitHub accepted a request but has not exposed its run
yet, FlexFactor reports the request as pending and retries the same UUID. It
does not bypass or delete the atomic claim to force another dispatch.

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
