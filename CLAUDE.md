# CLAUDE.md — FlexFactor

Local dual-provider code tool: refactor / scout / audit. Single-file core
(`flexfactor.py`, ~10k lines) + Tkinter dashboard + PowerShell launchers.
No app deployment — "prod" = the desktop shortcuts working.

## EVERY RUN IS REAL (owner order 2026-08-11, stronger form) — read this first

> "I will NEVER just 'review' with this program either."
> "I do not want test runs as part of the app's functions. Each run must be for real."

The second order (later the same day) superseded the escape hatch the first one
left open: `audit` and `prodready` no longer HAVE a review-only mode at all.
`--report-only`/`--dry-run` were **removed from the audit/prodready CLI** (an
invocation naming them fails argparse, exit 2, before anything runs or spends),
`_assert_review_only_was_asked_for` was deleted (nothing left to assert — a
resurrected copy would mean the mode crept back; `test_review_only_escape_hatch_no_longer_exists`
guards this), and `audit_one_program` contains no `report_only` branch
(`test_review_only_mode_is_gone_from_the_audit_pipeline` pins the absence).
The offline pipeline tests now stub `_full_gate`/pass `--no-bootstrap` instead
of riding a review mode. The surviving rules, and the defect each one killed:

- **No TTY → APPLY, never review.** `_confirm_audit_apply` used to return False
  without a TTY, and `run_audit` turned that into a silent report-only run: the
  2026-08-11 GrantFlow prodready spent **6 hours and $17.75**, found 3,464
  defects, fixed **0**, and exited **0**. `isatty()` alone is not enough —
  measured on this machine, `python ... < /dev/null` under Git Bash reports
  `isatty() == True`, and the piped-answers launcher EOFs after its last answer,
  so **EOFError also means "automation" and applies**. Only Ctrl-C or a human
  typing something other than `apply` cancels, and cancelling **aborts (exit 2)**
  rather than degrading into a paid review (there is nothing to degrade into —
  the review mode no longer exists).
- **Exit codes carry the truth.** `EXIT_APPLIED_NOTHING = 3`: an apply run that
  found defects and fixed none is no longer exit 0. Both retry supervisors read
  exit 0 as success, which is how a 6-hour no-op looked like a good night. The
  launcher does **not** retry on 3 (a retry re-spends the budget for the same
  nothing).
- **`_full_gate` is TRI-STATE.** `None` = no build/verify command existed, so
  nothing was verified. It used to return `True` there, and `_commit_and_sync`
  merged+pushed to the default branch on the strength of it — every repo whose
  toolchain FlexFactor cannot drive shipped unverified. The merge gate is now
  `final_ok is True`; `None` prints `merge+push REFUSED`. A baseline with no
  runnable build command is likewise `None`, not `True` (the report used to say
  "Baseline build: passed" for a build that never ran).
- **Scout is deliberately exempt.** `scout --apply` stays opt-in: the owner's own
  contract for Scout requires "proposal-only default; separate explicit
  FlexFactor apply approval". Never flip it to match audit.

## Purpose awareness (`flexfactor_purpose.py` + `memory/`, 2026-08-11)

> "The goal is not to make every program resemble the same generic application.
> The goal is to make every program successfully perform the particular job it
> was created to perform." — the owner's portfolio directive

FlexFactor reads **why the program exists** before it reads any code.

- `memory/doctrine/` — the owner's four Axiom master prompts, verbatim, with
  `PROVENANCE.md` recording the `.docx` originals and drawing the line between
  what is ingested (purpose, acceptance, status vocabulary, definition of
  production ready) and what is **not** (the ACTIVE_APP lock, no-fan-out and
  one-agent-per-program mechanics — those were instructions to that effort's
  executors, not properties of the programs).
- `memory/purpose_contracts.json` — 26 programs seeded **verbatim** from the
  master prompts' "Assigned Applications" sections. Schema is the owner's
  section 5. Never rewrite a purpose downward to make a run look finished.
- Resolution order (`find_contract`): the audited repo's own
  `.flexfactor-purpose.json` / `docs/purpose-contract.md` / `PURPOSE.md` **wins**,
  then the registry by slug → alias → `local_path`. Nothing authored → the
  purpose is INFERRED and every report says so.
- The contract rides in `purpose_blob`, so **every per-file review** judges
  defects against this program's job. `assess_purpose_gap` then scores each
  numbered acceptance criterion; each gap must cite `acceptance_ref`, so
  `acceptance_coverage()` can render the criteria table a generic linter cannot.
- `fulfillment_pct` with a contract is derived from criteria met / total, not
  the model's impression. The owner's purpose text always overrides the model's
  paraphrase.
