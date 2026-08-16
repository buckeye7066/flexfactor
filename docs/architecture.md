# FlexFactor execution architecture

FlexFactor is one fail-closed pipeline with four entry modes. `flexfactor.py` owns orchestration and every mutation; sibling modules provide deterministic policy, containment, purpose, readiness, resume, and evidence services. The Windows shortcut still launches `flexfactor_launch.ps1`, and launcher option 3 still maps to the real `audit` command.

## Audit lifecycle

1. Resolve and lock the target without following symlinks or junctions.
2. Recover a SHA-verified checkpoint and build the pre-change repository index.
3. Load authored purpose evidence or label the result inferred.
4. Select free/local or configured provider adapters; every payload crosses the egress and budget chokepoints.
5. Measure and repair purpose gaps, then run batched whole-source review and build-gated repair cycles.
6. Generate function tests, execute the repository suite, and drive live web routes and controls when applicable.
7. Re-index the resulting tree, rescan every changed file, compute reverse-dependency blast radius, and build file/function/workflow coverage ledgers.
8. Run normalized build, tests, secret, inventory, rescan, blast-radius, function-coverage, behavior, and exact-commit independent-review gates.
9. Emit the Markdown report, immutable run manifest, JSON evidence bundle, SARIF, screenshots, Playwright trace, and secret-redacted event stream.
10. A non-passing or unavailable gate revokes convergence and yields a non-zero process result.

## Persistent state

Machine-local state is under `~/.flexfactor/`: `brain.json` for bounded repository memory, `runs/` for resumable checkpoints, `events/` for observable JSONL events, and `evidence/<project>/<run>/` for immutable proof. Evidence is kept outside the audited repository so FlexFactor does not certify artifacts it injected into the target.

## Provider contract

Provider adapters implement completion, structured output, grading, and health checks. Ollama is local-only and refuses non-loopback endpoints. Cloud adapters cross the secret/PII gate. Free-first routing may use a loopback FCC endpoint; paid rescue is bounded, named, and metered. Audit and prodready expose `--model-mode auto|local|paid`: local removes paid-rescue credentials before provider construction, paid excludes loopback/Ollama routes, and unavailable requested modes fail rather than silently converting intent.

## Extensibility

`EventLedger` is the tool-hook boundary: in-process hooks receive before/after events but cannot turn a failing operation into success. The JSONL event shape contains trace/run identity, time, latency/cost attributes when provided, and redacted details; it can be translated to OpenTelemetry. External MCP/tool adapters remain outside the repository boundary and must call the same command, egress, containment, and evidence chokepoints.

## Trust boundary

Model output is advice until a contained write, native build/test execution, changed-file rescan, blast-radius analysis, and independent exact-commit review prove it. Missing tools, no collected tests, skipped material controls, an unavailable reviewer, or a commit mismatch are blockers—not passes.
