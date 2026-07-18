# FlexFactor Portfolio Hardening Audit (spec 5.11)

Branch: `claude/portfolio-hardening-2026-07-18`
Scope: `flexfactor.py` (single-file core, ~4.3k lines), `flexfactor_scout_launch.ps1`,
`flexfactor_tests.py`, new `pyproject.toml`. No wholesale rewrite (item 14 honored).

## Baseline

- Python 3.12.10.
- `python flexfactor_tests.py` -> **30 tests GREEN** at baseline (no RED at baseline).
- `python flexfactor_dashboard.py --selftest` -> GREEN.
- Launchers ASCII + parse clean under Windows PowerShell tokenizer.
- Working tree clean on `main` before branching.

After hardening: **56 tests GREEN** (30 original + 26 new), dashboard GREEN,
launchers still ASCII + parse-clean, `git diff --check` clean, no secrets in diff.

## Contract matrix (spec section 5.11)

| # | Requirement | Status | Evidence / fix |
|---|---|---|---|
| 1 | Scout default REPORT-ONLY; `--apply` + confirmation to mutate | **FIXED** | parser `--apply` default False + `_confirm_scout_apply`; launcher default "report" |
| 2 | Treat 3rd-party README/issue/source/patch as untrusted (fence) | **FIXED** | `_fence_untrusted` applied to repo summaries + program context |
| 3 | Never execute adopted code outside isolated sandbox w/ limits | **PARTIAL / documented** | tool integrates into user's own repo, not clone+exec; `npm install` still runs install scripts in project dir — see "Not fully reproduced" |
| 4 | Classify PHI/genomic/financial/credential; redact; opt-in; local mode | **NOT DONE / documented** | larger feature; no redaction/local-only mode today — see blockers |
| 5 | Reserve cost atomically BEFORE concurrent calls | **FIXED** | `CostMeter.reserve/release`, `over_limit` counts reservations, wired into prefetch `_first_attempt` |
| 6 | `_run` non-throwing only if failure can't be read as success | **FIXED** | `_run` marks failures `flexfactor_launch_error`, traps all exceptions; `_git_current_branch` no longer fabricates "main" |
| 7 | Clean-file memory keyed to content hash + policy version | **FIXED** | `clean_files` now `{policy, files:{rel:sha256}}`; skip only if hash matches |
| 8 | brain/status: file locks + atomic replace; corrupt recovery | **FIXED (brain)** | atomic temp+fsync+replace, in-proc + cross-proc lock, corrupt quarantine; status.json already atomic |
| 9 | pyproject.toml + pinned deps + supported Python | **FIXED** | `pyproject.toml`, `requires-python>=3.12`, pinned SDK ranges |
| 10 | Unknown model pricing FAIL CLOSED | **FIXED** | `_DEFAULT_PRICE` = highest known rate; warn-once |
| 11 | Keep changes on branch; never auto-push/merge/mutate dirty tree | **FIXED** | scout+audit `push` now opt-in (`--push`), default OFF; dirty-tree gate already present |
| 12 | License/provenance/maintenance/vuln screen before adopting | **PARTIAL** | Repo Rewards `safety.verdict` + `licenseSpdx` consumed by `classify_benefit`; not a full gate |
| 13 | Preserve PS 5.1 ASCII launchers + `_winify` | **PRESERVED** | verified ASCII + parse; `_winify` untouched, regression test intact |
| 14 | Refactor large file incrementally, no wholesale rewrite | **HONORED** | all changes are surgical edits behind tests |

## Findings + fixes (with evidence)

### [HIGH] Item 1 — Scout applied+pushed by default (silent repo mutation)
`main()` scout parser used `--report-only` (store_false, dest apply) so `apply`
defaulted True, and `--no-push` left `push` default True. Running scout on any repo
would generate, commit, AND push integrations with no confirmation.
- Fix: `--apply` (store_true, default False) is now required; `_confirm_scout_apply`
  demands an interactive "apply" (or `--yes`) and **fails safe on a non-TTY**.
  `--push` is opt-in (default OFF). Launcher default flipped to "report".