- **The criteria figure is an ASSESSMENT, not a measurement — and it is
  UNSTABLE.** Live GrantFlow 2026-08-14: the same unchanged tree scored 2/10,
  then 0/10, then 3/10 on three consecutive runs (~30% variance), while the
  doctrine treats it as the headline scoreboard. Determinism is deliberately
  **not** forced (temperature/seed pinning would hide the uncertainty, and what
  the number should MEAN is an owner design decision). Instead
  `assess_purpose_gap` takes `PURPOSE_ASSESS_SAMPLES` (env
  `FLEXFACTOR_PURPOSE_SAMPLES`, default 3) independent assessments
  **concurrently** — N cheap calls, ~1 call of wall clock — and folds them via
  `aggregate_coverage()`:
  - per criterion, the **majority** verdict; a split vote is `UNKNOWN`, never
    `met` (same doctrine as an unattributed whole-purpose gap);
  - gaps are **UNIONed**, de-duplicated by normalized **TITLE only** — the ref
    is the wobbly part, and keying on (ref, title) would emit one gap three
    times, burn fix budget on duplicates, and break `gap_progress()`, which
    closes gaps BY TITLE. Every ref any sample proposed is kept in
    `acceptance_refs_seen`;
  - the observed spread (`criteria_met_samples`, `criteria_noise_band`,
    `assessment_stable`) rides with the number into **every** print, the audit
    report and the run manifest.
  `movement_is_real()` gates the PURPOSE SCORE line: a before→after swing **inside
  the observed band is reported "WITHIN MEASUREMENT NOISE"**, never as criteria
  closed or regressed. A single-sample run reports variance `UNMEASURED` —
  which is NOT the same as stable and must never be printed as agreement.
- Gap-driven fixing: an owner-authored gap is an unmet requirement, so it
  **bypasses `--fix-severity`** and gets `MAX_PURPOSE_GAP_FIXES_AUTHORED` (12)
  instead of 3, worst-severity first. Inferred gaps still respect the fix floor
  (a guess must not drive a rewrite spree). The headline is
  `gap_progress()` — "closed N of M gaps toward the purpose, unblocked K
  acceptance criteria" — not a score.
- **Status vocabulary is enforced.** `production_ready_status()` only returns
  PRODUCTION READY when every applicable condition has passing evidence; a
  critical condition that is `unknown` blocks. `DONE` raises. `forbidden_claims()`
  is the tripwire for "build passes"/"tests pass"/"deployed"/"works locally"/
  "health endpoint returns 200" being used as readiness.

## Version-aware review (2026-08-14) — never recommend an API that isn't installed

The one class where FlexFactor actively **damages** the program it exists to
improve. Live GrantFlow: findings on `GrantMonitoring.jsx` L73, `MyProfiles.jsx`
L86 and `Organizations.jsx` L118 each claimed cache invalidation was broken and
recommended the ARRAY form `invalidateQueries(['key'])`. GrantFlow runs
**@tanstack/react-query 5.101.4**, where that signature was REMOVED in v5. The
object form already in the code is correct, `refetchType` is valid, and keys
match by **PREFIX** so `['profiles']` already matches `['profiles', isAdmin]`.
Applying those three "fixes" would have broken invalidation on three working
pages.

Two defenses, both narrow:
1. **Tell the reviewer what is installed.** `_installed_versions()` reads
   package.json ranges (always present; a range pins the MAJOR reliably) and
   REFINES them from `package-lock.json` (v1 and v2/v3 layouts) when readable.
   `_dep_version_block()` puts the versions of **only the packages this file
   imports** into the review prompt, fenced as repo data with the instruction
   outside the fence. `review_file(..., project_dir=...)` is what enables it.
2. **Gate the advice.** `_version_conflict()` drops a finding whose
   RECOMMENDATION names a signature removed in the installed major, printing
   `[version] <file>: dropped finding ...`. `VERSION_API_RULES` is the table;
   adding a rule requires hard evidence of removal.

Fail-OPEN everywhere: an unknown major (`workspace:*`, `latest`, a git URL) or
an older major never drops anything — dropping a REAL defect is worse than
keeping a questionable one. That is why the v4-keeps-it test matters as much as
the v5-drops-it test.

Corroboration, not a separate bug: those same three files produced repeated
`[no-op]` and timeout outcomes, because the author model could not generate a
passing fix for a non-defect. **A no-op on a file with findings is sometimes the
system correctly declining to break working code** — there is a comment at the
no-op accounting saying so, because counting it honestly as an error is what
made this visible. Do not "fix" that accounting into a success.

## Bare-list salvage is SHARED (2026-08-14) — never discard a good payload over its envelope

`_check_structured_type` is the one chokepoint every provider's `structured()`
output passes through, so its bare-list salvage serves the EDIT path and the
REVIEW path alike. `e4ef6b6` fixed the symptom where it was first seen (edits)
by demanding that **every** element carry **all** of `items.required` — and that
rule then discarded good reviews.

Live GrantFlow 2026-08-14: `FunderDetailDialog.jsx` review failed with
`expected a JSON object, got list`. The payload head was `{"findings":[` — a
valid envelope whose closing brace was cut, so `_extract_json_object` fell
through to the balanced inner `[...]` span and returned a bare list. One finding
omitted `category`, the all-keys rule rejected it, and the whole well-formed
review was thrown away; the file was retried on a slower backend and ended that
cycle **UNREVIEWED**. (It is correctly *not* marked clean — do not weaken that.)

