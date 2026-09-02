# FlexFactor - Architecture (0.5.0 working tree)

NOTE: on Windows `docs/ARCHITECTURE.md` and the pre-existing tracked
`docs/architecture.md` are the SAME file (case-insensitive FS); git tracks it as
`docs/architecture.md`. The original lifecycle narrative is preserved as
section 0; the module/chokepoint map follows. Everything below was read in the
working tree.

## 0. Original lifecycle narrative (pre-0.5.0, kept verbatim)


FlexFactor is one fail-closed pipeline with four entry modes. `flexfactor.py` owns orchestration and every mutation; sibling modules provide deterministic policy, containment, purpose, readiness, resume, and evidence services. The Windows shortcut still launches `flexfactor_launch.ps1`, and launcher option 3 still maps to the real `audit` command.

### Audit lifecycle

1. Resolve and lock the target without following symlinks or junctions.
2. Recover a SHA-verified checkpoint and build the pre-change repository index.
3. Load authored purpose evidence or label the result inferred.
4. Select free/local or configured provider adapters; every payload crosses the egress and budget chokepoints.
5. Load the authored purpose contract when available (otherwise label the purpose inferred), assess it before changes, research the top five competitors for purpose-serving ideas, prioritize purpose-critical files/gaps, run build-gated repair cycles, then reassess the final tree before claiming purpose progress.
6. Generate function tests, execute the repository suite, and drive live web routes and controls when applicable.
7. Re-index the resulting tree, rescan every changed file, compute reverse-dependency blast radius, and build file/function/workflow coverage ledgers.
8. Run normalized build, tests, secret, inventory, rescan, blast-radius, function-coverage, behavior, and exact-commit independent-review gates.
9. Emit the Markdown report, immutable run manifest, JSON evidence bundle, SARIF, screenshots, Playwright trace, and secret-redacted event stream.
10. A non-passing or unavailable gate revokes convergence and yields a non-zero process result.

### Persistent state

Machine-local state is under `~/.flexfactor/`: `brain.json` for bounded repository memory, `runs/` for resumable checkpoints, `events/` for observable JSONL events, and `evidence/<project>/<run>/` for immutable proof. Evidence is kept outside the audited repository so FlexFactor does not treat artifacts it injected into the target as independent proof.

### Provider contract

Provider adapters implement completion, structured output, grading, and health checks. Ollama is local-only and refuses non-loopback endpoints. Cloud adapters cross the secret/PII gate. Free-first routing may use a loopback FCC endpoint; paid rescue is bounded, named, and metered. Audit and prodready expose exactly two modes, `--model-mode free|paid` (owner order 2026-08-24). `free` is the default: it removes paid-rescue credentials before provider construction and EXCLUDES every billable route from the catalog, so the run cannot spend - a filter, not an ordering preference. `paid` is the owner's own Anthropic and OpenAI accounts only (metered keys, the Claude subscription, and the local `claude`/`codex` CLI lanes), and it excludes free tiers, Ollama, and reseller credits such as OpenRouter and Cursor. Unavailable requested modes fail rather than silently converting intent, and the retired `local`/`auto` spellings normalize to `free` with a warning rather than dying at argparse.

### Extensibility

`EventLedger` is the tool-hook boundary: in-process hooks receive before/after events but cannot turn a failing operation into success. The JSONL event shape contains trace/run identity, time, latency/cost attributes when provided, and redacted details; it can be translated to OpenTelemetry. External MCP/tool adapters remain outside the repository boundary and must call the same command, egress, containment, and evidence chokepoints.

### Trust boundary

Model output is advice until a contained write, native build/test execution, changed-file rescan, blast-radius analysis, and independent exact-commit review prove it. Missing tools, no collected tests, skipped material controls, an unavailable reviewer, or a commit mismatch are blockers—not passes.

## 1. Canonical runtime and entry paths

Every entry path ends in `flexfactor.run_cli(argv)`:

