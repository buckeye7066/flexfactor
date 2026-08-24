# FlexFactor - Current state vs. purpose (gap register)

## 0. UPDATE 2026-08-23 (supersedes the counts below; the analysis still stands)

Everything in sections 1-5 was written against HEAD `9875420` on 2026-08-22.
Re-measured today at `a03ee7c` + this change. What moved:

| Then (2026-08-22) | Now (2026-08-23, measured) |
|---|---|
| main suite 887 collected, **9 FAIL** | `flexfactor_tests.py` **888 run, 0 failures**, 8 skipped |
| module suites run individually | **29 test modules, all green - 1,395 tests, 0 failures** (counted from the run, not estimated): the CI list plus errors / structural / openai-param / intent / paid-first / quality / localbench / glimmer / config-surface / ledger-routing / dashboard |
| CI status not recorded here | `production-readiness` and `rotation-extensions` **green on main** (run 32668261872, 2026-08-23) |
| section 5 item 9 "9 main-suite failures" | **CLOSED** |
| section 5 item 3 "dashboard has no panels for the newer evidence" | **still open for chunk/coverage/journey panels**; the dashboard now DOES carry the run's error ledger, per program |

Closed today, each with the evidence that closed it:

1. **`flexfactor_errors` was never in the wheel.** It is imported at runtime by
   `_start_error_ledger`, so an installed FlexFactor printed
   `[errors] ledger unavailable: No module named 'flexfactor_errors'` and
   recorded nothing - the error ledger did not exist outside a source checkout.
   Added to `py-modules`; `test_the_error_ledger_loads_from_the_wheel` imports
   it from a fresh venv outside the checkout, and
   `test_every_module_the_runtime_imports_is_in_the_wheel` pins the CLASS (the
   CI import step could not: it imports the list, so the list was its own
   oracle). Verified by removing the entry again - the test reddens.
2. **The error ledger was process-global under `--parallel`.** Several programs
   audit in one process; the last to open a ledger owned every other program's
   errors. Now a ContextVar + `_CtxThreadPoolExecutor`
   (`flexfactor_ledger_routing_tests.py`, including a control test that shows a
   stock executor mis-filing).
3. **`gitignore_protects` (high) FAILED on FlexFactor itself** - `.env` was not
   ignored in a repo whose every credential comes from the environment. Fixed,
   asserted through `git check-ignore`, not by grepping `.gitignore`.
4. **`config_documented` (medium) FAILED** - no `.env.example`. Written, and
   kept honest in both directions by `flexfactor_config_surface_tests.py`
   (it found 8 runtime variables that had never been documented anywhere).
5. **`license_present` (low) FAILED** - no LICENSE file, while `pyproject.toml`
   had declared `Proprietary` all along. Written to match.

FlexFactor's own readiness rubric, run on FlexFactor with real build evidence
(`python -m compileall -q .` exit 0) and the 29-module battery as the test
evidence: **11 of 11 evaluated gates PASS, 2 N/A, no blockers** (was 6 pass /
9 evaluated with 3 blockers this morning).

Not closed, and deliberately named rather than quietly dropped:

- `pytest` collects only `flexfactor_tests.py` (`python_files` in
  `pyproject.toml`). That is a real choice - the modules share process state -
  but it means the *detected* test command (`python -m pytest -q`, 880 passed)
  exercises a fraction of the battery CI runs. The binding gate is the workflow,
  not pytest.
- Windows network isolation is still `best-effort-env` (section 5 item 1).
- Sections 5.2 (Linux bwrap unrun here), 5.4 (resume hash-verification scope),
  5.5 (scout mutation path outside the orphan-WIP transaction) and 5.7 (purpose
  `gh` runner outside the broker) are unchanged and still open.


Tree: HEAD `9875420` (feat: one canonical packaged runtime) + uncommitted
changes to flexfactor.py, flexfactor_cmdpolicy.py, flexfactor_evidence.py,
flexfactor_partial.py, flexfactor_purpose.py, flexfactor_tests.py,
flexfactor_wip.py, pyproject.toml, .github/workflows/production-readiness.yml,
plus untracked flexfactor_sandbox.py, flexfactor_ledger.py,
flexfactor_coverage.py, flexfactor_journeys.py, flexfactor_assets/,
test_flexfactor_{sandbox,wip,partial,ledger,coverage,journeys,purpose}.py,
eval_fixtures/{coverage,journeys}/. Baseline for "AS OF BASELINE" = `736bb2a`
(v0.4.1 at baseline; the working tree is now 0.5.0), facts verified by the lead engineer this session.

