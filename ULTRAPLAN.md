# FlexFactor ULTRAPLAN (2026-07-24)

Master plan for the next phase of FlexFactor. Grounded in a verified baseline,
sequenced by risk-reduction value, with per-item verification criteria.

## Verified baseline (2026-07-24)

- `python flexfactor_tests.py`: **243 OK / 7 skipped** in ~8s (offline).
- Working tree clean at `69c27ce` (Sol cycle-2 fixes).
- Invariants in force: rollback, dirty-tree abort, `CostMeter` budget,
  edit-anchor fail-closed apply, unified-diff verification, `_winify`,
  ASCII launchers, scout/audit report-only defaults, three-verdict scout gate,
  `flexfactor_cmdpolicy` command gate, adversarial fable<->sol loop with
  materiality gate.

## Open-items inventory (sources)

- `OPTIMIZATION_REPORT.md` roadmap items 1-5 (all still open).
- `PORTFOLIO_AUDIT.md` "Not fully reproduced / external blockers":
  Item 3 (install/build isolation), Item 4 (PII/secret pre-send scan,
  local-only provider — flagged as **largest outstanding risk**), Item 12
  (OSS provenance gate, partially covered).
- README caveat: Anthropic cache marks never actually cache (prompts are
  under the 4096-token minimum cacheable prefix on haiku-4-5/opus-4-8).
- Structure: `flexfactor.py` ~352KB and `flexfactor_tests.py` ~211KB are
  single files; extraction was deliberately deferred until the E2E
  characterization suite soaked (it now has).

---

## Phase 1 — Close the largest outstanding risk (data egress)

### 1.1 Pre-send secret/PII scan at the provider chokepoint  [DONE 2026-07-25]

Shipped as `flexfactor_egress.py` + provider wiring + `--redact`/
`--allow-sensitive` on all three modes + `eval_fixtures/egress_corpus.json`
(+21 tests, suite 264 OK / 7 skipped). Original spec below.

Every audit/refactor/scout call ships raw source text to cloud models with no
secret or PII screening (PORTFOLIO_AUDIT Item 4). Add a deterministic scanner
that runs INSIDE `AnthropicProvider`/`OpenAIProvider` (`complete`/`grade`/
`structured`) — the single egress chokepoint, mirroring the `_run` cmdpolicy
pattern:

- Detectors: PEM/private-key blocks, AWS/GCP/Azure/GitHub/Stripe/Slack token
  shapes, `password=`/`secret=` assignments, high-entropy strings, .env-style
  lines, obvious PHI markers (SSN shape, MRN-like patterns).
- Fail-closed policy mirroring cmdpolicy: on detection, REFUSE the call with a
  `flexfactor_egress_blocked` marker and surface the finding in the report;
  owner opt-in via `--allow-sensitive`, `FLEXFACTOR_ALLOW_EGRESS`, or
  `~/.flexfactor/policy.json`.
- Redaction mode (`--redact`) as the middle path: mask the matched spans,
  send the rest, record what was masked in the report.
- New module `flexfactor_egress.py` (stdlib-only, same shape as
  `flexfactor_cmdpolicy.py`) + characterization tests pinning that normal
  source files pass untouched.

Verify: unit corpus of seeded-secret fixtures (zero false negatives on the
labeled set — same hard-invariant style as `scout_candidates.json`); full
suite stays green; a seeded-secret audit run refuses cleanly.

### 1.2 Optional `--local-only` provider (Ollama)  [MED value, MED effort]

Escape hatch for sensitive repos: an `OllamaProvider` implementing the same
`complete`/`grade`/`structured` surface against `localhost:11434` (the Ellie
project already runs local Ollama on this machine). No cloud egress at all
when selected; quality tradeoff documented. Judge-tier can stay local too.

Verify: provider contract tests (mocked HTTP); `--local-only` audit of a toy
repo completes with zero network calls to Anthropic/OpenAI (spy on providers).

## Phase 2 — Finish the scout trust layer (roadmap 3-5)

### 2.1 Evidence enrichment from a real temp-worktree clone  [roadmap #3]

Clone the candidate into a temp dir (never the user's repo), then fill the
evidence fields currently recorded `unknown`: actual lifecycle scripts in
package.json, native-build markers (gyp/rust/cmake), true dependency burden,
LICENSE-file-vs-metadata agreement. `safe_to_execute` stays owner-granted —
this makes the approval card honest, it does not auto-grant.

Verify: fixture repos (safe / postinstall / native-build / license-mismatch)
produce the expected evidence + verdicts; unknown-on-clone-failure fails
closed.

### 2.2 Eval-corpus extension  [DONE 2026-07-25 — 9 new candidates; typosquat/
lockfile-tamper pinned via the safety-verdict allowlist until 2.1 lands]