| Entry | Path |
|---|---|
| `python flexfactor.py ...` | `if __name__ == "__main__": raise SystemExit(run_cli())` |
| `python -m flexfactor ...` | same module guard |
| installed `flexfactor` console script | `[project.scripts] flexfactor = "flexfactor:run_cli"` |
| `flexfactor_run.py` | compatibility shim: `from flexfactor import run_cli` |
| `flexfactor_launch.ps1`, `flexfactor_audit_launch.ps1`, `flexfactor_scout_launch.ps1` | `Join-Path $PSScriptRoot "flexfactor_run.py"` |
| `--runtime-manifest` | handled inside `run_cli` before `main` |

`run_cli` arms the death-obituary instrumentation, calls `main(argv)`, marks the
finish (also on `SystemExit`). `main` builds the argparse tree (refactor default,
`scout`, `audit`, `prodready`, `policy`) and dispatches to `run_scout`,
`run_audit` (-> `audit_one_program` per program, `--parallel` threads),
`run_policy`, or the refactor loop. `flexfactor_entrypoint_tests.py` asserts
manifest parity across all of them and that launchers resolve the shim next to
themselves; CI installs the wheel into a venv outside the checkout and compares
manifests.

## 2. Module map (single-owner responsibilities)

| Module | Owns | Imports flexfactor? |
|---|---|---|
| `flexfactor.py` (16.5k lines) | orchestration, providers, review/fix loops, git publication, reports, manifest, argparse | - |
| `flexfactor_cmdpolicy.py` | command classification + high-risk refusal; policy.json `allow_classes` | no |
| `flexfactor_egress.py` | secret/PII scan of every repo-derived provider payload; `allow_egress` | no |
| `flexfactor_sandbox.py` | OS execution broker: `capability_report`, `prepare`, `run_contained`, `spawn_contained`, `require_containment_or_trust` | no |
| `flexfactor_trust.py` | trusted-repo decision (env / policy.json), frozen-install argv, `containment_claim` (stale wording) | no |
| `flexfactor_wip.py` | orphan WIP snapshot / restore / fingerprint / secret scan / `publish_allowed` | no (git runner injected) |
| `flexfactor_partial.py` | partial-output evidence, salvage, `refuse_clean_if_partial`, continuation merge | no |
| `flexfactor_ledger.py` | content-addressed chunks, `ReviewLedger`, `head_matches` | no |
| `flexfactor_coverage.py` | coverage artifact parsers, `direct_function_rows`, `direct_function_gate`, `merge_into_function_coverage` | no |
| `flexfactor_journeys.py` + `flexfactor_assets/flexfactor_explorer.js` | explorer path/env/result parsing/completeness; the Playwright engine | no |
| `flexfactor_evidence.py` | repository index (chunked large files), coverage ledger, quality gates, SARIF, evidence bundle, `EventLedger` | no |
| `flexfactor_purpose.py` | contracts (registry + in-repo), status vocabulary, `production_ready_status`, purpose evidence + confidence | no |
| `flexfactor_runstate.py` | durable per-run checkpoint + resume verification | no |
| `flexfactor_directed.py` | unfit-route patterns, skip-dir test, directed theme block | no |
| `flexfactor_rotation.py` | AI Time route catalog rotation | no |
| `flexfactor_competitors.py`, `flexfactor_prodready*.py`, `flexfactor_autoclean.py`, `flexfactor_locate.py`, `flexfactor_scout_contract.py` | competitor research, readiness rubric/toolchains, pre-work repo cleanup, program location, scout bridge contract | no (callers inject `_run`) |
| `flexfactor_dashboard*.py`, `flexfactor_web.py` | read-only viewers of `~/.flexfactor/status.json` | dashboard reads only |

## 3. Chokepoints