## 1. Verified current behaviour (read in code; tests run by me on 2026-08-22)

- `python flexfactor.py --runtime-manifest` reports `tool_version 0.5.0` (bumped after this doc was first written) and
  `wired: {command_policy, directed, egress, execution_broker, partial_output,
  trust_gate, wip_snapshot} = true` (all seven).
- `_run` classifies every command (`flexfactor_cmdpolicy`), refuses high-risk
  classes rc 126, and routes `install|build|test` classes through
  `_run_target_code` -> `flexfactor_sandbox.run_contained`; `_spawn` does the
  same for dev servers. `_tool_authored_syntax_check` is the only carve-out
  (`python -c`, `node -e/--check`, `-m py_compile|compileall|ast|tokenize`).
- cmdpolicy now classifies pip/poetry/uv/conda, cargo/go/dotnet/mvn/gradle/
  make/cmake/ninja/msbuild/swift/mix/bundle/rake/composer, mocha/playwright/
  cypress (uncommitted diff) - these previously fell through to `unknown` and
  bypassed the broker.
- Audit/prodready `--allow-dirty`: `capture_orphan_wip_snapshot` -> orphan
  commit under `refs/flexfactor-wip/<sha12>`, secret scan, `reset --hard HEAD`
  + individual unlink of captured untracked paths; `_wip_publish_guard` fronts
  both push sites in `_commit_and_sync`; `_restore_wip_if_active` runs in the
  `finally` of `audit_one_program` and drops the ref only on fingerprint match.
- Partial output: every provider salvage passes `_mark_partial`; `_judge`
  applies `refuse_clean_if_partial`; `review_file` raises `PartialOutputError`
  when a salvaged review has zero findings; `_independent_final_review` marks a
  partial chunk `blocked`.
- Final review: full patch -> `chunk_patch(max 60k chars)` -> one `_judge` per
  chunk -> `ReviewLedger.verdict_allowed()`; `head_matches` afterwards.
- Evidence: `_index_large_file_in_chunks` replaces the old too-large label
  (hard cap 64 MiB -> `blocked`); `_direct_coverage_evidence` runs the suite
  under a grounded coverage tool and overlays rows via
  `merge_into_function_coverage`; `quality_gates` `function-coverage` gate
  passes only on `direct_gate.complete`.
- Journeys: `_run_live_ui_exploration` spawns the dev server through `_spawn`,
  runs `flexfactor_assets/flexfactor_explorer.js` via `_run`, reads
  `FLEXFACTOR_E2E_ROLES/VIEWPORTS/MAX_PAGES/ISOLATED`, folds
  `journeys.completeness()` into `incomplete_reasons` and `ok`.
- Purpose: `_gather_from_folder` calls `gather_purpose_evidence(git_runner=_git)`
  and caches it; `_purpose_confidence_for` derives
  `purpose_confidence` / `purpose_mutation_authorized`; gap-bridging cap is
  forced to 0 when not authorized (reported, not bridged).
- Module test suites (run by me): sandbox 20 OK (2 skipped), wip 19 OK,
  partial 27 OK, ledger 26 OK, coverage 32 OK, journeys 17 OK (36 s, includes
  real Playwright runs), purpose 28 OK, trust 6 OK.
- Main suite `flexfactor_tests.py`: **887 collected, 9 FAIL, 8 skipped**
  (98.7 s). Failing: `EmptyPackageJsonDistinctTests.test_empty_present_missing_distinct`
  (purpose-evidence block now mentions package.json),
  `EvidenceRuntimeTests.test_live_explorer_records_per_item_a11y_performance_and_trace_evidence`
  (expects `context.tracing.start` in the explorer source),
  `LargeFileChunkLedgerTests.test_file_above_cap_gets_a_complete_chunk_ledger_with_absolute_lines`
  (0 != 1), `LargePatchChunkedFinalReviewTests.test_head_moving_after_review_revokes_the_approval`
  (`git git rev-parse` - real defect, FIXED: `head_matches` now receives `_git_argv`), `OrphanWipWiringTests.test_audit_pipeline_snapshots_dirty_tree_and_restores_it` (FIXED: process-global `_LAST_FREE_REVIEW_POOL` leaked between tests),
  `ReviewIncompleteHonestyTests.*` (2, source-fragment greps), `VacuousGateTests.*`
  (2, source-fragment greps). flexfactor.py was being edited concurrently while
  I read it (line numbers shifted ~30 between reads); re-run before trusting
  this list either way.