`_list_fits_array_prop()` now SCORES fit instead of demanding perfection, and
`_check_structured_type` wraps into the **unique best-scoring** array property.
Teeth retained: every element must be the right JSON type; for object items every
element must show **at least one** required key and average coverage must clear
`_LIST_FIT_MIN_COVERAGE` (0.34); a **tie** between two array properties is
genuinely ambiguous and still raises. The separate `_salvage_truncated_json`
path (text that does not parse at all) is untouched and regression-tested.

Note the ordering that made this reachable: `_extract_json_object` runs BEFORE
`_salvage_truncated_json`, so a cut envelope with a complete inner array never
reaches truncation repair — it arrives here as a bare list.

## Free-vs-paid failover: the numbers, and why they are those numbers

A stall threshold **below the free route's healthy latency** silently converts a
free-primary setup into a metered one. The governing measurement on this machine:
the FCC proxy runs `PROVIDER_MAX_CONCURRENCY=2`, and a **healthy** judge ping
measured **307.8s**, nearly all of it queued. So:

- `MEASURED_HEALTHY_QUEUE_S = 307.8`, `STREAM_FIRST_EVENT_FLOOR_S = 461.7`
  (1.5x), `STREAM_FIRST_EVENT_DEADLINE_S = 600.0`, `STREAM_IDLE_DEADLINE_S = 120.0`.
- `FLEXFACTOR_STREAM_TIMEOUT` below the floor is **clamped up and logged**;
  `FLEXFACTOR_ALLOW_UNSAFE_TIMEOUT=1` overrides (tests use it). "Make the timeout
  snappier" is the well-meant tweak that bills the owner for free work.
- `_stream_with_deadline` is **two-phase, never total-elapsed**: the first-event
  budget absorbs queueing; after the first event a 120s **idle** timer (reset on
  every event) applies. A long-but-progressing generation is never killed.
- **CONSEQUENCE of "never total-elapsed" (live wedge 2026-08-14, fixed):** a
  stream that keeps dribbling ONE event inside the 120s idle window never times
  out — by design. So the two-phase deadline cannot be the only bound on a
  generation. `_fix_files` consumed its prefetched first attempt with a bare
  `pf.result()` (no timeout) and a live GrantFlow run froze on one file for 25+
  minutes, cost meter static, zero `[timeout]` output (py-spy: MainThread in
  `concurrent/futures/_base.py:451`). `FIX_FILE_MAX_SECONDS` could not save it:
  that deadline is armed BELOW the prefetch consumption and is only tested
  BETWEEN attempts. The wait is now `pf.result(timeout=FIX_FILE_MAX_SECONDS)`
  and expiry abandons the file LOUDLY + re-queues it
  (`PrefetchWaitIsBoundedTests`). The INLINE generation path (a file with no
  prefetch — notably the FIRST file of every batch, and every retry attempt) had
  the same unbounded shape and is now bounded too: `_call_bounded` runs the
  generation on a DAEMON thread and waits only until `file_deadline`; on expiry
  the thread is ABANDONED (Windows cannot interrupt a blocking recv), the file
  is rolled back through the contained chokepoint, accounted `[timeout]` and
  re-queued, and a REFUSED rollback still takes the `[dirty-abort]` path
  (`InlineFixGenerationIsBoundedTests`). `_AbandonedCallTimeout` is caught
  BEFORE the generic `except Exception`, so a timeout never demotes to
  whole-file mode against the same wedged backend. daemon=True is load-bearing:
  a `ThreadPoolExecutor` thread would keep the interpreter alive at exit.
- `_is_backpressure` — 429/overloaded/503/model-loading/cold-start means *alive,
  be patient*: back off and retry FREE, never rescue. **Trap:** markers must be
  specific phrases. A bare `"queue"` marker made `StreamDeadlineError`
  (whose text quotes the queued-call measurement) classify itself as
  backpressure, so real stalls never rescued. `StreamDeadlineError` is
  hard-excluded by type.
- `_note_free_path_hang` **probes `/health` first**. A deadline hit on a proxy
  that still answers 200 is queueing or one wedged socket — the paid hold is NOT
  armed. Return to free is guaranteed: the hold is a 300s window
  (`FLEXFACTOR_FALLBACK_HOLD`), never sticky.
- Damage bounds: `--max-cost` caps dollars per program; `_paid_rescue_admit`
  caps the **rate** at 40 rescues/hour (`FLEXFACTOR_PAID_RESCUE_PER_HOUR`);
  `_paid_rescue_gate` caps concurrency at 3. Every rescue logs the trigger,
  model, duration and running count to stderr, and `paid_rescue_stats()` lands
  in the run manifest.

## TEST HYGIENE TRAP — tests must never touch `~/.flexfactor`

