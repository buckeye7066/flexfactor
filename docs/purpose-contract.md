# Purpose & Acceptance Contract — FlexFactor

**Version:** 0.1  
**Application:** FlexFactor  
**Executor:** Cursor  
**Repository:** buckeye7066/flexfactor (default `main`)  
**Launcher:** `flexfactor_launch.ps1`

## Purpose

A trustworthy local code auditor/refactorer that never retains unverified changes, never leaks sensitive source, fails closed on verifier loss, contains untrusted installs/builds, produces reproducible evidence, and offers deterministic rollback.

## Acceptance (master prompt)

1. Forced verifier outage leaves target byte-for-byte unchanged and creates no success commit
2. Full Windows and Linux containment evidence
3. Artifact files never enter integration commits
4. One real report-only audit and one explicitly approved apply journey
5. Exact manifests, budgets, commands, tests, commits, and rollback

## Forbidden substitutes

Fake success, UNVERIFIED keep on outage, docs-only claims, Windows-only when Linux evidence is required without an external blocker packet.
