# FlexFactor - Evidence model (0.6.1)

Two artifacts per run: the **run manifest** (`<slug>_run_manifest_<stamp>.json`
written into the target dir by `_write_run_manifest`, never overwritten) and the
**evidence bundle** (`~/.flexfactor/evidence/<project16>/<run_id>/` by
`flexfactor_evidence.write_evidence_bundle`). Plus the markdown audit report and
the durable checkpoint (`~/.flexfactor/runs/<run>/checkpoint.json`).

## 1. Run manifest keys (schema `flexfactor.run_manifest.v1`)

Pre-existing: program, project_dir, branch, mode ("apply"), report_only
(always false), max_cost_usd, usd_spent, providers, cycles, files_reviewed,
review_ledger, applied/unverified/unresolved files, test_files, commit_status,
stop_reason, converged, baseline_ok, fix_notes, verification_is_real/_note,
purpose_* (contract, before, acceptance coverage, progress, samples, noise
band), criteria_closed, release_status, release_status_unmet, paid_rescue,
system_inventory, evidence_run_id, final_commit, code_intelligence_totals,
workflow_coverage, changed_file_rescan, dependency_blast_radius, quality_gates,
evidence_artifacts.

New in this tree:

| Key | Source | Meaning |
|---|---|---|
| `partial_output_events` (<=500) + `partial_output_event_count` | `_PARTIAL_OUTPUT_EVENTS` via `_mark_partial` | every salvaged structured answer: provider, raw_len, correlation_id, when |
| `execution_ledger` (<=1000) | `_EXECUTION_LEDGER` | every broker decision: cmd[:6], cwd, classes, refused/reason OR basis (`os-sandbox`/`trusted-repo`), mechanism, level, network, rc; `spawn: true` for servers |
| `containment` | `flexfactor_sandbox.capability_report()` | platform, mechanisms[], strongest, per-dimension enforcement, `claim` |
| `wip_snapshot_ref` | `audit_one_program` | `refs/flexfactor-wip/<sha12>` or null |
| `wip_restore` | `_restore_wip_if_active` | "restored byte-for-byte; ref dropped" / "NOT restored ..." / "FAILED; ref RETAINED" / "restored but fingerprint differs ... RETAINED" / "ERROR ..." |
| `purpose_confidence` | `purpose_confidence()` | owner-authored / strongly-inferred / weakly-inferred / unresolved |
| `purpose_mutation_authorized` | `mutation_authorized_by_purpose()` | bool; false => gap cap forced to 0 |
| `purpose_evidence_summary` | cache of `gather_purpose_evidence` | counts of sources / contradictions / unknowns / integrations |
| `trust_repo_override` | `--trust-repo` (audit, production ready, and scout) | bool |

## 2. Evidence bundle artifacts

`code-index.json` (files with `status` incl. `analyzed-in-chunks` and a
per-chunk ledger for large files), `purpose-graph.json`, `coverage-ledger.json`,
`quality-gates.json`, `blast-radius.json`, `changed-file-rescan.json`,
`results.sarif`, `manifest.json` (claims: quality_gate_passed,
changed_files_rescanned, blast_radius_ran; final_commit). UI artifacts
(screenshots/trace) under `evidence-runtime/<run>/ui`.

## 3. Chunk ledger

- **Files** (`_index_large_file_in_chunks`): above the 4 MB structural cap ->
  `chunk_text(max_chars=cap//4)`; each chunk hashed, generic-scanned at a line
  offset (symbol lines file-absolute, `chunk_id` on each symbol); record
  `status: analyzed-in-chunks` only when `chunk_scanned == chunk_total`
  (`chunk_ledger_complete`), else `blocked`; > 64 MiB -> `blocked` with reason.
- **Patches** (`_independent_final_review`): `chunk_patch(max 60k chars)` per
  file at hunk boundaries; `ReviewLedger` rows `clean|findings|blocked`;
  `review_ledger` summary (`expected`, `reviewed_clean`, `reviewed_findings`,
  `blocked`, `missing`, `complete`, per-chunk rows) rides in the
  `independent-final-review` gate evidence; `patch_truncated` is always false;
  `evidence_truncated` is true when the evidence JSON exceeds 80k.

## 4. Direct coverage

Rows (`flexfactor_coverage.direct_function_rows`, schema
`flexfactor.direct_function_coverage.v1`): `{id, file, line, name, status:
direct|unproven, evidence: {format, artifact, hits, ...}, reason}` from
coverage.py JSON, Istanbul, lcov, Go coverprofile, JaCoCo, Cobertura.
Gate (`direct_function_gate`): `complete` iff `total == direct + blocked`
(blocked requires a reason; `blocked_without_reason` and `unknown_blocked_ids`
are reported). `merge_into_function_coverage` sets
`function_coverage_basis` = `direct-tool-evidence` when any direct row exists,
else `module-execution-only (NOT direct)`; `coverage_run` meta records the
commands tried, rc, refusals and artifacts. Limitation: the audit path passes
`blocked={}`.

## 5. Journey matrix

From `flexfactor_explorer.js` via `parse_result`: `journeys[]` (id, kind, role,
target, status, reason), `authorization_matrix[]` (role, outcome),
`findings[]`, `summary`, `incomplete_reasons[]`, `skipped[]`, plus the legacy
route/control/form/accessibility/performance evidence. `completeness()` is
true only with zero reasons and `complete: true`; `e2e.ok` requires explorer
rc 0 AND completeness. Roles/viewports/page cap/isolation come from
`FLEXFACTOR_E2E_ROLES|VIEWPORTS|MAX_PAGES|ISOLATED`; `anonymous` is implicit.

## 6. Quality gates table (`flexfactor_evidence.quality_gates` + audit append)

| id | pass when | blocked when |
|---|---|---|
| build | baseline ran and passed | no real verify command |
| tests | suite ran, passed, tests collected | suite not run |
| secrets | no unresolved secret findings | - |
| inventory | `complete_source_inventory` | - |
| rescan | every changed file rescanned | - |
| blast-radius | analysis ran | not run |
| function-coverage | `direct_gate.complete` (or zero functions) | - |
| behavior | e2e ok and executed routes/controls >= discovered (or not applicable) | e2e did not run while applicable |
| independent-final-review | verdict approve AND evidence_consistent AND commit == final sha AND HEAD unchanged | reviewer unavailable -> `ran: false`, fail |
| remote-default-publication | required and the exact reviewed SHA is proven on the remote default branch | required but incomplete; `not-run` when publication is not applicable |

`passed` is all-pass across applicable gates. A `blocked` gate is never a pass,
and a `not-run` gate is never counted as a pass.

## 7. What can and cannot be claimed

CAN: what ran (execution ledger), under which mechanism (containment), which
files/chunks were reviewed (ledgers), which functions have direct evidence and
the basis label, which journeys executed per role/viewport, whether owner WIP
was restored, the release status and its unmet list.

CANNOT: "sandboxed" on Windows; "all functions exercised" without
`direct_gate.complete`; approval from a partial/blocked chunk; PRODUCTION READY
with any critical unknown; a pass from a gate that did not run; on this tree,
a passing `independent-final-review` gate on the audit path (head_matches
defect).