`flexfactor_tests.py` redirects `ff.BRAIN_PATH` and `ff.STATUS_PATH` to a temp
dir **at import**, and `TestSessionIsolationTests` proves it. Do not remove this.
Measured harm 2026-08-11: only one test class patched `BRAIN_PATH`, so test runs
wrote tempdir projects into the REAL `~/.flexfactor/brain.json`; with
`MAX_BRAIN_PROJECTS = 40` and LRU pruning, a couple of full runs **evicted every
real project** (GrantFlow, GeneMap, SermonSmith, IPlay, FutureU lost their
`clean_files` skip sets and run history — the next audit re-reviews and re-pays
for files it had already driven clean). Tests also stomped `status.json`,
clobbering the live dashboard of a run in flight.

## Run / test
```bash
pip install -r requirements.txt                # exact tested pins (or: pip install anthropic openai)
python flexfactor.py --file <f> --goal "..."   # refactor mode
python flexfactor.py scout --program <p>
python flexfactor.py audit --program <p>
python flexfactor.py prodready --program <p>   # detect+install+fix+score, zero questions
python flexfactor.py policy init|show          # owner policy (~/.flexfactor/policy.json)
python flexfactor_tests.py                     # unit tests, no API keys needed
python flexfactor_dashboard.py --selftest
```
Key flags: `--economy` (cheap author tier), `--whole-file-fixes` (legacy, edit
blocks are default), `--repo-rewards-url` (scout backend), `--max-cost` (USD
budget, default 50), `--fix-prefetch N` (parallel first-attempt fixes, default 3),
`--adversarial`/`--no-adversarial` (adversarial fable<->sol fix-verify loop, default
ON), `--adversarial-rounds N` (re-fix rounds before reject, default 2),
`--adversarial-materiality {material,all}` (default material: don't burn rounds on
exotic goal-irrelevant residuals; accept+document them instead).

## Production-readiness engine (`flexfactor_prodready.py`, 2026-08-04)

Stdlib-only, never imports flexfactor, and NEVER spawns a subprocess itself —
the caller injects `_run`, so every command still passes cmdpolicy + `_winify`.
Closes two silent-failure holes the audit had:

1. **Nothing ever installed dependencies.** On a fresh checkout `npm run build`
   failed for a reason unrelated to the code, the baseline gate went red, and
   every fix was downgraded to syntax-only + `[unverified]`. `_run_bootstrap_phase`
   now installs first (inserted just before the baseline `_full_gate`), so the
   gate measures the code. `--no-bootstrap` opts out; lifecycle scripts are OFF
   by default (`--allow-scripts` to permit) mirroring scout's policy.
2. **`_full_gate` returned `True, "(no build/verify command available)"`** for
   every non-Node/Python repo — indistinguishable from a real pass, so Go/Rust/
   Java/.NET/Ruby/PHP/Elixir fixes shipped with zero verification. `_detect_stack`
   now calls `_enrich_stack_with_toolchains`, which fills `verify_cmds`/`test_cmd`
   from 13 ecosystems. `stack["verification_is_real"]` + `verification_note` carry
   the honest answer and are printed and put in the report.

- `detect_toolchains()` — 13 ecosystems, monorepo-aware to depth 3, skips
  vendor dirs. Node manager follows the LOCKFILE (npm in a pnpm tree breaks it).
- `assess_readiness()` — 13 deterministic gates (no model calls; incl.
  `structured_data_valid`, an offline JSON-LD check — "na" when no JSON-LD,
  severity low so it reports but never blocks). Status is
  FOUR-valued: `unknown` is NOT `fail`, and an `unknown` critical gate still
  BLOCKS (an unevaluated property is not evidence of safety).
- `verification_is_real()` — the honesty guard; `build_needs_deps` is why a
  Python repo with no `.venv` is still verifiable (compileall parses without
  importing) while a Node one is not.
- `_ext_syntax_gate` extends the per-file gate to go/rb/php/sh/json/toml. A
  MISSING interpreter must return `None`, never `False`: `False` makes
  `_fix_files` roll the file back, so on a machine without Ruby every correct
  `.rb` fix would be silently discarded. Guarded by `shutil.which` + `_run`'s
  own `flexfactor_launch_error` marker (covers policy-block/timeout/not-found).
- `prodready` mode = audit with `--apply`, `--fix-severity medium`, readiness ON,
  branch prefix `flexfactor/prodready-`. There is no review-only override:
  `--report-only`/`--dry-run` were removed from audit/prodready outright.