1. **Execution broker** - `_run(cmd, cwd, timeout, env)` (never raises):
   cmdpolicy gate -> if classes intersect `{install, build, test}` and not
   `_tool_authored_syntax_check` -> `_run_target_code` -> `_execution_authorization`
   (`flexfactor_trust.trust_decision` + `_RUN_TRUST_OVERRIDE`, then
   `flexfactor_sandbox.require_containment_or_trust`) -> `run_contained(Limits(
   timeout, network=("install" in classes)))`; every decision appended to
   `_EXECUTION_LEDGER`. `_spawn` mirrors it for servers (network on, no timeout,
   `kill_tree` attached). Plain path for git/read-only/unknown.
2. **Provider `structured()` + `_judge`** - `_check_structured_type` envelope
   salvage -> `_mark_partial` on salvage -> `_judge` routes to the judge tier
   and applies `refuse_clean_if_partial`. Egress gate sits inside every
   provider call on repo-derived payloads.
3. **Publication** - `_publication_gate` (build gate, then strongest suite) ->
   `_commit_and_sync`: local commit always; push only if `final_ok is True` and
   `_wip_publish_guard` allows; protected trunk -> `flexfactor/land-<sha8>` +
   `_gh_pr_automerge`; merge block guarded the same way; no force flags.
4. **Evidence bundle** - after the cycles: `build_repository_index` (chunked
   large files) -> rescan + blast radius -> `coverage_ledger` ->
   `_direct_coverage_evidence` overlay -> `secret_findings` -> `quality_gates`
   -> `_independent_final_review` (chunk ledger) -> `head_matches` ->
   `write_evidence_bundle` under `~/.flexfactor/evidence/<proj16>/<run>/` ->
   `_write_run_manifest` in the target dir.

## 4. What the monolith still owns

Providers and pricing; rotation hook; review batching/pool; fix generation
(edit blocks, shrink loop, adversarial verify); bootstrap phase; baseline
repair; cycle loop and accounting ledgers; git operations; report rendering;
run manifest; e2e orchestration (`_dev_server_command`, `_wait_http_ready`);
purpose gap assessment prompts; scout pipeline (`apply_integration` has its own
byte-backup rollback, not the WIP transaction); argparse for all modes.

## 5. Recommended extraction order (next)

1. `flexfactor_git.py`: `_git`, `_git_tree_clean`, `_commit_and_sync`,
   `_gh_pr_automerge` - the publication gate is the highest-consequence logic
   still inline, and `head_matches` needs a single git-runner contract (the
   current double-`git` argv bug is exactly this seam).
2. `flexfactor_report.py`: `_write_audit_report`, `_write_run_manifest`,
   `_release_status` - the manifest is the evidence contract; it should be
   versioned separately from the orchestrator.
3. `flexfactor_e2e.py`: `_dev_server_command`, `_wait_http_ready`,
   `_run_live_ui_exploration` - already depends only on `_spawn`/`_run` and
   `flexfactor_journeys`.
4. `flexfactor_fix.py`: edit-block generation/apply/shrink + adversarial loop.
5. Move `--trust-repo`/`--allow-dirty` help and parser fragments into one
   shared `add_common_exec_flags(parser)` so audit/prodready/scout cannot drift.

## 6. Managed mobile control plane

The Android product has three explicit layers:

| Layer | Owns | Must not own |
|---|---|---|
| Android APK | Four-mode UI, confirmations, Android-Keystore session storage, repository-key sealed boxes, run history, signed updates | GitHub REST orchestration, workflow installation, arbitrary toolchain execution |
| FlexFactor Cloud (`cloud/`) | Device OAuth and refresh grants, repository discovery, exact caller installation, dispatch correlation, status, bounded artifact proxy, steering | Persistent bearer/provider-key storage, target-code execution, generic upstream proxying |
| Selected repository runner | Exact tagged engine, target checkout, Python/Node/browser/build toolchains, verification, publication, result/error artifacts | Owner OAuth token, mobile UI state |

The APK's only product API origin is the fixed production FlexFactor Cloud URL. The cloud service
uses fixed upstream origins and validates the complete run request again. GitHub Actions is the
ephemeral compute substrate behind the service, not the interface presented as the finished mobile
product. `cloud/THREAT_MODEL.md` records the credential, artifact, mutation, and execution boundaries.
