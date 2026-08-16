# Troubleshooting

## Launcher does not open

The shortcut must target Windows PowerShell with `-ExecutionPolicy Bypass -NoProfile -File "C:\Users\firer\flexfactor\flexfactor_launch.ps1"` and use `C:\Users\firer\flexfactor` as its working directory. Run the PowerShell file directly to expose parsing or Python-path errors. Keep the launcher ASCII for Windows PowerShell 5.1.

## Free/local mode cannot start

The desktop launcher expects the FCC health endpoint at `http://127.0.0.1:8082/health` and attempts to start `fcc-server`. An explicit Ollama run expects a loopback `OLLAMA_BASE_URL`. Start the selected local service and retry the same command; the checkpoint reuses SHA-matching reviews. FlexFactor does not silently substitute a paid provider for explicit local-only mode.

## Paid mode reports missing credentials

Set the provider credential in the process environment and retry. A paid request with no credential is a hard, explicit error. Never place credentials in the repository, command output, or report.

## A run is quiet or interrupted

Inspect `~/.flexfactor/status.json`, the dashboard, and `~/.flexfactor/runs/<run>/checkpoint.json`. Re-run the identical command. Reviews are resumed only when the exact file SHA and policy version still match; changed entries are re-run. A stale audit lock is reclaimed only when its recorded process is dead.

## A green suite still does not complete

Open `~/.flexfactor/evidence/<project>/<run>/quality-gates.json`. Typical blockers are zero tests collected, incomplete function evidence, skipped destructive/unnamed controls, accessibility or performance smoke failures, an incomplete changed-file rescan, or an unavailable exact-commit reviewer. Correct the named cause; do not delete or suppress the gate.

## Playwright behavior evidence is blocked

Install the target's declared Playwright dependency and ensure its dev/start command answers the configured loopback URL. Destructive controls require a disposable target with `FLEXFACTOR_E2E_ISOLATED=1`. Unnamed controls fail low-confidence targeting; add an accessible role/name instead of forcing an index click. Trace and screenshots are under the run's UI artifact directory.

## Push or merge is refused

FlexFactor never force-pushes. Confirm the working tree is clean, integrate the current remote branch, rerun the final gates, and publish the exact tested tree. Protected branches use a pull request. A red project suite, unverified build, or failed publication check blocks the push/merge claim.