- **Dirty-tree walk-away (2026-08-10).** Prodready no longer faceplants on a
  dirty tree (the GrantFlow failure): `--snapshot-dirty` (default ON in prodready,
  OFF in audit) commits the pre-existing changes verbatim as the sandbox branch's
  FIRST commit (`_snapshot_dirty_tree`, `--no-verify`, files on disk untouched) so
  the per-cycle `git add -A` commits contain only FlexFactor's changes. Every
  cleanup path is fail-closed: the empty-branch drop paths call
  `_drop_branch_restoring_wip` which cherry-picks the snapshot back onto the
  original branch as plain uncommitted changes (conflict-free by construction —
  same parent) BEFORE `branch -D`; if the restore fails the branch is PRESERVED
  (never delete the only ref holding owner WIP). Snapshot-commit failure refuses
  to run + unwinds. `--allow-dirty` keeps legacy sweep-it-in behavior and wins
  over snapshot mode. TRI-STATE (2026-08-11, live SermonSmith abort):
  `_snapshot_dirty_tree` returns ('committed'|'nothing'|'failed', sha) —
  'nothing' = PHANTOM dirt (status reports modifications but `git add -A`
  stages zero content; CRLF/stat-cache churn) and the audit PROCEEDS with no
  snapshot instead of aborting the program. Related parking fixes: the run now
  RETURNS to the owner's original branch at end-of-run (parked-on-sandbox
  repos made the next run see prev_branch == the sandbox branch), and
  `_commit_and_sync` skips the meaningless self-merge when prev == branch.

Trap (RESOLVED 2026-08-13): `MAX_REVIEW_BYTES` was hand-bumped four times
(200k -> 300k -> 400k -> 600k) because this file kept outgrowing it, and each
time `flexfactor.py` silently dropped out of its own audit. It happened a FIFTH
time that night — a 5-line comment took the file to 600,003 bytes, three over
the cap, and `test_flexfactor_can_review_itself` caught it. The constant is no
longer hand-maintained: `MAX_REVIEW_BYTES` is now
`max(600k, sizeof(flexfactor.py) + 200k)`, so ordinary growth can never recreate
the blind spot. 600k remains the floor for every other repo, and the test is
still the guard.

## Map (all in flexfactor.py)
- Constants: `DEFAULT_MODELS` (author tier), `JUDGE_MODELS` (cheap tier),
  `ECONOMY_MODELS` (`--economy`, accepted by EVERY mode - refactor, scout,
  audit, prodready (owner feedback 2026-08-11: one flag, one meaning, every
  mode): author = claude-sonnet-5 at $3/$15 vs Opus 4.8's $5/$25, near-Opus
  code quality; launcher defaults economy ON),
  `MODEL_PRICING` (incl. Claude 5 family), `CostMeter` (hard `--max-cost`
  budget, default $50/program)
- Providers: `AnthropicProvider` / `OpenAIProvider` / `OllamaProvider`
  (`complete`/`grade`/`structured`/`ping`). Ollama (2026-07-25, ULTRAPLAN
  1.2) = LOCAL-ONLY: refuses non-loopback `OLLAMA_BASE_URL` (fail closed),
  no egress gate (nothing leaves the machine), bills `ollama:<model>` ids at
  $0 via the hardcoded `ollama:` prefix branch in `_price_for` (deliberately
  NOT a `MODEL_PRICING` table entry — the generic separator rules must not
  apply to it), and
  `build_audit_providers` never adds a cloud secondary when primary=ollama.
  Defaults: author `deepseek-coder:33b`, judge `llama3.2:latest`.
  FREE-FIRST PREFLIGHT FALLBACK (owner order 2026-08-11): when the chosen
  cloud primary fails preflight, `build_audit_providers` falls back to FREE
  local ollama BEFORE the other paid cloud key; a usable cloud provider is
  kept as cross-check reviewer in that case (the zero-egress no-cloud-secondary
  rule applies only when the owner POINTS at ollama). An owner-chosen usable
  primary still wins;
  `_cached_system()` marks Anthropic system prompts cacheable; `_judge()` routes
  classification calls to the judge tier
