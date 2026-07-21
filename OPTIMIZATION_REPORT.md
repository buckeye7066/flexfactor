# FlexFactor + Scout Optimization Report (2026-07-21)

## Goal / diagnosis

Make FlexFactor a safer, more reproducible, easier-to-understand autonomous
code-improvement tool, and make Scout a trustworthy building-block finder that
integrates nothing until safety, licensing, fit, verification and human intent
are clear.

Diagnosis of the pre-existing gaps:

1. **Scout's apply decision was one-dimensional.** `classify_benefit` mixed
   fit and safety into a single ADOPT/CONSIDER/SKIP label, and an LLM verdict
   (potentially swayed by repo-authored text) was the only gate between a
   candidate and `--apply`. There was no license policy, no separation between
   "safe to read", "safe to add to the project" and "safe to run", and npm
   installs executed candidate lifecycle scripts (arbitrary network code) on
   the host.
2. **Approval was all-or-nothing.** One blanket "apply?" prompt covered every
   qualifying candidate; there was no per-candidate summary of files, deps,
   license, scripts or rollback plan, and no reviewable policy-file path for
   automation.
3. **The subprocess chokepoint had no notion of risk.** `_run` executed
   whatever command a code path produced; nothing distinguished `git status`
   from `vercel deploy` or `rm -rf`.
4. **No reproducible safety evals.** Nothing measured "does an unsafe
   candidate ever get through?" over a fixed corpus.

## Baseline

- `python flexfactor_tests.py`: **209 OK / 7 skipped** (main @ `8d340fb`).
- `python flexfactor_dashboard.py --selftest`: PASS.
- `python flexfactor.py --help`: lists all three modes (shipped earlier today).
- Invariants in force and preserved: rollback, dirty-tree abort, per-project
  budget (`CostMeter`), edit-anchor fail-closed application, unified-diff
  verification, `_winify` Windows command resolution, ASCII launchers, scout
  apply/push default-OFF, `_confirm_scout_apply` fail-closed.

## Opportunity matrix

| # | Opportunity | Impact | Effort | Risk | Confidence | Decision |
|---|---|---|---|---|---|---|
| 1 | Three-verdict candidate safety (inspect/integrate/execute) + evidence matrix | 0.9 | 0.5 | 0.2 | 0.9 | **DONE** |
| 2 | Deterministic injection/execution-risk scanning as a hard apply gate | 0.8 | 0.3 | 0.1 | 0.9 | **DONE** |
| 3 | Per-candidate approval + reviewed policy file | 0.7 | 0.4 | 0.2 | 0.85 | **DONE** |
| 4 | Isolated installs (`--ignore-scripts` default) + before/after manifest | 0.8 | 0.3 | 0.2 | 0.9 | **DONE** |
| 5 | Command classification gate at `_run` (destructive/credentialed/deploy) | 0.8 | 0.4 | 0.3 | 0.85 | **DONE** (new module `flexfactor_cmdpolicy.py`) |
| 6 | Reproducible eval fixtures (unsafe false-negatives, precision@k) + mocked E2E | 0.7 | 0.4 | 0.1 | 0.9 | **DONE** |
| 7 | Carve `run_scout`/`audit_one_program` orchestration out of the monolith | 0.5 | 0.9 | 0.7 | 0.5 | **DEFERRED** (see roadmap) |
| 8 | OS-level network sandbox for integration verification | 0.6 | 0.9 | 0.6 | 0.4 | **DEFERRED** (Windows job objects/firewall rules; roadmap) |
| 9 | npm-install sandboxing / PII redaction / license-CVE gate (audit items) | - | - | - | - | **UNTOUCHED** — documented owner decisions |

## Changes

### Slice 1 — Scout three-verdict safety + evidence matrix (`flexfactor.py`)

- `build_evidence_matrix(evaluation)`: per-candidate evidence — goal fit,
  language, stars, last activity, license + `_license_compatible` (permissive
  allowlist; copyleft denylist; unknown => `None`), provenance, safety verdict,
  advisories, injection flags, execution-risk flags, install-script/network/
  native-build/dependency-burden status (recorded `unknown` until inspected),
  rollback plan, and a knownness-based `confidence` (0.0-1.0).
