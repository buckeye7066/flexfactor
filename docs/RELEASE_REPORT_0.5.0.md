# FlexFactor 0.5.0 release report (2026-08-22)

Status: **RELEASE CANDIDATE** (not PRODUCTION READY - see "Unresolved").

## Identity
- Baseline: `736bb2a` (main, v0.4.1, 2026-08-21)
- Branch: `feat/canonical-runtime-0.5.0`, PR #61
- Final SHA: `3bf262881a3b1db55ce95ce30de447fae57867ef (branch tip reviewed by CI)` (merged into main as `2d5513c`)
- Wheel built from `git archive` of the final SHA, installed into a fresh venv and
  driven from outside the checkout: `flexfactor-0.5.0-py3-none-any.whl`
  sha256 `6913569cb7572364474ba81715fa1fedf32bed4d2e225639b7699be4b40594c3 (built by the package-artifact job from 3bf2628; local exact-HEAD builds during dogfood: 29e5d054..., 0a9915c6...)`
- `flexfactor --runtime-manifest` from that install: tool_version 0.5.0,
  every runtime module importable, `wired` = command_policy, directed, egress,
  execution_broker, partial_output, trust_gate, wip_snapshot all `true`.

## What changed (architecture)
See `docs/ARCHITECTURE.md`, `docs/CURRENT_STATE_GAP.md`, `docs/migration-notes-0.5.0.md`.
One canonical runtime (`flexfactor.run_cli`); execution broker + trusted-repo
gate behind `_run`/`_spawn`; orphan-WIP transaction on audit and scout; partial
structured output fail-closed; chunked exact final review with completeness
ledger and HEAD-race revocation; direct function-coverage gate; chunk ledgers
for large files; browser journey engine (roles/viewports/real submissions);
cited purpose evidence + confidence gating; purpose-aware resume policy.

Files changed vs baseline: `60 files changed, 11496 insertions(+), 1565 deletions(-)`.

## Commands run and outcomes (this session, local Windows host)
| Command | Result |
|---|---|
| `python flexfactor_tests.py` | Ran 888 tests, OK (skipped=8) |
| `python flexfactor_entrypoint_tests.py` | Ran 11 tests, OK (includes a fresh-venv wheel install driven from outside the checkout) |
| `python test_flexfactor_sandbox.py` | Ran 20, OK (2 BLOCKED skips: no OS network isolation on Windows) |
| `python test_flexfactor_wip.py` | Ran 19, OK |
| `python test_flexfactor_partial.py` | Ran 27, OK |
| `python test_flexfactor_ledger.py` | Ran 26, OK |
| `python test_flexfactor_coverage.py` | Ran 32, OK |
| `python test_flexfactor_purpose.py` | Ran 28, OK |
| `python test_flexfactor_journeys.py` | Ran 20, OK (two real Playwright runs + watchdog test) |
| `python test_flexfactor_trust.py` | Ran 6, OK |
| rotation 81 / cli-provider 17 / locate 16 / autoclean 12 / node-lock 2 / prodready-persistence 10 / cursor 28 / rotation-extensions 25 | all OK |

## CI (GitHub Actions, production-readiness workflow)
`Run 32554827325 on 3bf2628: tests (windows-latest) pass, tests (ubuntu-latest) pass (incl. the real Playwright journey runs and the rlimit sandbox path), package-artifact pass (installed-wheel manifest == source manifest, all 5 modes' --help), lint pass, unit-tests (rotation) pass on both OSes, CodeRabbit pass, Cursor Security Reviewer pass. Earlier red runs on this PR each exposed a real defect that is fixed in the final SHA (probe_tiers shipped as a runtime module; host-dependent claude-CLI test; Linux explorer hang; python3.12 unrecognised by the classifier; completeness vetoed by slow pages).`

## Containment results
- Windows (dev host + CI): Job Object containment of process tree, memory,
  process count and CPU time verified live by `test_flexfactor_sandbox.py`
  (memory bomb killed at 64MB, fork bomb capped at 5, grandchild dead after
  timeout, 50MB flood capped at 8MB). Network isolation: **best-effort env
  poisoning only**, reported as such in every manifest (`containment.claim`).
- Linux CI: no bwrap/unshare on ubuntu-latest runner -> `rlimit` mechanism; the
  raw-socket exfiltration test is an honest BLOCKED skip there too.
- Untrusted repository on a host without an OS sandbox: install/build/test
  refused before running (rc 126, `flexfactor_containment_blocked`) - proven
  by `ExecutionBrokerWiringTests`.