- **CONCURRENT FREE-REVIEW POOL** (2026-08-12, owner correction): local Ollama
  on this machine is CPU-only (measured: 20+ min for one large-file review);
  the FCC proxy (`http://127.0.0.1:8082`, `~/.fcc`/`fcc-server`) answers the
  same review in well under a minute. The old free-first check only ever
  asked `_usable('ollama')` and picked ONE winner, leaving a usable second
  free backend idle. Owner: "make sure these different models are not
  working independently... orchestrated... optimized." Fix, when free-first
  applies (no explicit `--provider`):
  - `_auto_activate_fcc_proxy()` gives zero-setup: probes
    `127.0.0.1:8082/health` directly (no launcher/env pre-setup required),
    and if reachable (or startable via `fcc-server` on PATH), sets
    `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` FOR THIS PROCESS and flips
    the module-level `_FCC_PROXY_ACTIVE` flag BEFORE any call is made (that
    flag is read-only elsewhere, frozen at import in the launcher-driven
    case, so activating it late but before first use is the only safe way
    to arm it without duplicating the deadline/restart protections it
    gates). A real `ANTHROPIC_API_KEY` already present is preserved as
    `FLEXFACTOR_FALLBACK_ANTHROPIC_KEY` (paid rescue), never discarded.
  - `build_audit_providers` then builds `_LAST_FREE_REVIEW_POOL`
    (`[(name, provider, concurrency), ...]`) from EVERY free backend usable
    at once (FCC proxy at 2x concurrency = its own `PROVIDER_MAX_CONCURRENCY`;
    Ollama at 2x = `_ollama_gate()`'s own default), not just one.
  - `_review_all`'s new `_ReviewerPool` puts them all to work on ONE shared
    file queue: `acquire()` tries every backend's semaphore in order and
    returns whichever frees up first, so a fast backend naturally pulls more
    files with no hardcoded ratio - self-balancing by real throughput.
  - The single-provider AUTHOR/FIX phase (inherently more serial -
    build-gating, cross-verification, commits) is NOT pooled; it stays on
    whichever pool member is fastest (`pool[0]`), same as a single free-first
    primary always has been. `reviewers` (the pre-existing --use-both
    cross-check list) still runs on top of the pool result, unchanged
    semantics, filtered so a backend already IN the pool is never
    double-reviewed by itself.
  - `flexfactor_tests.py` neutralizes `_auto_activate_fcc_proxy` to a no-op
    at import (same TEST HYGIENE pattern as BRAIN_PATH/RUNS_PATH/STATUS_PATH):
    this dev machine genuinely runs `fcc-server`, so an unguarded test would
    silently activate real proxy routing mid-suite and poison every test
    that ran after it (measured: broke an unrelated deadline test and a
    transport-recovery test). `FreeReviewPoolTests` installs its own fakes
    per-test and restores the no-op in `tearDown`.
  - Gemini free tier (stretch goal, investigated 2026-08-12, NOT added): no
    `GEMINI_API_KEY`/`GOOGLE_API_KEY` exists anywhere on this machine (env or
    persisted user vars), and AITime's `config.json` Gemini entry is a
    browser-launcher only (`aistudio.google.com`/`gemini.google.com` links,
    "Google exposes no per-account quota API" - no programmatic credential
    tracked at all). Adding it would mean the owner first provisioning a real
    API key, then a brand-new `GeminiProvider` class (complete/grade/
    structured/ping) wired into `make_provider`/pricing - a real new
    integration, not the "clean, low-effort addition" the brief asked for.
    Revisit if the owner provisions a Gemini API key.
- Audit loop: `run_audit` → `audit_one_program` (cycle loop, until-clean) →
  `_review_all` (parallel, judge tier, 35% budget frac) → `_fix_files` →
  `_commit_and_sync`; sandbox branch `flexfactor/audit-<slug>`
- Adversarial fix-verify (fable<->sol): with a 2nd provider present, `_fix_files`
  runs `_adversarial_verify_fix` (`ADVERSARIAL_VERIFY_SYSTEM`/`_SCHEMA`) instead of
  the legacy single-shot `_cross_verify_fix`. The reviewer ASSUMES the fix is wrong,
  hunts residual/new/uncovered defects, and each `needs_work` verdict feeds the
  residual list back so the author re-fixes; loops until a genuinely CLEAN verdict
  or `--adversarial-rounds` (default 2) is hit (then reject+rollback). Fail-CLOSED:
  a downed verifier means the candidate is ROLLED BACK to the exact pre-change
  tree and rejected — never kept as an `[unverified]` success (Master Prompt
  83/88; the CLI help and README state the same contract).
  If a candidate is WRITTEN but its rollback is REFUSED (any of the build-gate / veto /
  adversarial / budget paths), `_fix_files` raises `DirtyTreeError`; `audit_one_program`
  catches it, git-restores the file, and ABORTS the cycle WITHOUT committing (so an
  un-rolled-back unverified candidate is never staged-and-committed).
  Flags: `--adversarial`/`--no-adversarial` (default ON), `--adversarial-rounds N`.
  MATERIALITY GATE: the verifier classifies each residual (`realistic_input`,
  `affects_core`); the loop re-iterates only if >=1 residual is MATERIAL (either true).
  If the sole remaining residuals are sub-threshold (exotic AND goal-irrelevant) the fix
  is ACCEPTED + the residuals DOCUMENTED in the report (no wasted round/credits); cap-hit
  rejects only with a material residual still open. `--adversarial-materiality all`
  restores iterate-on-everything (default `material`). `_residual_is_material()` fail-safe:
  missing keys => material (never silently drop). All fail-closed invariants unchanged.
  `MAX_REVIEW_BYTES` is 600k (300k -> 400k -> 600k as flexfactor.py grew; it is
  ~540k now — see the trap note above; `test_flexfactor_can_review_itself` guards).
- `_fix_files` pipelines generation: `--fix-prefetch N` (default 3, 0=serial)
  first-attempt generations run in background threads while the current file is
  applied/gated/verified; retries + all tree writes/commits stay serial. Scout
  benefit-judging is parallel (8 workers). brain.json capped at
  `MAX_BRAIN_PROJECTS` (40) most-recent projects.
- Fix generation: `generate_file_fix_edits` (search/replace edit blocks,
  DEFAULT — output scales with the change) + `_apply_edits` (exact-unique-match,
  fails closed) → fallback `generate_file_fix` (whole file, 128k). Flag
  `--whole-file-fixes` = legacy. Cross-verify ALWAYS judges a `_fix_diff`
  unified diff, capped at 96k chars (never two full file copies — that was
  ~100k input tokens on big rewrites).
- Scout: `run_scout` → repo-rewards service (localhost:3000, auto-started from
  `C:\Users\firer\repo-rewards\scripts\launch.ps1`) → `generate_integration` /
  `apply_integration` with `_rollback`
- Real-clone enrichment (2026-07-25, ULTRAPLAN 2.1): in `_apply_phase`,
  before per-candidate approval, `enrich_evidence_from_clone` shallow-clones
  the candidate to a temp dir and `inspect_checkout` fills the evidence
  fields that were `unknown` pre-clone (lifecycle scripts, native-build
  markers, dependency burden, LICENSE-text family). Read-only via
  `_read_contained` (symlink-safe); git clone runs no repo hooks; verdicts
  RE-COMPUTED after enrichment. LICENSE-text-vs-SPDX mismatch downgrades
  `license_compatible` to None -> integrate fails closed
  (`skipped-demoted-by-inspection`). Clone failure leaves fields unknown
  (never demotes on transport errors). `--no-clone-inspect` opts out; the
  offline E2E passes it (fixture urls aren't cloneable).
- Scout safety (2026-07-21): per-candidate `build_evidence_matrix` +
  `candidate_verdicts` — THREE deterministic verdicts (safe_to_inspect /
  safe_to_integrate / safe_to_execute), fail-closed on unknowns;
  `_qualifies_for_apply` hard-gates on safe_to_integrate (LLM/repo text can
  never reach apply alone). `_injection_scan`/`_execution_risk_scan` feed the
  gate; `_approve_candidate` = per-candidate approval (dry-run / --yes /
  reviewed `.flexfactor-scout-policy.json` / TTY prompt; else skip).
  npm installs run `--ignore-scripts` unless `--allow-scripts`;
  `ApplyResult.manifest` records files/deps delta + script policy.
  Eval corpus: `eval_fixtures/scout_candidates.json` (zero unsafe
  false-negatives is a hard test invariant).
- EGRESS GATE (2026-07-25): `flexfactor_egress.py` scans every repo-derived
  provider payload (the `instruction`/`prompt` args of `complete`/`grade`/
  `structured` in BOTH providers — system prompts are FlexFactor-authored
  constants, not gated) for secrets/PII BEFORE any cloud call. Default
  refuses (fail closed, `EgressBlockedError` subclasses RuntimeError so
  sweeps degrade to a per-file skip, marker `flexfactor_egress_blocked`);
  `--redact` masks-and-sends, `--allow-sensitive` sends anyway; category
  allow via `FLEXFACTOR_ALLOW_EGRESS` env or policy.json `allow_egress`.
  Block tier = high-confidence only (PEM, vendor-prefix tokens, JWT,
  secret env lines, credential-like assignments, SSN) with placeholder +
  letter-and-digit-value filters so `token = "sentinel"` fixtures never
  block an audit. Eval corpus `eval_fixtures/egress_corpus.json`: zero
  false negatives AND zero false positives are both hard test invariants.
  `EGRESS_MODE` global is set once by `_set_egress_mode` at CLI parse
  (allow > redact precedence), read-only afterward (thread-safe by design).
- Subprocess chokepoint: `_run` + `_winify` (PATHEXT-aware; npm/npx are .cmd shims —
  removing _winify breaks every Node-repo audit with WinError 2). `_run` never raises.
  COMMAND POLICY GATE: `flexfactor_cmdpolicy.py` classifies every command;
  destructive/credentialed/deploy are refused (rc 126 +
  `flexfactor_policy_blocked`) unless allowed via `FLEXFACTOR_ALLOW_CLASSES`
  env or `~/.flexfactor/policy.json`.
- State: `~/.flexfactor/brain.json` (per-project memory incl. clean_files skip),
  `~/.flexfactor/status.json` (dashboard bus via `ProgressBus`)
- RESUME (owner order 2026-08-11, "there needs to be a resume"; FIXED
  2026-08-12 - it was never actually wired in, see the trap note below):
  storage lives in `flexfactor_runstate.py`, one durable file per run under
  `RUNS_PATH` (`~/.flexfactor/runs/<run-id>/checkpoint.json`) -
  deliberately NOT brain.json, which is capped at `MAX_BRAIN_PROJECTS` (40)
  with LRU eviction and is exactly what destroyed every real project's
  memory on 2026-08-11. `audit_one_program` calls `_resume_recover` at start:
  it finds the latest resumable checkpoint for this program+dir
  (`flexfactor_runstate.latest_resumable`) and re-verifies every recorded
  entry against the file's CURRENT contained-read sha
  (`flexfactor_runstate.verify_reviewed`) - recovered clean files join the
  skip set, recovered findings skip cycle-1 review and go straight to
  fixing, anything changed or unreadable is dropped and re-reviewed.
  `_resume_checkpoint_for` then either CONTINUES that same checkpoint (same
  run_id, `resume_count` incremented) or starts a fresh one
  (`flexfactor_runstate.new_run`) if nothing was resumable. During the run,
  `_review_all`'s existing `checkpoint_cb` (every 10 files + at sweep end)
  now calls `checkpoint.record_reviewed(...)` directly - the checkpoint's own
  `reviewed` map already carries forward every recovered entry, so nothing is
  replayed by hand the way the old brain-based save did. At the end of the
  run `checkpoint.finish(status=...)` marks it `"finished"` (converged -
  nothing left to resume) or `"interrupted"` (cost cap / manual-review
  leftovers / a caught exception - stays resumable); a genuine crash never
  reaches `finish()` at all, so the checkpoint stays `"running"`, which is
  also resumable. `--recheck` ignores it. Policy-versioned like clean_files -
  never trusted across a content or policy change. `flexfactor_tests.py`
  redirects `RUNS_PATH` to a tempdir at import, same as `BRAIN_PATH`/
  `STATUS_PATH` (`TestSessionIsolationTests` guards all three).
  **Trap (found 2026-08-12):** `flexfactor_runstate.py` was written, committed,
  and independently adversarial-tested in isolation - and then never called
  from anywhere else in the file. The resume mechanism that actually ran in
  production was a DIFFERENT, older one living inside brain.json's `resume`
  key (`_load_resume_state`/`_save_resume_state`, now deleted). A module
  existing and passing its own tests is not evidence it is wired in; grep for
  every symbol it exports before trusting a "this is now used" claim.
  **Checkpoint-flush gap (found + fixed 2026-08-12):** `_review_all`'s
  `checkpoint_cb` was gated `done["n"] % 10 == 0` - a full-dict-SNAPSHOT
  callback fired only every 10th completed file. Empirically reproduced: a
  real audit of FlexFactor's own 12-file codebase, killed after 11 files had
  genuinely completed review, recovered only 1 of them. Root cause was two
  compounding bugs, not one: (1) the 10-file batching itself, and (2) within
  ONE batched call, looping `record_reviewed()` over 10 entries essentially
  instantaneously defeated `RunCheckpoint.save()`'s own elapsed-time throttle
  - real wall-clock time had passed since the LAST physical flush, so entry
  #1 of the batch tripped the elapsed-time condition and reset the clock,
  and the other 9 landed in memory only (too fast for the throttle to fire
  again). Fixed by making `checkpoint_cb` a per-file DELTA callback -
  `(rel, sha, findings)`, called immediately after EVERY completed review
  (not batched) - exactly what the function's own docstring always promised.
  This also drops the old post-loop full-snapshot re-flush (redundant once
  every file already reported itself) and keeps the caller O(n) instead of
  O(n^2) on a large sweep. `audit_one_program` also now force-flushes the
  checkpoint at the review/fix phase boundary (`checkpoint.save(force=True)`),
  matching the other phase-change force-saves. Proven in
  `flexfactor_tests.py::ResumeCheckpointTests::
  test_killed_mid_sweep_recovers_far_more_than_the_old_batched_checkpoint`:
  the SAME 11-file-kill shape now recovers 10/11 from disk (vs the old 1/11),
  driven through a REAL `flexfactor_runstate.RunCheckpoint`, never calling
  `finish()` (the kill), then reloading fresh from disk.
- Console progress: `ConsoleMeter` (2026-08-11, "no progress meter in option 4")
  draws ONE live status line fed from the same `report(**fields)` stream the
  dashboard uses, with a background tick so spinner/elapsed move during long
  silent LLM/build calls. TTY -> in-place `\r` line (no ANSI; wraps
  builtins.print while active so log lines interleave cleanly, restored on
  stop); redirected -> `[progress]` heartbeat lines every 30s. Best-effort
  (never breaks an audit), ASCII-only, one drawing meter per process
  (parallel runs: extras are no-ops). Started/stopped in `audit_one_program`.
- SHIP TO MAIN (owner order 2026-08-10, extended to audit 2026-08-11): push+
  merge default ON for BOTH audit --apply and prodready — verified results go
  back to main automatically. Still gated: merge only on a green final build,
  push `--force-with-lease` for the sandbox branch, protected mains fall back
  to a PR with auto-merge, conflicts abort cleanly. `--no-push`/`--no-merge`
  (raw-argv checked) opt out. Every audit/prodready run applies (review-only
  removed 2026-08-11), so push+merge defaults are live on every run.

## Gotchas
- **Launchers must stay ASCII** (PS 5.1 + no-BOM = CP1252; em-dashes break strings).
- Desktop .lnk files (G:\One Drive\Desktop: FlexFactor / Scout a Program /
  Audit a Program) point HERE (`C:\Users\firer\flexfactor\`) — moving/renaming
  files means re-saving the shortcuts.
- Keys come from env (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) — never hardcode.
- Tests use the hermetic load pattern: `sys.modules["flexfactor"] = module`
  BEFORE `exec_module`, or dataclasses with future annotations die.