Add to `eval_fixtures/scout_candidates.json`: adversarial license spoofing
(LICENSE text vs SPDX metadata mismatch), lockfile-tamper, and
typosquat-name cases. Keep zero-unsafe-false-negatives as the hard invariant.

### 2.3 Owner-reviewed `~/.flexfactor/policy.json` defaults  [DONE 2026-07-25
— `flexfactor policy init|show`, deny-by-default template covering
allow_classes + allow_egress, never overwrites, +6 tests]

Ship a commented template (deny high-risk everywhere, explicit allowlists
empty) + a `flexfactor.py policy init` subcommand that writes it only if
absent. Document in README.

## Phase 3 — Isolation infrastructure (roadmap #2 + audit Item 3)

### 3.1 Research spike: no-network verification on Windows  [timeboxed]

Options to evaluate (1-2 days, decision doc not code): WFP filter via helper,
per-user firewall rule + dedicated restricted user, Docker-when-available,
`--offline` npm + severed proxy env vars as the cheap 80%. Outcome: pick one,
record blast radius and failure modes.

### 3.2 Implement chosen isolation for `apply_integration` verify + audit build gate

`--ignore-scripts` already blocks install-time execution for adopted packages;
this extends containment to the build/verify step itself. Opt-out flag with
loud disclosure in the approval card (same honest-disclosure pattern as
`_verify_disclosure`).

Verify: E2E fixture whose build step attempts a network call — must fail the
gate under isolation, succeed with the opt-out.

## Phase 4 — Structure and cost hygiene

### 4.1 Monolith extraction  [roadmap #1 — now unblocked]

The prerequisite (soaked E2E characterization suite) is met. Carve in this
order, one PR-sized commit each, suite green after every step:

1. `flexfactor_scout.py` — scout orchestration (`run_scout`, evidence matrix,
   verdicts, approval, apply/rollback) behind the existing CLI.
2. `flexfactor_audit.py` — `run_audit`/`audit_one_program`/`_review_all`/
   `_fix_files` + adversarial loop.
3. `flexfactor_providers.py` — provider classes + pricing + CostMeter.
4. Split `flexfactor_tests.py` along the same seams (keep the hermetic
   `sys.modules` load pattern documented in CLAUDE.md).

Trap to respect: desktop .lnk files and launchers point at current filenames —
keep `flexfactor.py` as the CLI entry that imports the new modules, so no
shortcut re-save is needed.

### 4.2 Prompt-caching truth pass  [LOW effort]

Measure real cache hits (API usage fields) on a live audit. If the stable
prefix stays under the 4096-token minimum, either consolidate the high-volume
judge-tier system+instruction prefix past the threshold or drop the
`cache_control` marks and the README claim. Either outcome removes a
misleading cost assumption.

### 4.3 Dependency pinning  [DONE 2026-07-25 — requirements.txt pins the
tested pair anthropic==0.116.0 / openai==2.44.0; pyproject keeps the ranges]

## Phase 5 — Opportunistic hardening (backlog, do when touched)

- Windows reparse-point ancestors: the audit notes full closure needs
  native `NtCreateFile`/`FILE_FLAG_OPEN_REPARSE_POINT`; a ctypes helper could
  close the residual TOCTOU window. Only worth it if containment code is
  being touched anyway.
- Brain analytics: `brain.json` already stores per-project history; a
  `flexfactor.py stats` view (cost per program, accept/reject rates,
  adversarial-round distribution) would make the economy claims measurable.

---

## Recommended sequence

1.1 (egress scan) -> 2.2 (cheap eval wins) -> 2.3 (policy template) ->
2.1 (evidence enrichment) -> 4.2/4.3 (cheap hygiene) -> 4.1 (extraction) ->
3.1/3.2 (isolation) -> 1.2 (local provider) -> Phase 5 backlog.

Rationale: the egress scan is the audit's own "largest outstanding risk" and
reuses a proven pattern (cmdpolicy chokepoint), so it is high value at low
regression risk. Isolation comes after extraction only if the spike says the
cheap path is insufficient; otherwise 3.1's 80% option can land earlier.

## Standing rules for every item

- Suite green (`python flexfactor_tests.py`) + `flexfactor_dashboard.py
  --selftest` PASS before commit; add characterization tests BEFORE moving
  code.
- All new gates fail closed; refusals are marked, never silent.
- Launchers stay ASCII; no key material in code; brain/status stay
  machine-local.
- Each landed item gets an adversarial Sol review pass (the project's own
  convention) before merge.