## 2. Contradictions found by reading

| # | Where | Says | Reality in code |
|---|---|---|---|
| 1 | `flexfactor audit/prodready --help` `--allow-dirty` | "pre-existing changes get swept into the cycle commits" | orphan snapshot + restore (`audit_one_program`) - **FIXED**: help text on every parser now describes the orphan snapshot |
| 2 | `flexfactor scout --help` `--allow-dirty` | "snapshotted to an orphan ref ... restored byte-for-byte" | `run_scout`/`apply_integration` never call `capture_orphan_wip_snapshot`; scout only skips-or-proceeds |
| 3 | `--trust-repo` | help says run-level authorization | defined only on the scout parser; `audit_one_program` reads `args.trust_repo` which audit/prodready argparse never sets (`audit --help` has 0 matches) |
| 4 | `flexfactor_trust.trust_decision` reason text | "pass --allow-untrusted-exec" | no parser defines that flag - **FIXED**: text now names `--trust-repo`, which audit/prodready/scout all accept |
| 5 | `flexfactor_trust.containment_claim()` | "FlexFactor does not provide an OS sandbox" | `flexfactor_sandbox.capability_report()["claim"]` reports Job Object / bwrap; only the sandbox sentence reaches the manifest |
| 6 | `pyproject.toml`, `TOOL_VERSION`, CI `assert inst["tool_version"] == "0.5.0"` | 0.5.0 | **FIXED**: bumped together with the CI parity assertion |
| 7 | README "scout ... on a `flexfactor/adopt-<repo>` branch" | sandbox branch | CLI: `--branch-prefix` "ACCEPTED BUT INERT" |
| 8 | README "Dual-provider (Anthropic + OpenAI)" | two fixed providers | pool-first rotation is the default provider when AI Time's catalog is usable |
| 9 | `flexfactor_launch.ps1` line ~117 comment | "flexfactor_run.py installs directed orchestration" | `flexfactor_run.py` is a shim; directed is a hard import in flexfactor.py |
| 10 | `audit_one_program` comment above the dirty-tree gate | describes `--snapshot-dirty` and a sandbox branch first commit | both removed; orphan ref is the mechanism |
| 11 | `flexfactor_wip.DIRTY_SNAPSHOT_MSG` | "never an ancestor of the sandbox branch" | there is no sandbox branch; it is the owner's branch |
| 12 | `docs/evidence/report-only-audit-report.md` | artifact of a `report-only` run on branch `flexfactor/audit-...` | both the mode and the branch topology no longer exist; stale evidence in the repo |
| 13 | `docs/purpose-contract.schema.json` vs `flexfactor_purpose._contract_from_registry` | schema `additionalProperties: false` (no `slug`, `aliases`, `false_substitutes`, `required_design`) | loader reads those keys; a schema-valid `.flexfactor-purpose.json` therefore cannot carry `false_substitutes` (loader returned 0 for this repo's file) |
| 14 | PROJECT-BRIEF "Running Tests" list | 5 test files | CI runs 13 + 4 new module suites; brief omits `test_flexfactor_*` |
| 15 | `flexfactor_scout_launch.ps1` line 4 | "SAFE DEFAULT is report-only" | scout is proposal-only by contract (permitted exception), but the wording collides with the no-report-only doctrine |
| 16 | `_spawn` | code after `return proc, ""` (`# pragma: no cover - superseded`) | dead code |
| 17 | `docs/ARCHITECTURE.md` (requested) vs tracked `docs/architecture.md` | two files | one file on Windows (case-insensitive); git tracks `docs/architecture.md`, now modified: original narrative kept as section 0, new map below |

## 3. Unwired-module table

| Module | AS OF BASELINE 736bb2a (lead-verified) | AS OF NOW (verified by me) |
|---|---|---|
| flexfactor_trust | zero importers; absent from wheel | hard import; `_execution_authorization`; in `py-modules` |
| flexfactor_wip | zero importers; absent from wheel | hard import; audit dirty-tree path + publish guard + restore |
| flexfactor_partial | zero importers; absent from wheel | hard import; `_mark_partial`, `_judge`, `review_file`, final review |
| flexfactor_directed | monkey-patched by flexfactor_run.py; absent from wheel | hard import; shim only forwards |
| flexfactor_locate / flags / autoclean | absent from wheel | in `py-modules` |
| flexfactor_prodready .part1/.part2 | FileNotFoundError in a clean wheel | no `.part*` files exist; `flexfactor_prodready_engine.py` present; CI installs the wheel outside the checkout and runs every mode's `--help` |
| flexfactor_sandbox | did not exist | `_run_target_code`, `_spawn`, manifest `containment` |
| flexfactor_ledger | did not exist | final review chunks, `_index_large_file_in_chunks`, `head_matches` |
| flexfactor_coverage | did not exist | `_direct_coverage_evidence` + `merge_into_function_coverage` |
| flexfactor_journeys + assets/explorer.js | `_UI_EXPLORER_JS` string constant in flexfactor.py | `_UI_EXPLORER_JS` gone (grep 0); `.js` is package data |
| Evidence engine `direct_function_coverage` | hard-coded False; gate passed on module execution | rows overlaid from artifacts; gate = `direct_gate.complete`; basis label |
| Final review | patch truncated at 180,000 chars | chunked, ledger-complete |
| Large files > 4 MB | `too-large-for-structural-parser` | `analyzed-in-chunks` (64 MiB hard cap -> blocked) |
| Launchers | hard-coded `C:\Users\firer\flexfactor\flexfactor_run.py` | `Join-Path $PSScriptRoot "flexfactor_run.py"` in all three |

## 4. Unsupported or broken claims (verified)

- `head_matches(_git, project_dir, sha)` built `git git rev-parse HEAD` and would have revoked every approval. **FIXED** the same day: the audit passes `_git_argv` (a complete-argv runner); `LargePatchChunkedFinalReviewTests.test_head_moving_after_review_revokes_the_approval` drives the real check and is green.
- `_direct_coverage_evidence` returns `blocked: {}` always; no audit-path way to record a blocked-with-reason function, so the function-coverage gate needs 100 % direct rows to complete.
- `gather_purpose_evidence` default `gh` runner is raw `subprocess.run` (bypasses `_run`/cmdpolicy); git runner is injected as `_git`.
- Scout `apply_integration` verify step still uses the legacy `_no_network_env()` in addition to the broker's own poisoning (harmless duplicate, but two owners of the same rule).
- CI parity now asserts 0.5.0 (bumped with the version).

## 5. BLOCKED / not implemented (each verified before listing)

1. Windows OS network isolation: `capability_report()` -> `network_isolation: best-effort-env`; AppContainer entry `available: false`.
2. Linux bwrap/unshare/rlimit: code present; 2 sandbox tests skipped on Windows; no Linux host run this session; CI step only prints the probe.
3. Dashboard panels for chunk ledger / direct coverage / journeys: `flexfactor_dashboard.py` has zero references to chunk|coverage|journey|ledger|containment|wip.
4. Durable resume hash-verification scope: `verify_reviewed` re-hashes only `reviewed` entries (clean + findings); `files` outcomes and `bootstrap.done` are not hash-verified; a policy mismatch drops everything; a fresh checkpoint is started instead of continuing a different-policy one.
5. Scout mutation path does not use the orphan-WIP transaction.
6. ~~`--trust-repo` unreachable from audit/prodready~~ FIXED: defined on the audit, prodready and scout parsers (`--help` verified).
7. Purpose evidence `gh` runner outside the broker.
8. `head_matches` argv double-`git` defect.
9. 9 main-suite failures on this tree.
10. ~~Version/migration mismatch~~ FIXED: 0.5.0 everywhere (pyproject, TOOL_VERSION, CI).