- Tests: `ScoutApplyDefaultTests` (5).
- File:line: parser ~`flexfactor.py:4363`; gate `_confirm_scout_apply` ~`flexfactor.py:2020`;
  wiring `run_scout` ~`flexfactor.py:1998`.

### [HIGH] Item 5 — Prefetch/parallel workers overshoot `--max-cost`
`CostMeter` had only post-hoc `record()` + a `usd`-only `over_limit()`. The prefetch
pool (`_top_up_prefetch`/`_first_attempt`) let N background workers each pass the
pre-check and then all spend (code comment even admitted "overshoot ... by at most
prefetch_n calls").
- Fix: `reserve(est)`/`release(est)` do a single locked check-and-add; `over_limit()`
  now counts outstanding reservations; `_first_attempt` reserves an estimated cost
  before generating and releases in `finally`. Parallel workers can no longer
  collectively exceed the cap.
- Residual (documented): the SERIAL main-thread generation is not itself reserved,
  so the main thread can still make at most ONE unreserved call between its own
  `over_limit()` checks — an inherent single-call boundary, not the concurrency bug.
- Tests: `BudgetReservationTests` (5, incl. a 50-thread race asserting <=4 grants).
- File:line: `CostMeter.reserve/release/over_limit` ~`flexfactor.py:157-185`;
  `_estimate_call_cost` ~`flexfactor.py:193`; wiring ~`flexfactor.py:3260`.

### [HIGH] Item 7 — Clean-file memory skipped by PATH, not content
`clean_files` was a plain list of relative paths; `_enumerate_source_files(skip_clean=)`
excluded any path in it. A file marked clean, then edited (human/merge/new bug), was
silently skipped forever (until `--recheck`) — a permanent blind spot.
- Fix: clean memory is now `{"policy": POLICY_VERSION, "files": {rel: sha256}}`. The
  audit only skips a remembered file while its **current hash matches**; a changed
  file is re-reviewed. Legacy list/old-policy records are ignored (re-review).
- Tests: `CleanFileHashMemoryTests` (3).
- File:line: `_clean_map`/`_file_sha` ~`flexfactor.py:236-320`; skip decision
  ~`flexfactor.py:3600`; persist map ~`flexfactor.py:3854` + record call ~`flexfactor.py:4023`.

### [HIGH] Item 10 — Unknown model priced at Opus tier (budget under-count)
`_price_for` fell back to `(5.0, 25.0)` for unknown ids. A model pricier than Opus
(e.g. a new `fable`-class or any unrecognized id) would be **under-billed**, letting a
budget-capped run overspend.
- Fix: `_DEFAULT_PRICE` = the highest known rate on each axis `(10.0, 50.0)`; unknown
  ids warn once and bill at that rate (fail closed for budget). `PRICING_VERSION`
  added so the table is versioned.
- Tests: `UnknownModelPricingFailsClosedTests` (3).
- File:line: `flexfactor.py:106-125`.

### [MED] Item 6 — Failure could be read as success
`_run` never raises (correct for resilience), but two gaps: (a) it caught only
`Timeout/FileNotFound/OSError` (a `ValueError` on bad args could still propagate),
and (b) `_git_current_branch` **fabricated "main"** on any git failure — a failure
read as success that could later `git checkout main`, switching the user off their
real branch.
- Fix: `_run` traps all exceptions into a NON-ZERO result tagged
  `flexfactor_launch_error` (documented contract: callers gate on `returncode == 0`,
  so a non-launch is never success). `_git_current_branch` returns the exact SHA on
  detached HEAD, `""` on hard failure, and all four checkout-back sites guard on a
  truthy `prev_branch`.
- Tests: `RunFailsClosedTests` (3), `GitCurrentBranchNoFabricationTests` (3).
- File:line: `_run` ~`flexfactor.py:1520`; `_git_current_branch` ~`flexfactor.py:1553`;
  guards ~`flexfactor.py:1935, 1964, 3487, 3986`.

### [MED] Item 8 — brain.json non-atomic, unlocked, lossy on corrupt/concurrent write
`_save_brain` wrote in place (a crash mid-write truncates it -> next `_load_brain`
returns `{}` and loses ALL memory). With `--parallel` audits, concurrent
`_brain_record_run` calls did read-modify-write with no lock -> last-writer-wins
dropped sibling programs' records.
- Fix: atomic temp+fsync+`os.replace`; in-process `threading.Lock` + cross-process
  advisory lock file (`_brain_file_lock`, steals stale locks); corrupt file is
  quarantined to `.corrupt` instead of silently overwritten.
- Tests: `BrainPersistenceTests` (4, incl. a 20-thread no-clobber test).
- File:line: `flexfactor.py:218-366`.

### [MED] Item 2 — Untrusted third-party text unfenced in prompts
Repo metadata/AI summaries (from Repo Rewards, i.e. third-party repos) and the raw
program context were interpolated straight into LLM prompts — a prompt-injection
surface.
- Fix: `_fence_untrusted(label, text)` wraps them with an injection-resistant preamble
  and hard-to-spoof markers (forged end-markers are broken). Applied to the benefit
  judge, both integration passes, and the program-profiling call.
- Tests: `test_untrusted_fence_neutralizes_forged_markers`.
- File:line: `_fence_untrusted` ~`flexfactor.py:2050`; call sites ~`flexfactor.py:1808,1826,1954,1918`.

### [LOW] Item 11 — Auto-push default (both modes)
Covered with item 1. `push` is now opt-in for scout AND audit; merge already opt-in;
dirty-tree gate already present.

## Not fully reproduced / external blockers

- **Item 3 (sandbox for adopted code).** FlexFactor does NOT clone and execute
  arbitrary third-party repos; scout generates an integration into the USER's own
  repo and runs the user's own build. However `apply_integration` runs
  `npm install <adopted-packages>` in the project dir, which executes package
  install scripts (arbitrary code) outside any isolation, and the build then runs.
  Existing mitigations: build-gate + hard rollback + dedicated branch + clean-tree
  gate. Full isolation (worktree/container, network/CPU/disk/time caps, secret
  scrubbing, `--ignore-scripts`) is an infra change beyond a surgical fix and could
  break legitimate packages if defaulted on — **deferred, documented as a real gap.**
- **Item 4 (sensitive-content classification / redaction / local-only mode).** The
  tool sends source file contents to cloud models with no PHI/secret classifier,
  no redaction, and no local-only provider. This is a substantial new subsystem;
  **not implemented** — flagged as the largest outstanding risk for repos containing
  PHI/genomic/financial/credential data. Recommend: a pre-send secret/PII scan +
  `--local-only` (e.g. Ollama) provider + explicit `--allow-sensitive` opt-in.
- **Item 12 (OSS provenance/vuln/maintenance gate).** Partially covered: Repo
  Rewards supplies `safety.verdict` + `licenseSpdx`, consumed by `classify_benefit`.
  A dedicated license-compatibility + archive-status + CVE gate before apply is not
  implemented here (belongs largely in the Repo Rewards screen). Documented.
- **Live end-to-end audit/scout runs** were intentionally NOT executed (no cloud AI
  calls in verification, no third-party repo clone+exec per operating rules). The
  budget-reservation primitive, `_run` contract, hash memory, brain persistence, and
  scout gating are proven at unit level with mocks/temp dirs/threads.

## Verification

- `python flexfactor_tests.py` -> 56 passed.
- `python flexfactor_dashboard.py --selftest` -> OK.
- `python flexfactor.py scout --help` / `audit --help` -> exit 0 (no argparse conflicts).
- Launchers: ASCII-only; `[PSParser]::Tokenize` parses all three cleanly.
- `git diff --check` clean; no API keys in the diff.

---

## Follow-up round (Codex review of commit 67690b5) — 6 defects: SCOUT fixed but the parallel AUDIT path had the same holes

All 6 fixed with a regression test each, verified RED on 67690b5 first (via
`git stash push flexfactor.py` then running the new suites) then GREEN post-fix.
Notably the parallel-review test on the baseline recorded **$0.82 spend against a
$0.30 cap** — defect 2 reproduced live.

| # | Sev | Defect | Fix | file:line | Test |
|---|---|---|---|---|---|
| 1 | HIGH | Audit mutated without `--apply` (parser only had `--report-only` store_false, apply defaulted True) | `--apply` (default False) + `--yes` + `_confirm_audit_apply` up front in `run_audit`; audit launcher default flipped to report | parser ~`4519`, confirm ~`4188`, gate `report_only` ~`3730` | `AuditApplyDefaultTests` (3) |
| 2 | HIGH | Parallel REVIEW calls bypassed CostMeter (per-file `over_limit()` once, then N workers spend) | `_review_one` reserves `_estimate_call_cost(judge_model,…,REVIEW_MAX_TOKENS)` before each review call, releases in finally, `stop.set()` on refusal | `_review_all._review_one` ~`3246` | `ParallelReviewBudgetTests` (1) |
| 3 | HIGH | `_commit_and_sync` ignored checkout/merge return codes → could write next cycle on wrong branch | check every checkout/merge/abort rc; verify HEAD is back on the audit branch (1 retry) else raise `BranchStateError` (stops the program) | `_commit_and_sync` ~`3556-3585` | `CommitSyncBranchStateTests` (2) |
| 4 | MED | Prefetch reserved ~1k output vs the call's real 32k/128k `max_tokens` | `_estimate_call_cost(model, chars, max_out_tokens)` reserves the requested ceiling; shared `REVIEW/FIX_EDITS/FIX_WHOLE_MAX_TOKENS` constants keep reserve == actual call | `_estimate_call_cost` ~`198`; `_first_attempt` ~`3347` | `EstimateReflectsMaxTokensTests` (2) |
| 5 | MED | `_price_for` substring match mispriced `ft:gpt-4o-mini:…` / `my-gpt-4o-mini` at the cheap tier | match on exact id or `key + separator(-/:/@)` only; aliased/fine-tuned ids fall through to fail-closed `(10,50)` | `_price_for` ~`119` | `ModelPrefixPricingTests` (3) |
| 6 | MED | Audit review/fix/verify/test-gen embedded raw source (hostile comment could suppress findings) | `_fence_untrusted("source"/"patch", …)` on review, both fix generators, cross-verify diff, and unit-test-gen; source-as-data language added to `AUDIT/FIX/FIX_EDITS/FIX_VERIFY_SYSTEM` | `review_file`/`generate_file_fix*`/`_cross_verify_fix` | `AuditSourceFencingTests` (3) |

Follow-up verification: **70 tests GREEN**, dashboard OK, `audit`/`scout --help`
exit 0, all three launchers ASCII + parse-clean, `git diff --check` clean, no
secrets. Audit is now report-only by default (confirmed by test); the parallel
review sweep cannot exceed `--max-cost` (reservation-gated, test-proven).

---

## Round 3 (Codex review) — 4 defects, fixed with CHOKEPOINTS (2 HIGH incl. a sandbox escape)

The pattern (per-call-site fixes miss siblings) was addressed by routing whole
CLASSES of operation through single chokepoints. Each fix has a regression test
proven RED on the prior HEAD (`d302f60`) — the sandbox-escape test literally wrote
a file OUTSIDE the repo on baseline.

### Chokepoint 1 — budget reservation moved INTO the provider call (defect 1, HIGH)
Round 2 only reserved `_first_attempt` prefetches; the main-thread retry/fallback
`generate_file_fix*`, `_cross_verify_fix`, and unit/e2e test-gen all called the model
with no reservation and could spend past `--max-cost`.
- Fix: `_budget_guard(meter, model, prompt_chars, max_tokens)` context manager now
  wraps **every** provider method — `AnthropicProvider` and `OpenAIProvider`
  `.complete` / `.grade` / `.structured` (6 methods). It reserves before the call and
  releases after; a refusal raises `BudgetExceededError`. The now-redundant external
  reservations in `_first_attempt` and `_review_one` were removed (no double-reserve);
  both, plus `_fix_files`, catch `BudgetExceededError` to stop cleanly.
- Every provider call site is therefore bounded: review, benefit/profile judging,
  cross-verify, fix-edits, whole-file fix, unit-test gen, e2e-spec gen, integration
  plan/patch, refactor rewrite/grade — all go through `.structured/.complete/.grade`.
- File:line: `_budget_guard` ~`flexfactor.py:213`; providers `~503-690`; removals
  `_first_attempt ~3400`, `_review_one ~3300`; cap-stop in `_fix_files ~3520`.
- Tests: `ProviderReservationChokepointTests` (3).

### Chokepoint 2 — path containment for ALL generated-file writes (defect 2, HIGH — sandbox escape)
`audit --apply` wrote `os.path.join(project_dir, model_path)` with no rejection of
absolute / drive-relative / `..` paths, so a hostile/confused model response
overwrote files OUTSIDE the repo (on Windows an absolute `C:\…` discards
`project_dir`).
- Fix: `_contained_path(project_dir, rel)` rejects absolute (POSIX + Windows), UNC,
  drive-relative (`C:x`), `~`, and any `..` escape via realpath containment. Routed
  **every** generated-file write through it: unit-test gen, e2e-spec gen,
  scout `apply_integration` (escape -> `ApplyError` -> rollback), and fix-apply
  (defense in depth).
- File:line: `_contained_path` ~`flexfactor.py:238`; writes `apply_integration ~1983`,
  e2e ~`3050`, unit-test ~`4108`, fix-apply ~`3452`.
- Tests: `PathContainmentTests` (3) incl. `apply_integration` writes nothing outside.

### Defect 3 (MED) — `_commit_and_sync` ignored `git add` / `git diff` rc
`git add -A` rc unchecked and `git diff --cached --quiet` treated as binary though
rc>1 is a real error — an index lock could report "nothing to commit" with fixes
unstaged, or commit stale content.
- Fix: check `add` rc (hard-fail `BranchStateError`); treat diff rc 0=none / 1=change
  / >1=error (hard-fail) before any commit/push/merge. File:line ~`flexfactor.py:3600`.
- Tests: `CommitSyncGitRcTests` (3).

### Defect 4 (MED) — retry feedback was an unfenced injection channel
Fix prompts fenced the source but appended `feedback` (populated from build logs +
cross-reviewer reasons — both can carry attacker-controlled source excerpts) RAW as
an `IMPORTANT` instruction outside the fence; model-generated finding `bullets` were
also raw.
- Fix: fence `feedback` (`UNTRUSTED feedback`) and `bullets` (`UNTRUSTED findings`) in
  both fix generators and in `_cross_verify_fix`; only the wrapper stays trusted.
  File:line `generate_file_fix*` ~`2882/2910`, `_cross_verify_fix` ~`3224`.
- Tests: `FeedbackFencingTests` (3).

Round-3 verification: **82 tests GREEN**, dashboard OK, `audit`/`scout`/refactor
`--help` exit 0, all three launchers ASCII + parse-clean, `git diff --check` clean,
no secrets. Confirmed: every provider call routes through the `_budget_guard`
reservation chokepoint, and every generated-file write routes through
`_contained_path`.

---

## Round 4 (Codex review) — 4 remaining budget/safety holes (2 HIGH)

Path chokepoint confirmed ✅; these close the remaining gaps. Each fix has a
regression test proven RED on the prior HEAD (`8ebbeaa`).

### Defect 1 (HIGH) — OpenAI `complete`/`grade` reserved tokens but didn't cap the request
`OpenAIProvider.complete` reserved 16384 and `grade` 4000, but neither
`chat.completions.create` passed `max_tokens`, so the API could bill more output
than reserved and concurrent workers could exceed `--max-cost`.
- Fix: pass `max_tokens` == the reserved amount on both calls (`flexfactor.py:~660`,
  `~674`). Audited Anthropic `complete`/`grade`/`structured` — reservation already
  equals the requested `max_tokens` there (no change needed).
- Test: `OpenAIOutputCapTests` (2) — the SDK kwargs carry the capped budget.

### Defect 2 (HIGH) — git COMMIT failure wasn't fatal
`_commit_and_sync` raised on `add`/`diff` errors but a non-zero `git commit` only
returned stderr text, so callers continued into later cycles with staged-but-
uncommitted changes and could still report "committed".
- Fix: raise `BranchStateError` on commit failure (`flexfactor.py:~3684`); the final
  status now only claims "committed" from a CONFIRMED clean tree (`_git_tree_clean`),
  else reports "UNCOMMITTED changes remain" (`~4212`).
- Test: `CommitFailureIsFatalTests` (1) — simulated hook failure → `BranchStateError`.

### Defect 3 (MED) — health pings bypassed the meter + unsynchronized cache
`_provider_health()` made raw `messages.create` / `chat.completions.create` pings
ignoring the shared `CostMeter`, and the `_PROVIDER_HEALTH` cache was unlocked
(parallel audits issued duplicate hidden pings).
- Fix: pings now run inside `_budget_guard(meter, model, …)` and record their usage
  against the shared meter; the cache is guarded by `_PROVIDER_HEALTH_LOCK`
  (`flexfactor.py:~818-893`). `build_audit_providers` threads its `meter` through.
- Test: `BudgetedHealthPingTests` (2) — the ping bills the meter + releases its
  reservation; the cache lock exists.

### Defect 4 (MED) — scout integration patch prompt embedded raw model/untrusted text
The second pass fenced `repo_summary` but inserted the FIRST model's
plan/packages/file-list AND raw project source directly into `patch_prompt` (whose
output `apply_integration` later writes). `_contained_path` limits WHERE files land,
not whether malicious text becomes instructions.
- Fix: fence the plan fields (`UNTRUSTED plan`) and raw source (`UNTRUSTED source`)
  in `patch_prompt`, and `package.json` + file tree (`UNTRUSTED package`/`filetree`)
  in `plan_prompt` (`flexfactor.py:~1884`, `~1905`).
- Test: `ScoutIntegrationPromptFencingTests` (1) — injected plan + source text land
  inside their fences.

Round-4 verification: **88 tests GREEN**, dashboard OK, all `--help` exit 0, three
launchers ASCII + parse-clean, `git diff --check` clean, no secrets. Confirmed:
OpenAI calls cap output == reservation; commit failure is fatal; health pings are
budgeted + lock-guarded; the scout integration prompt fences all untrusted/model text.

---

## Round 5 (Codex review) — 3 SIBLING defects, fixed as EXHAUSTIVE audits (2 HIGH)

Each was a sibling of an earlier fix; done as full audits (with the audit results
enumerated below) so there is no round 6. Each has a test proven RED on `b3a1175`.

### Defect 1 (HIGH) — reserve-vs-request-cap: full 6-method audit
`OpenAIProvider.structured` reserved `max_tokens` but requested `min(max_tokens,16384)`
— so a 32000/128000 request reserved more than it sent.
- Fix: one `out_cap = min(max_tokens, 16384)` passed to BOTH `_budget_guard` and the
  SDK (`flexfactor.py:~703`).
- **AUDIT — reserved output == request output cap for all 6 methods:**

  | Method | reserve | request cap | equal |
  |---|---|---|---|
  | Anthropic.complete | 64000 | `max_tokens=64000` | ✅ |
  | Anthropic.grade | 4000 | `max_tokens=4000` | ✅ |
  | Anthropic.structured | `max_tokens` | `max_tokens` | ✅ |
  | OpenAI.complete | 16384 | `max_tokens=16384` | ✅ |
  | OpenAI.grade | 4000 | `max_tokens=4000` | ✅ |
  | OpenAI.structured | `min(mt,16384)` | `min(mt,16384)` | ✅ (was ✗) |
- Test: `ReserveEqualsRequestCapTests` (6) — asserts equality for every method.

### Defect 2 (HIGH) — untrusted fields in write-generating prompts: full audit
- (a) scout `generate_integration` fenced repo/package/filetree/plan/source but left
  `profile_blob` + `need` (from the profiling model over untrusted program context)
  trusted. Fixed: fence both in plan AND patch prompts (`flexfactor.py:~1930/~1955`).
- (b) refactor mode concatenated current file + prior feedback into the rewrite
  prompt (written to `args.file`) and the candidate into the grade prompt. Fixed:
  fence `source` + `feedback` (rewrite) and `candidate` (grade); source-as-data
  language added to `REWRITE_SYSTEM`/`GRADE_SYSTEM` (`flexfactor.py:~1130/~1150`).
- **AUDIT — every prompt whose model output is WRITTEN to disk, and its untrusted
  fields (all now fenced):**

  | Prompt (→ writes) | Untrusted fields fenced |
  |---|---|
  | `generate_integration` plan/patch → `apply_integration` writes files | profile, need, repo, package, filetree, plan, source |
  | `generate_file_fix_edits` → fix written | source, findings, feedback |
  | `generate_file_fix` (whole) → fix written | source, findings, feedback |
  | refactor `complete` → `args.file` written | source, feedback (goal trusted) |
  | refactor `grade` → feeds feedback | candidate |
  | unit-test gen → tests written | source |
  | e2e-spec gen → specs written | (no untrusted field: base URL + framework enum only) |
- Test: `WriteGeneratingPromptFencingTests` (2) — scout profile/need + refactor
  source/feedback/candidate fences.

### Defect 3 (MED) — health pings via adapter + single-flight cache
`_provider_health` still called the SDK directly (outside the six adapter methods)
and released the lock before the ping, so parallel callers both missed the cache and
double-pinged.
- Fix: pings now run through `make_provider(name, …, meter).ping()` — a new adapter
  method on BOTH providers that goes through `_budget_guard` + `_meter`; the cache is
  SINGLE-FLIGHT via an in-flight `Event` so concurrent callers issue exactly one ping
  (`flexfactor.py:~638/~745` ping methods, `~865` single-flight).
- Test: `HealthPingSingleFlightTests` (1) — 25 concurrent checks → exactly 1 ping via
  1 adapter build.

Round-5 verification: **97 tests GREEN**, dashboard OK, all `--help` exit 0, three
launchers ASCII + parse-clean, `git diff --check` clean, no secrets. Confirmed:
reserve == request cap for all 6 provider methods; every write-generating prompt
fences all untrusted/model/source fields (table above); health pings go through the
adapter and are single-flight.

---

## Round 6 (Codex review) — 1 HIGH: containment guarded WRITES but not READS

`_contained_path` protected writes, but `generate_integration` READ every
`plan.get("modify_files")` entry (MODEL output influenced by untrusted repo/program
text) via `os.path.join(project_dir, rel)` + `_read_text_safe` with no containment.
A plan naming `..\..\.env` or an absolute path had that file's contents read into the
SECOND provider prompt — fencing marks it untrusted but still DISCLOSES local secrets
to the model.
- Fix: route every `modify_files` entry through `_contained_path` BEFORE
  `os.path.isfile`/`_read_text_safe`; an escaping/absolute/traversal path is skipped
  (never opened, never included). `flexfactor.py:~1957`.
- **AUDIT — every path READ into a prompt or opened, and whether it's model-named:**

  | Read site | Path source | Contained? |
  |---|---|---|
  | `generate_integration` modify_files (`~1957`) | **MODEL** (`plan.modify_files`) | ✅ FIXED this round |
  | `resolve_program_input` pkg/README/file (`~1539/1556/1598`) | user/tool (the program the user pointed at) | n/a (not model) |
  | `_detect_verify` / `_detect_stack` package.json (`~1921/2864`) | tool (fixed filename) | n/a |
  | `generate_integration` package.json (`~1935`) | tool (fixed filename) | n/a |
  | `_review_all` / `_first_attempt` / fix-loop / unit-test-gen source reads (`~3418/3518/3581/4196`) | tool (`_enumerate_source_files` walk of the repo) | n/a (not model); fix-loop also `_contained_path`-guarded |
  | refactor `_load_source_text` (`~1112`) | user (`--file`) | n/a |

  Only ONE read site was model-named; it is now contained. All generated-file WRITES
  were already contained (round 3).
- Test: `ModelNamedReadPathContainmentTests` (1) — a `modify_files` entry
  `../secret.txt` AND an absolute path are never read; the secret never appears in
  the second prompt; a legitimate in-repo file still is.

Round-6 verification: **98 tests GREEN**, dashboard OK, all `--help` exit 0, three
launchers ASCII + parse-clean, `git diff --check` clean, no secrets. Confirmed: every
model-named READ path is contained (table above), not just writes.