## Direct function / route / control / journey coverage
- Mechanism: `flexfactor_coverage` parses coverage.py / c8 / lcov / go /
  jacoco / cobertura artifacts into per-function direct rows; the quality gate
  `function-coverage` passes only on direct evidence or owner-declared blocked
  functions. Module execution is recorded but never satisfies the gate.
- FlexFactor itself: no grounded coverage tool is installed on the dev host
  (`coverage` absent), so the self-dogfood recorded every first-party function
  as UNPROVEN with that reason - truthfully, not as a pass.
- Journeys: the engine's fixture run covers 7 routes x 2 roles, 2 viewports,
  real form submissions with backend verification, duplicate and destructive
  cases (isolated mode). No real web target was journey-tested in this release.

## Dogfood (installed 0.5.0 wheel, from outside the checkout)
Self-audit of `buckeye7066/flexfactor` (auto mode, $4 cap, 6 files, 71 min,
$0.00 spent; stopped by the engineer once every decision path had fired):
- Guards observed firing: every incomplete review stayed NOT clean; decoy
  JSON from small free models refused; an ungrounded finding dropped; the one
  fix attempt rejected by the publication gate and the pre-change tree
  restored; no commit, no push.
- Defects it found in FlexFactor, all fixed in this release: `python -m pip`
  classified as build (network poisoned -> pip could not reach PyPI);
  `llava:7b` (vision) rotated in for code review; OpenRouter 403 "only
  available on agentic harnesses" treated as a bad credential instead of a
  per-route refusal; a recovered batch-review crash logged without a frame;
  versioned interpreters (`python3.12`) unrecognised by the classifier (found by
  CI on Linux).
- Facts it surfaced that are NOT code: FlexFactor's own declared test runner
  (`python -m pytest`, from `[tool.pytest.ini_options]`) is not installed on
  the host, so its baseline publication suite is red; the paid Anthropic key
  answers "credit balance too low" (billing); `--model-mode local` rotates
  onto CPU Ollama and is impractically slow for large files.
- GrantFlow Option-3 run (isolated worktree of `origin/main` @ 9cec9ef0,
  $3 cap, 5 files): `still running at report time (34 min, $0.00): npm install succeeded through the broker (network on for install class); gradle/swift bootstrap steps failed (toolchains absent on this host); the baseline publication suite `npm run test:all` is RED on a fresh worktree of GrantFlow main, so FlexFactor entered bounded baseline repair targeting src/main.jsx; edit generation on the free route returned non-JSON and fell back to whole-file regeneration; rotation then reached the paid Anthropic route whose key is credit-exhausted. No commit, no push (the worktree branch has no remote). Final outcome to be appended to this report when the run ends.`

## Budget and provider usage
Self-dogfood: $0.00 of $4 (free OpenRouter/NIM routes; the single paid
Anthropic call was refused for billing). GrantFlow: `$0.00 at report time ($3 cap)`.

## Independent review
- Code: CodeRabbit (GitHub check) `pass on every pushed revision of PR #61`; Cursor Security Reviewer
  `pass`. Both are automated reviewers; no human reviewer has examined the
  exact final commit.
- Subagent-built modules (sandbox, wip/partial, ledger, coverage, purpose,
  journeys, docs) were each verified by their own test modules and re-run by
  the lead engineer; the lead engineer's wiring was adversarially tested
  (the `_git` vs `_git_argv` defect that would have revoked every approval
  was caught by `LargePatchChunkedFinalReviewTests`).

## Unresolved (why this is not PRODUCTION READY)
1. Windows has no OS network isolation (AppContainer is the documented follow-up).
2. Linux bwrap/unshare paths are written but verified only via rlimit fallback on CI.
3. FlexFactor's own declared runner (pytest) is not in `requirements.txt`; its
   self-audit baseline is therefore red on a fresh host. Owner decision: add
   pytest to requirements or change the declared runner.
4. No real web target had its journey matrix executed in this release.
5. Dashboard shows direct coverage / purpose / containment / WIP / BLOCKED but
   has no chunk-ledger or per-journey panels.
6. Durable resume hash-verifies reviewed entries (and the purpose contract), not
   bootstrap/file inventories.
7. Scheduled runs on the owner's machine will refuse install/build/test until the
   repositories are trusted (`FLEXFACTOR_TRUSTED_REPOS` or policy.json).
8. Paid Anthropic credits are exhausted (owner billing action).