- `candidate_verdicts(evidence)`: **deterministic**, fail-closed:
  - `safe_to_inspect`: `"yes"` or `"caution"` (injection indicators present).
  - `safe_to_integrate`: requires license verified compatible AND clean
    Repo-Rewards safety verdict AND zero injection flags. Unknown license or
    missing safety data **fails closed**.
  - `safe_to_execute`: **never granted automatically** — execution needs the
    owner's explicit `--allow-scripts`.
- `_qualifies_for_apply` now hard-gates on `safe_to_integrate is True` on top
  of the ADOPT/CONSIDER tier: an LLM recommendation (or repo text that swayed
  one) can never reach apply by itself. A missing verdict fails closed.
- Reports (console + markdown) show per-candidate verdicts + evidence.

### Slice 2 — Injection defense + isolated application (`flexfactor.py`)

- `_injection_scan` (override-instructions, role-hijack, fence-forgery,
  secret-exfiltration, tool-trigger) and `_execution_risk_scan`
  (curl-pipe-shell, postinstall, native-build) run over every repo-authored
  string. They are *detectors feeding the deterministic gate* — the existing
  `_fence_untrusted` wrapping (all repo text enters prompts only as fenced
  data) is unchanged and remains the first line of defense.
- Per-candidate approval: `_approve_candidate` prints a plain-language summary
  (repo, need, benefit, license, safety, verdicts, install-script policy,
  rollback plan) and requires, in order: dry-run, `--yes` (explicit blanket
  consent), a **reviewed project policy file** `.flexfactor-scout-policy.json`
  (`auto_approve` + explicit license allowlist + verdict must pass — all
  required), or an interactive per-candidate `approve`. No TTY and none of the
  above => the candidate is skipped (`skipped-unapproved`), fail-closed.
- Isolated dependency install: `npm install` runs with `--ignore-scripts`
  unless the owner passes the new `--allow-scripts` flag — lifecycle scripts
  (arbitrary code execution) are blocked by default, enforcing the
  `safe_to_execute` verdict.
- Before/after manifest on every applied integration: files changed (from
  git's own view), dependency delta (snapshotted vs post-install
  package.json), packages requested, lifecycle-script policy — recorded on
  `ApplyResult.manifest` and rendered in the report.

### Slice 3 — Command classification gate (`flexfactor_cmdpolicy.py`, new)

- Standalone stdlib-only module classifying every command `_run` executes:
  `read_only / vcs / build / test / install / network / destructive /
  credentialed / deploy / unknown`.
- High-risk classes (`destructive`, `credentialed`, `deploy`) are **denied by
  default** at the single subprocess chokepoint; refusal preserves `_run`'s
  never-raises contract (rc 126 + `flexfactor_launch_error` +
  `flexfactor_policy_blocked` marker). Owner opt-in via
  `FLEXFACTOR_ALLOW_CLASSES` env or `~/.flexfactor/policy.json`.
- Characterization tests pin every command shape FlexFactor legitimately runs
  today (git workflow incl. rollback's `checkout --force`/`branch -D`/
  `push --force-with-lease`, npm/npx build/test/install, node, python) as
  ALLOWED — the gate is additive safety, not a behavior change. Lease-less
  `git push --force`, `git clean`, `rm -rf`, deploy/credentialed tools are
  blocked.

### Evals + end-to-end scenario (`flexfactor_tests.py`, `eval_fixtures/`)

- `eval_fixtures/scout_candidates.json`: 10 labeled candidates (5 safe, 5
  unsafe: unknown license, GPL, prompt-injection, safety-blocked,
  safety-missing).
- `ScoutEvalFixtureTests`: **zero unsafe false negatives** (hard invariant),
  safe-set precision, ranking precision@5 floor.
- `ScoutEndToEndTests`: fully mocked search -> ranking -> rejection (hostile
  high-scoring candidate) -> proposal -> approval -> isolated application ->
  verification -> commit-on-branch, plus the verification-failure variant
  asserting a pristine tree after rollback.

## UX

- Scout report (console + markdown) now shows, per candidate: license and
  compatibility, safety verdict, the three safety verdicts with reasons, and
  evidence confidence — the "why" behind every decision is visible.
