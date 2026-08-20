# FlexFactor — Project Brief

## Purpose

FlexFactor is a trustworthy local code auditor and refactorer. It reads why a
program exists (via a purpose contract), reviews every source file against that
purpose, applies verified fixes, and publishes only when a green build and test
suite confirm the work.

It never retains unverified changes, never leaks sensitive source outside the
machine, fails closed on verifier loss, contains untrusted installs/builds,
produces reproducible evidence, and offers deterministic rollback.

## Algorithm Methodology

### Review–Fix–Gate Cycle

1. **Purpose baseline** — load the program's `purpose-contract` and score all
   acceptance criteria before touching any file.
2. **Parallel review** — each source file is reviewed concurrently by a judge
   LLM against the purpose contract, producing a findings list with severities.
3. **Edit-block fix generation** — fixes are expressed as search/replace edit
   blocks (output scales with the change, not the file); a shrinking loop retries
   with fewer findings when the model output budget is exceeded.
4. **Adversarial verification** — a second model reviews every candidate fix
   assuming it is wrong; the loop re-iterates on material residuals up to
   `--adversarial-rounds` (default 2) before rollback.
5. **Publication gate** — build gate first, then the strongest test suite the
   project exposes; a defined-but-red suite is a hard publication failure.
6. **Commit and sync** — verified commits are pushed with a plain
   fast-forward `git push`; **nothing is ever force-pushed** (`--force-with-lease`
   described the deleted sandbox topology and `test_push_is_never_forced` pins
   its absence). A protected main that rejects the direct push falls back to
   publishing `flexfactor/land-<sha8>` and opening a PR with auto-merge.

### Scheduling and Resource Allocation

- **Free-vs-paid failover** — a two-phase stream deadline (first-event budget
  absorbs queuing; per-event idle timer) routes stalled calls to the paid rescue
  path, rate-capped at 40 rescues/hour.
- **Concurrent free-review pool** — every available free backend fills a shared
  file queue; a fast backend naturally pulls more files with no hardcoded ratio.
- **Fix prefetch pipeline** — `--fix-prefetch N` (default 3) runs first-attempt
  fix generations in background threads while the main thread applies and gates
  the current file.

### Constraint Handling

| Constraint | Enforcement |
|---|---|
| No review-only mode | `--report-only`/`--dry-run` removed; argparse exits 2 |
| Apply-nothing exit code | `EXIT_APPLIED_NOTHING = 3` so supervisors see the failure |
| Push requires green gate | `final_ok is True` guards both push and merge |
| Budget cap | `--max-cost` hard-stops per-program spend |
| Egress gate | High-confidence secrets/PII refused before any cloud call |
| Command policy | Destructive/credentialed/deploy commands refused (rc 126) |
| Version-aware review | Findings recommending removed APIs are dropped, and the drop is printed (`[version] <file>: dropped finding ...`) |

### Scenario Analysis

- **Purpose-gap fixing** — owner-authored unmet acceptance criteria bypass
  `--fix-severity` and receive up to 12 fix attempts each.
- **Competitor research** — Phase 1b scrapes corroborated competitors, applies
  the licence gate, and bridges accepted ideas into the fix queue (capped by
  `--competitor-fixes`, default 5).
- **Production-readiness rubric** — 13 deterministic gates (no model calls)
  including structured-data validity, dependency pinning, and test-suite presence.

## Supported Constraints

- `--fix-severity` — minimum severity level that triggers a fix attempt
- `--max-cost` — maximum USD budget per program (default $150)
- `--adversarial-rounds` — re-fix rounds before reject (default 2)
- `--fix-prefetch` — parallel first-attempt generations (default 3)
- `--competitor-fixes` — max bridged competitor findings per run (default 5)
- `--no-bootstrap` — skip dependency installation before build gate
- `--no-push` / `--no-merge` — opt out of automatic publication
- `--economy` — use cheaper author tier (claude-sonnet-5) across all modes

## Use Cases

1. **Audit a codebase** — `python flexfactor.py audit --program <path>`  
   Finds and fixes defects; publishes verified result to the branch.

2. **Production-readiness check** — `python flexfactor.py prodready --program <path>`  
   Installs deps, runs 13 rubric gates, fixes medium-and-above findings.

3. **Scout an integration** — `python flexfactor.py scout --program <path>`  
   Proposes third-party integrations; requires explicit `--apply` approval.

4. **Single-file refactor** — `python flexfactor.py --file <f> --goal "..."`  
   Targeted rewrite of one file toward a stated goal.

## Optimization Approach

- Edit blocks over whole-file regeneration — output proportional to change size.
- Shrink-and-retry on budget overrun — worst-severity half retried, never skipped.
- LRU brain cache — `clean_files` skip set avoids re-reviewing unchanged files.
- Resume checkpoints — per-file delta flushes survive crashes; re-verifies hashes
  on recovery so stale entries are dropped.
- Two-phase stream deadline — long-but-progressing generations are never killed;
  only idle streams time out.

## Running Tests

```bash
pip install -r requirements.txt
python flexfactor_tests.py          # unit tests, no API keys needed
python flexfactor_rotation_tests.py
python flexfactor_node_lock_tests.py
python flexfactor_prodready_persistence_tests.py
python flexfactor_dashboard.py --selftest
```
