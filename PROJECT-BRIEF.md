# FlexFactor — Project Brief

## Purpose

FlexFactor is a managed code auditor and refactorer. It reads why a
program exists (via a purpose contract), reviews every source file against that
purpose, applies verified fixes, and publishes only when a green build and test
suite confirm the work.

It never retains unverified changes, never leaks sensitive source outside the
machine, fails closed on verifier loss, produces reproducible evidence, and
offers deterministic rollback. Target-controlled code (dependency install,
build, test, dev server) runs only through the execution broker
(`flexfactor_sandbox`): OS-enforced containment where the host provides it
(Linux bwrap/unshare + rlimits; Windows Job Objects for process-tree, memory,
process-count and CPU time), otherwise ONLY for repositories the owner has
explicitly trusted. Windows has no OS network isolation today; that limit is
recorded in every run manifest rather than described as containment
(docs/EXECUTION_CONTAINMENT.md).

## Algorithm Methodology

### Review–Fix–Gate Cycle

1. **Governed repository preparation** — resolve one queued target, account its
   complete Git-visible manifest, establish purpose, prepare dependencies, and
   measure the publication baseline under one durable target receipt.
2. **Purpose-first repair** — repair a red baseline and bridge authorized
   purpose gaps inside the first whole-repository pass, then reconcile its
   manifest before semantic review begins.
3. **Semantic review** — review every eligible file against the purpose
   contract. Worker concurrency may accelerate this one active target; it never
   admits another repository concurrently.
4. **Edit-block fix generation** — fixes are expressed as search/replace edit
   blocks (output scales with the change, not the file); a shrinking loop retries
   with fewer findings when the model output budget is exceeded.
5. **Adversarial verification** — a second model reviews every candidate fix
   assuming it is wrong; the loop re-iterates on material residuals up to
   `--adversarial-rounds` (default 2) before rollback.
6. **Exact-delta follow-up** — later passes review only verified byte changes
   from the preceding pass; the top-three competitor gate runs between passes
   one and two.
7. **Publication gate** — build gate first, then the strongest test suite the
   project exposes; a defined-but-red suite is a hard publication failure.
8. **Commit and sync** — verified commits are pushed with a plain
   fast-forward `git push`; **nothing is ever force-pushed** (`--force-with-lease`
   described the deleted sandbox topology and `test_push_is_never_forced` pins
   its absence). A protected main that rejects the direct push falls back to
   publishing `flexfactor/land-<sha8>` and opening a PR with auto-merge.

### Scheduling and Resource Allocation

- **One quality-first ladder** — every call starts with the strongest available
  paid or subscription route and descends to free/local capacity only after
  stronger capacity is unavailable.
- **Sequential repository queue** — 1–30 selected targets run one at a time and
  resume from an atomically persisted receipt after interruption.
- **Concurrent review workers** — workers may share a file queue within the one
  active repository; this does not create a competing target scheduler.
- **Fix prefetch pipeline** — `--fix-prefetch N` (default 3) runs first-attempt
  fix generations in background threads while the main thread applies and gates
  the current file.

### Constraint Handling

| Constraint | Enforcement |
|---|---|
| No review-only mode | `--report-only`/`--dry-run` removed; argparse exits 2 |
| Apply-nothing exit code | `EXIT_APPLIED_NOTHING = 3` so supervisors see the failure |
| Publication is mandatory | writing modes require Git, origin, push, and merge |
| Push requires green gate | exact-commit evidence guards both push and merge |
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
- `--no-bootstrap` — advanced compatibility switch; verification remains
  fail-closed if dependencies are unavailable

Legacy provider, economy, model-mode, parallel, and publication opt-out flags
may still parse for saved-command compatibility, but they cannot select a
different production execution policy.

## Use Cases

1. **Audit a codebase** — `python flexfactor.py audit --program <path>`  
   Finds and fixes defects; exit 0 requires the exact verified commit on the
   authoritative remote default branch.

2. **Production-readiness check** — `python flexfactor.py prodready --program <path>`  
   Installs deps, runs 13 rubric gates, fixes medium-and-above findings.

3. **Scout an integration** — `python flexfactor.py scout --program <path>`  
   Researches source-backed improvements. Mutation requires explicit Scout
   authorization and then the same verification/publication contract.

4. **Single-file refactor** — `python flexfactor.py --file <f> --goal "..."`  
   Targeted rewrite of one file toward a stated goal.

## Optimization Approach

- Edit blocks over whole-file regeneration — output proportional to change size.
- Shrink-and-retry on budget overrun — worst-severity half retried, never skipped.
- LRU brain cache — prior hashes provide provenance and recovery evidence;
  exhaustive Audit/Production Ready pass one still reviews the current complete
  repository manifest.
- Resume checkpoints — per-file delta flushes survive crashes; re-verifies hashes
  on recovery so stale entries are dropped.
- Two-phase stream deadline — long-but-progressing generations are never killed;
  only idle streams time out.

## Running Tests

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[all]"
.venv/bin/python -m pytest -q
```

The binding Ubuntu/Windows matrix in
`.github/workflows/production-readiness.yml` also verifies a clean wheel,
entry-point parity, the cloud service, and live release-policy gates.
