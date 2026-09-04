# Purpose & Acceptance Contract — FlexFactor

**Version:** 0.3
**Application:** FlexFactor  
**Executor:** Cursor  
**Repository:** buckeye7066/flexfactor (default `main`)  
**Launcher:** `flexfactor_launch.ps1`

## Purpose

A trustworthy local expert tool that can audit or improve a codebase, never
retains unverified changes, never leaks sensitive source, never executes
untrusted dependencies outside containment, and always produces reproducible
evidence and deterministic rollback.

## Acceptance (master prompt)

1. Forced verifier outage restores the exact pre-run bytes, fails the run, and
   creates no `UNVERIFIED` commit or success score.
2. Source is classified before every cloud-bound model call; sensitive
   repositories default to local processing and exceptions require explicit
   owner approval.
3. Repository-supplied installs, builds, tests, and scripts run inside enforced
   resource, network, path, process, and time containment on Windows and Linux.
4. Batch and project budgets, immutable manifests, exact commands and evidence,
   and deterministic rollback are required. Report-only/apply-off is the
   default; mutation requires explicit apply authorization.
5. The full Windows and Linux suite covers verifier outage, dirty worktrees,
   cancellation, timeout, partial failure, and backward compatibility.
6. Audit and apply are unambiguous, and every modification is tied to its
   manifest, test evidence, budget, commit, and rollback path.
7. Artifact files never enter integration commits.

## Resolved authority conflict

An earlier checked-in revision required every Audit and Production Ready run to
apply changes and denied a report-only path. That contradicted the owner's
current baseline and bridge plan. The owner directive controls: report-only is
the default, apply must be explicit, and mandatory-mutation behavior remains a
gap until corrected.

## Forbidden substitutes

Fake success, retained `UNVERIFIED` output after outage, docs-only claims,
best-effort network poisoning described as containment, or one-platform proof
when Windows and Linux evidence is required.