- Applying shows a per-candidate plain-language approval card before anything
  is generated; automation keeps working via `--yes` or the reviewed policy
  file. Skips are reported as `skipped-unapproved`, never silent.
- Applied changes carry an auditable manifest in the report.
- No CLI compatibility breaks: all existing flags/behaviors preserved; one new
  opt-in flag (`--allow-scripts`).

## Safety / privacy

- Repo-authored text remains data-only (`_fence_untrusted`) and now ALSO
  cannot influence the apply decision: the gate is deterministic and computed
  from structured evidence only. Unknowns fail closed at every layer.
- Lifecycle scripts (the main arbitrary-code-execution vector of `npm
  install`) are blocked by default; granting execution is an explicit owner
  action per run.
- The subprocess chokepoint now refuses destructive/credentialed/deploy
  commands unless the owner opted in — a compromised prompt or generated fix
  can no longer shell out to a deploy or wipe command through FlexFactor.
- No secrets are read, stored or transmitted by any new code path; the policy
  file contains no credentials. No live side effects were used in testing —
  the whole E2E is mocked and disposable.

## Verification evidence

- `python flexfactor_tests.py`: **232 OK / 7 skipped** (was 209/7; +23 tests:
  5 cmdpolicy, 8 verdict/evidence, 5 policy-file/approval, 3 eval-fixture,
  2 end-to-end).
- `python flexfactor_dashboard.py --selftest`: PASS.
- `py_compile` clean on `flexfactor.py` + `flexfactor_cmdpolicy.py`.
- Adversarial review (Sol, gpt-5.6-sol): see commit history for verdicts.

## Roadmap

1. **Monolith extraction** (deferred, matrix #7): carve scout orchestration
   into `flexfactor_scout.py` behind the existing CLI once the new E2E
   characterization suite has soaked — the suite added here is the
   prerequisite for doing that safely.
2. **Network isolation for integration verification** (matrix #8): run verify
   commands under a no-network job object / firewall rule on Windows;
   `--ignore-scripts` + deterministic gates are the current mitigation.
3. Enrich the evidence matrix from a real clone in a temp worktree
   (dependency burden, native build, actual lifecycle scripts) before
   `safe_to_execute` can ever be recommended (still owner-granted).
4. Extend the eval corpus with adversarial license spoofing (LICENSE file vs
   SPDX metadata mismatch) and lockfile-tamper cases.
5. Owner-reviewed defaults for `~/.flexfactor/policy.json` (currently empty =
   deny high-risk everywhere).

## Files changed

- `flexfactor.py` — scout safety layer (evidence matrix, verdicts, injection/
  execution scans, license policy), per-candidate approval + policy file,
  `--allow-scripts`, isolated install + manifest, `_run` policy gate wiring,
  report rendering.
- `flexfactor_cmdpolicy.py` — **new**: command classification + policy gate.
- `flexfactor_tests.py` — +23 tests (characterization, verdicts, policy file,
  evals, mocked E2E); `_adopt_eval` fixture updated to the new contract.
- `eval_fixtures/scout_candidates.json` — **new**: labeled eval corpus.
- `pyproject.toml` — `flexfactor_cmdpolicy` added to py-modules.
- `OPTIMIZATION_REPORT.md` — this report.

## Confidence + caveats

**Confidence: 0.85.**

- The deterministic gates are fully unit- and E2E-tested; the suite is
  **offline/mocked** — no live Repo-Rewards, LLM or npm calls were exercised,
  so live-path regressions (e.g. unexpected Repo-Rewards payload shapes)
  would surface only in a real run. Evidence fields degrade to `unknown`
  (fail-closed) on shape drift, which is the safe direction but could block
  applies if the live service omits safety verdicts.
- The injection scanner is heuristic; it is a *defense-in-depth* layer on top
  of fencing, not a guarantee. False positives only demote candidates to
  report-only (safe direction).
- The command gate allowlists by class, not by exact command; `unknown`
  commands remain allowed to preserve audit behavior on arbitrary project
  test runners. High-risk misclassification is covered by tests, but the
  vocabulary is extensible rather than exhaustive.
- Monolith extraction was deliberately deferred (high regression risk vs.
  immediate safety value); the characterization/E2E suite added here is the
  enabling step.
