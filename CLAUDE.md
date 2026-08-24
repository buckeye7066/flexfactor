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
- **THE PUSH IS GATED TOO (2026-08-14) — this is what made the merge gate real.**
  Live GrantFlow measured across one run's five batches: **4 committed+pushed
  green, 1 committed+pushed with `build FAILED`** — roughly a 1-in-5 chance per
  batch of putting a repo's `main` red, unattended and auto-pushed. The gate was
  never the problem: it ran `npm run typecheck` + `npm run build`, they failed,
  and it correctly returned `False`. **The branch push was simply not gated on
  it** — only the merge was. And because the 2026-08-11 order removed sandbox
  branches, `branch` IS the owner's real branch, so `prev_branch == branch`, the
  merge block is skipped entirely, and that ungated push was the *only* thing
  publishing. The merge gate was decorative. The `None` branch's own
  `merge+push REFUSED` message was therefore half a lie: the push had already
  happened above it.
  Now `if final_ok is True:` guards the push, with `False` and `None` each
  printing an explicit `PUSH REFUSED - ...` naming which state it was. **The
  local commit still happens in every case** — work is never lost, the next
  cycle still builds on it, and the first cycle whose gate passes pushes the
  accumulated commits, so the tip origin ever sees is green.
  The old guard `test_merge_and_push_refused_on_an_unverified_gate` was a
  **source grep for the sentence** and passed happily the entire time red builds
  were shipping — a check that cannot fail proves nothing. The replacements
  drive the real `_commit_and_sync` decision and assert on the git argv issued.
- **PUBLICATION REQUIRES THE PROJECT'S OWN TEST SUITE (2026-08-14, owner:
  "when you find flexfactor bugs like the push gate, fix them").** The build
  gate alone let a BUILD-CLEAN regression ship: the live Family Castle Clash
  audit rewrote an ESM `import` to `require` and widened the room-code
  alphabet past the join validator — Vite still built, four per-cycle pushes
  labelled "Final build gate: passed" carried both to the owner's main, and
  only the ungated end-of-run suite reported RED. `_publication_gate()` now
  fronts `_commit_and_sync`: the build gate first (red/unverified returns
  immediately — the suite's 20+ minutes are never spent on an unpublishable
  tree), then the STRONGEST suite the project exposes (`full_suite_cmd`,
  i.e. test:all/ci, falling back to `test_cmd`; detected in
  `_enrich_stack_with_toolchains`). A defined-but-red suite is a hard
  publication failure — push AND merge refuse on the same verdict, the work
  stays committed locally, and the commit message says which evidence backed
  it ("build + project test suite" vs "build only; no project test suite
  configured" — a repo with no runner still publishes on the build, since a
  blanket unpublish is not a safety gate). Completion honesty rides along:
  `checkpoint.finish` marks a run "finished" only when the sweep converged
  AND the suite isn't red AND readiness didn't fail — a converged sweep atop
  a crashed mechanics test is "interrupted", and the console says
  "done - verified" vs "done - partial". NOTE: two implementations of this
  gate were built concurrently (this one via the GitHub-hosted runner; a
  local one, discarded as the narrower of the two) — when reworking it,
  check origin/main first.
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

## Competitor research (`flexfactor_competitors.py`, audit PHASE 1b, 2026-08-16)

> "it would probably be a good thing for flexfactor to have it too. Maybe allow
> flexfactor and factorydeck (and purpose foundry) by default also use scout and
> repo rewards as well their own web search for competitors." — owner, 2026-08-16

The purpose engine can drive a program to 10/10 against its own acceptance
criteria while it still ships less than every product its users could switch to.
Phase 1b runs between the purpose baseline and the generic sweep, is **ON by
default** (`--no-competitors` opts out), and never aborts an audit: every failure
is a NAMED skip in `sources_skipped` that reaches the console and the report.

Containment mirrors `flexfactor_prodready.py`: stdlib only, **never imports
flexfactor**, and the caller injects the judging callable, the Repo Rewards
search function and (in tests) the URL opener.

- **The purpose contract stays the authority.** Every competitor idea is a
  PROPOSAL judged against the audited program's own job; `accept=false` ideas are
  reported and never bridged. "Competitor X has feature Y" is not a reason to
  build Y — copying a competitor's roadmap IS the "make every program resemble
  the same generic application" failure the portfolio directive forbids.
- **`license_reuse_mode(spdx, source_available)` is the legal gate, and it is
  mechanical.** Verified-permissive → `direct-code-reuse`; copyleft/restricted OR
  a closed-source product with no inspectable source → `clean-room-from-documented-behavior`;
  anything unverifiable → `reference-only`. `may_copy_source()` is True for
  exactly ONE mode and is the single place that answer lives. Note the asymmetry
  that makes it safe: a KNOWN-BAD licence still permits clean-room work from
  published behaviour, while an UNKNOWN licence permits less, because we cannot
  even establish what reading the source would oblige. The mode and the evidence
  URLs ride into the fix instruction, so the author model is told in-band whether
  it may consult the source.
- **One licence oracle at runtime.** `_competitors_module()` calls
  `set_license_oracle(_license_compatible)` at import, so the scout integrate
  gate and the competitor reuse gate cannot reach opposite conclusions about the
  same repo. The module's standalone table exists only for use outside
  FlexFactor, and `test_module_table_agrees_with_flexfactors_own_license_oracle`
  reddens on drift.
- **Nothing is invented.** A name the model recalled that NO reachable source
  corroborated is kept, marked `evidence_status="unverified"`, has its `accept`
  forced to False with the reason prefixed `NOT ACTED ON:`, is excluded from the
  verified count, and cannot bridge. Fewer than `--competitor-count` (5) is said
  out loud by `coverage_note()` as a **SHORTFALL, not evidence that fewer
  competitors exist**. A report with no competitors says the research failed, not
  that the program has none.
- **Search ladder is keyless.** SearXNG (`FLEXFACTOR_SEARXNG_URL`) → DuckDuckGo
  **Lite** → Wikipedia, plus GitHub repo search as the SPDX oracle
  (`GITHUB_TOKEN`/`GH_TOKEN` only raises the rate limit). **Measured 2026-08-16:
  `html.duckduckgo.com` answers 202 with a challenge page and ZERO results from
  this machine; `lite.duckduckgo.com` answers 200 with real organic results.**
  Do not "simplify" back to the html endpoint. Ad rows (`duckduckgo.com/y.js`)
  are filtered — a sponsored link is not a competitor finding.
- **Bridging is bounded.** Only accepted + corroborated + licence-permitted +
  `code_fixable` ideas with a real file enter `_fix_files`, capped by
  `--competitor-fixes` (default 5) and still under `--fix-severity`, the build
  gate and the adversarial verifier. The cap is checked BEFORE the append — a
  post-append check let `--competitor-fixes 0` still emit one finding.
- **TRAP the tests pin:** `all_findings` is REASSIGNED wholesale by every cycle
  (`all_findings = flat`), so competitor findings appended at phase 1b would be
  silently discarded. They are merged in AFTER the cycle loop, next to the purpose
  gaps. `test_competitor_findings_are_merged_after_the_cycle_loop_not_before`
  asserts the ordering of the three call sites.

### Measured on the first real runs (SermonSmith, 2026-08-16) - read before "improving" this

Live against `C:\Users\firer\sermonsmith` (owner-authored purpose contract,
java+node), free FCC route, production Repo Rewards, ~280s: **8 corroborated
competitors, 0 unverified, 2 accepted / 6 rejected**, sources
`web:duckduckgo + github + repo-rewards`, `web:searxng` a named skip
(`FLEXFACTOR_SEARXNG_URL` is not set on this machine).

The rejections are the evidence the design works, not a shortfall: OAuth
(ScribeJava), a CLI Bible reader, a Wine installer for Logos, a changelog
generator, a generic REST wrapper, and church member/event administration were
each rejected **with a reason quoting SermonSmith's own purpose**. The two
accepted ideas both cite the contract's requirement to preserve *exact
provider-sourced Scripture text*. That is the purpose contract acting as the
authority, exactly as intended - a run where everything is accepted would mean
the gate is not working.

**RESOLVED 2026-08-16 (two halves): idea extraction now runs on the AUTHOR
tier, and every dropped idea is ACCOUNTED.** The live run had measured that on
the FREE judge tier competitor ideas were effectively report-only: the cheap
model filled `idea_title` / `why_valuable` / `purpose_reason` but omitted
`severity`, `code_fixable` and `file`, so `competitor_findings()` dropped every
idea and **0 of 8 bridged** — while the report said "2 accepted" with no word
about where they went.
1. `research_competitors(..., author=)` routes `IDEA_SYSTEM` extraction to the
   injected STRONG-tier callable (the audit passes
   `purpose_reviewer.structured`, NOT `_judge` — the tier is chosen per-call
   via the `model=` arg, so the same provider object serves both). Discovery
   and benefit judging STAY on the cheap judge; extraction is the one call
   that decides whether an idea can actually be built. Falls back to `judge`
   when no author is supplied. `CompetitorIdeaAuthorTierTests` pins the
   routing at both the module and the audit call site.
2. `competitor_findings()` writes `research["bridge_ledger"]`:
   `candidates == bridged + dropped(by named reason)`, cap overflow recorded
   (the `break` at the cap is GONE — the loop walks on so the tail is named),
   `accounted: False` renders an ACCOUNTING-GAP warning in report + console.
   The PHASE 1 purpose-gap loop got the same ledger (its three bare
   `continue`s and the silent `[:cap_b]` truncation were the identical
   shape — and its drops are the owner's own unmet acceptance criteria).
   `CompetitorBridgeLedgerTests` pins both.
A dropped idea remains correct conservative behaviour (a missing severity must
never be invented) — the defect was the SILENCE, not the drop.

**Second known limitation:** competitor *discovery* quality is the model's.
"Scribe" resolved to `scribejava/scribejava`, which is not a sermon tool at all.
The licence gate handled it correctly (MIT, its own repo, direct-code-reuse) and
the purpose gate rejected it - both layers did their jobs - but an ambiguous
one-word competitor name still costs one idea-extraction call.

### Repo Rewards endpoint: local-first, production-by-default (2026-08-16)

`resolve_repo_rewards_url()` is the one chooser for scout AND audit: an
explicitly named non-local host is obeyed; otherwise local wins when it is
genuinely up; otherwise the production Railway deployment is used
**automatically**. The old default-off opt-in (`--allow-remote-repo-rewards`)
was a privacy guard against sending program-derived queries off-host, which the
owner has overridden for their own tooling — and with local RR usually down it
was simply turning the feature off. Opt back out with `--no-remote-repo-rewards`
or `FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS=0` (note `_env_falsy`: an UNSET var is
not an opt-out). The chosen endpoint is always printed and lands in the report;
`None` comes back with a note naming every door that was tried.
**LAUNCHER DRIFT:** `flexfactor_scout_launch.ps1` still passes
`--allow-remote-repo-rewards`, so the flag is KEPT as an accepted no-op — deleting
it is argparse exit 2 and a dead run.

## "GrantFlow never opened": 265s of SILENT indexing before phase 0 (2026-08-19)

Live 10-program run: GrantFlow's checkpoint sat at `phase="starting"` with
`updated == started` while smaller programs in the same batch had reached the
baseline gate. Nothing was wrong with name resolution (`_find_local_project`
resolves `grantflow` -> `C:\Users\firer\GrantFlow` correctly) — the run was
inside `build_repository_index`, which is called immediately after the
checkpoint is created and **before any `set_phase`**, so a big repo prints
nothing and looks dead.

- **Two full reads per file.** `_read_bytes` read the content, then
  `_sha256_file` opened and read the *same file again* for the digest, plus a
  second `_safe_file` resolve and a second `stat`. That is exactly the
  anti-pattern this machine punishes hardest (measured 11-70ms per filesystem
  op under AV scanning). `_read_bytes_full` now returns
  `(raw, truncated, path, size)` from ONE resolve + ONE stat + ONE read, and
  the digest is taken from the bytes already in memory.
- **MEASURED on GrantFlow (3,935 files): 265.2s -> 37.0s**, byte-identical
  index (same 3,935 files / 17,735 symbols / 1,197 routes / 3,676 controls),
  and a 250-file sample verified digest-for-digest against an independent
  streaming hash (0 mismatches).
- **The one case the in-memory digest would be WRONG is a TRUNCATED file**
  (over the 4MB cap): `raw` holds only the prefix, so hashing it would publish
  a digest that is not the file's. Truncated files still stream. GrantFlow
  happens to contain zero of them, so the live measurement could not have
  caught a regression here — `test_a_truncated_file_hashes_the_WHOLE_file...`
  is the only guard, and it fails when the branch is removed.
- **Observability is the other half of the fix**: the phase is now named
  (`indexing repository (baseline evidence)`) BEFORE the walk starts, and a
  progress callback ticks the dashboard + console every 10s. A long job that
  says what it is doing is not a hang; a silent one is indistinguishable from
  one.

## Phase 0 could not read a PYTHON failure at all (2026-08-19, live IPlay audit)

`[4/10 iplay] STOP: baseline publication suite remains red after bounded
targeted repair; unrelated review was not started. Targeted: (no contained
source path found).` — while the failing file was printed on screen three lines
above. `_FAILURE_SOURCE_RE`'s boundary accepted only `:\d`, whitespace or `>`,
but **Python delimits paths differently**: a traceback prints
`File "….py", line 81` (closing QUOTE) and pytest prints
`FAILED tests/test_thing.py::test_a` (DOUBLE COLON). Neither matched, so
`_publication_failure_paths` returned `[]` and phase 0 stopped **every Python
repo** on its first round. The boundary now also accepts quote / comma /
bracket / `::`; the `.jsx`-not-clipped-to-`.js` guard is unaffected (`x` is
still not a boundary char) and has its own test.

Two ranking defects rode along, both Python/Go blind spots:
- **`_TEST_FILE_RE` was JS-only** (`.test.` / `.spec.` / `__tests__/`), so
  pytest's own `test_*.py` / `*_test.py` and Go's `*_test.go` were classified
  as IMPLEMENTATIONS. That inverted the implementation-first ordering this
  module exists to enforce AND handed the repair model the "fix the product,
  preserve the test" instruction *while pointing it at the test* — an
  invitation to rewrite a red test as if it were product code.
- **The sibling fallback only understood the JS `.test.` infix.**
  `_test_import_candidates` parses JS relative imports only, so for Python this
  fallback is the ONLY route to the implementation; it now maps
  `test_foo.py`→`foo.py` and `foo_test.py`/`foo_test.go`→`foo.py`/`foo.go`.
  It also now requires `sibling != rel`: with no convention applied the old
  code appended the TEST to `implementations`, listing it as its own
  implementation and duplicating it in the returned ranking.

### Same day, second miss: the path had to START with a magic directory

The boundary fix above was necessary and not sufficient. The next live IPlay run
printed `[4/5 Iplay] STOP: ... Targeted: (no contained source path found)` while
pytest had printed `FAILED iplay/test_production_bridge.py::...`. Measured
against that exact line, `_FAILURE_SOURCE_RE` returned
**`/test_production_bridge.py`** — a WRONG absolute path — and
`FAILED test_motionsync.py::test_a` returned **`[]`**.

Cause: the relative alternative only accepted a first segment of
`apps|packages|src|tests?|lib`. `iplay/` is none of those, so the **POSIX-absolute**
alternative won and matched from the slash onward; a bare repo-root filename had
no alternative at all. Note the shape — the earlier POSIX fix made the failure
*silently wrong* instead of merely empty, which is worse.

There is now a fourth, general relative alternative (any path, including a bare
filename). **It is LAST on purpose** — a magic-directory path keeps the older
branch, which tolerates spaces inside a path. It excludes whitespace so the
match cannot swallow the runner's own `FAILED` / `FAIL` prefix *and the spaces
after it*, and excludes comma/paren/bracket — the characters the boundary
lookahead already treats as terminators — so `(motionsync.py:3)` yields
`motionsync.py`, not `(motionsync.py`.
**Over-matching is SAFE and under-matching is FATAL**: `_existing_failure_path`
drops every hit that is not a readable file inside the repo, which is what
discards the site-packages frames that dominate a pytest traceback
(`test_frames_outside_the_repository_are_discarded` pins it). Do not "tighten"
this regex without re-running `RedPublicationBaselineRepairTests` — the
`.jsx`-not-clipped-to-`.js` guard and the runner-prefix guard both live there.

Widening the regex made a **latent** bug reachable, now closed:
`_existing_failure_path` called `candidate.lstrip("./")`, and `lstrip` strips a
character SET — so `.github/workflows/x.py` became `github/workflows/x.py`,
failed the contained read, and came back as "(no contained source path found)".
That call is deleted; `_canon_rel` (whose own docstring forbids it, from the
2026-08-14 file-identity work) already strips whole leading `./` segments.

## The 8-hour zero-work night: a 400 that named its own fix (2026-08-20/21)

FlexFactor ran roughly eight hours across five repos overnight and produced
**one one-line code change** (`iplay/performance_transfer.py`, 1+/1-). Every
program ended `PROVIDER-OUTAGE ABORT`. **There was no provider outage.**

Measured, from the runs' own manifests — note the denominators, which the
reports did not print:

| program | reviewed | candidates | provider recorded | spend |
|---|---:|---:|---|---:|
| FutureU | 1 | 57 | `rotation:google/recurrentgemma-2b` | — |
| Iplay | 9 | 43 | `rotation:groq/compound` | $0.69 |
| PromoPilot | 2 | 82 | rotation | $1.55 |

Iplay's `stop_reason` carried the answer verbatim:
`BadRequestError: Error code: 400 - '`max_tokens` must be less than or equal to
`4096`'`. **The route's output ceiling was 4096 and FlexFactor asks for
`REVIEW_MAX_TOKENS = 16000`.** Re-probed live 2026-08-21 against the real
`groq/groq/compound` route: same 400, ceiling now 8192.

Four defects, in the order they compounded:

1. **`_openai_output_ceiling` only knows `gpt-*` ids.** Rotation serves **641**
   catalog routes from a dozen backends; every non-OpenAI id inherited the
   16384 default. The static table cannot enumerate the catalog and never will.
   Fix: `_LEARNED_OUTPUT_CEILINGS` — the provider's own 400 NAMES its ceiling
   (`_parse_max_output_limit`, four message shapes), so learn it, clamp, and
   retry once inside `OpenAIProvider.structured`. Learned ceilings only ever
   move DOWN. Below `MIN_USABLE_OUTPUT_TOKENS` (512) the route cannot answer at
   all and raises the typed `RouteCapabilityError` instead.
2. **`flexfactor_rotation._is_retryable` blanket-rejected status 400** with the
   comment *"a bad request stays bad on every backend"*. For a max_tokens /
   context-window / unsupported-parameter 400 that is exactly backwards: it is
   the strongest possible evidence that a DIFFERENT route would work.
   `_run` therefore re-raised **without giving any of the other 640 routes a
   turn**. `is_route_capability_error()` now splits the two, so a capability
   4xx rotates and a malformed request still fails fast (as do 401/403 — a
   wrong credential is not worth a 641-route tour).
3. **The circuit breaker lied about the cause.** Three consecutive
   zero-completion batches printed `provider outage`. The backends were up and
   answering. A confidently wrong diagnosis is worse than none — it points the
   owner at provider status pages for eight hours. The message now names what
   was actually observed, with the reviewed/candidate ratio in it.
4. **Its rollback could have eaten owner work.** The abort ran
   `git reset --hard HEAD` + `git clean -fd` on the strength of a comment
   claiming *"the run began from a required-clean tree"* — false under
   `--allow-dirty`, which **every** overnight launcher invocation passes. On
   2026-08-21 that reset happened to FAIL, which is luck, not a guard. It is
   now gated on `not args.allow_dirty` and says so when it declines.

**The name-pattern blocklist was not enough and could not have been.**
`_UNFIT_CODE_PATTERNS` (added 2026-08-20 for prompt-guards / TTS / vision)
filters by NAME. `google/recurrentgemma-2b` and `groq/compound` are text models
with unremarkable names, so they sailed through and reproduced the identical
fake-outage. **Gate on measured CAPABILITY, not on a name you thought of.**

### `Files reviewed: 1` — a numerator with no denominator

FutureU's audit report said, in full, *"Files reviewed: 1"*. FutureU has 57
candidate source files. Nothing in the report, the console, or the manifest
carried the other 56, so a 98% miss read like a small clean repo. That is the
same shape as the 6-hour $17.75 run the exit-code-3 rule exists to prevent.

`build_review_ledger()` now enforces the owner's standing identity —
**`candidates == acted_on + skipped_by_reason + failed`** — with per-reason
counts (`review_incomplete`, `unreadable`, `oversized`, `skipped_known_clean`,
`never_attempted`). `review_ledger_lines()` renders it to stderr, the console
summary, the markdown report and the run manifest, and escalates: `ZERO WORK`
when nothing was reviewed, `MOSTLY SKIPPED` under 50%, `ACCOUNTING GAP` when
the ledger itself does not balance (a reconciliation that silently balances
itself is a check that cannot fail).

**The exit-code hole this closes:** `_audit_exit_code`'s `barren` test keys on
DEFECTS, and *a repo nobody looked at has no defects to report*. FutureU
reviewed 1 of 57 and could still have exited 0. `candidates > 0 and
acted_on == 0` is now its own `EXIT_APPLIED_NOTHING` verdict, checked before
the apply test and independent of it.

### Verified 2026-08-21
- 857/857 unit tests OK (was 838 — 19 new in `ZeroWorkOvernightRunTests`).
- Reverting `_is_retryable` alone reddens the two rotation tests; the fix is
  load-bearing, not decorative.
- **Live** against the real `groq/groq/compound`: `max_tokens=16000` → the same
  400; `_parse_max_output_limit` → 8192; `_is_retryable` → True;
  `OpenAIProvider.structured(max_tokens=16000)` clamped and returned
  `{'summary': 'ok'}`.

### What this was NOT (checked, so nobody re-checks)
- **Not the `--yes` trap.** Every overnight `crash-<pid>.log` records
  `--apply --yes --allow-dirty --auto-clean`. Flags were correct.
- **Not launcher drift.** `--report-only`/`--dry-run` appear in both `.ps1`
  launchers only inside COMMENTS; no invocation passes them.
- **Not an empty `brain.json`** and not the missing-interpreter `None`/`False`
  gate.
- **Not AI Time.** The catalog was fresh (641 routes) and the runs spent real
  money getting real answers back.
- The final run's death at 08:19 was a **user-initiated reboot at 08:28**
  (System event 1074), which is why `status.json` holds live phases instead of
  the atexit obituary's `DIED ...`.

## Pool-first rotation is the DEFAULT provider (2026-08-19, owner order 2026-08-18)

`build_audit_providers`' free-first path now tries rotation FIRST: when the
owner named neither `--provider` nor `--model`/`--judge-model` and AI Time's
route catalog (`%LOCALAPPDATA%\AITime\routes.json`, `python -m aitime.catalog`)
has usable routes, the run gets ONE `RotatingProvider` (`flexfactor_rotation.py`)
that picks a different free route per call, walking QUOTA POOLS
least-recently-used (654 routes drain only ~24 ledgers — rotating model NAMES
spreads nothing). The FCC/ollama free pool is the fallback when no catalog is
usable, and `_build_rotating_provider` PRINTS why (`[rotation] not rotating:
...`) — never a silent fallback. `AI_ROTATE=off` restores prior behaviour
outright; pinning one route is `AI_ROTATE_PIN` / the state-file pin —
deliberately NO new CLI flag (launcher-drift trap). `--economy` maps to the
catalog's `strong` author tier; judging rides `light`.

- **Routes are filtered BEFORE the Rotator sees them** (`_route_unusable_reason`):
  unsupported api (gemini — no provider class), missing `auth_env` credential,
  paid cost class (rotation stays FREE-ONLY, `allow_paid=False` — never
  auto-promote), non-loopback in `--model-mode local`. An unbuildable route
  left in would be selected, fail, and burn a cooldown — across 600+ routes
  the first sweep becomes an error tour. Exclusions are counted and printed.
- **The judge sentinel must never reach the wire.** `_judge()` passes
  `model=provider.judge_model`; on a RotatingProvider that is
  `ROTATING_JUDGE_MODEL` ("rotating-judge"), a TIER REQUEST, not a model id.
  `RotatingProvider.structured()` translates it to the judge tier and strips
  the kwarg. `judge_model` stays FIXED at the sentinel (`.model` mutates to the
  last route's real id) so the translation cannot collide with a real id.
- **Catalog-free models bill $0 via `_FREE_ROUTE_MODELS`**, populated only by
  `_rotation_route_provider` from the catalog's `cost_class`. The branch sits
  AFTER the pricing table (a priced model can never dodge `--max-cost` by also
  appearing in a catalog — Sol-finding shape) and BEFORE the fail-closed
  premium default, which otherwise let a $0 run exhaust the budget on phantom
  spend and REFUSE free work.
- **Route providers are the REAL provider classes** (`OpenAIProvider` via the
  same `object.__new__` client-injection pattern as `_openai_rescue_provider`,
  `OllamaProvider`, env-configured `AnthropicProvider` for the FCC route —
  never a direct api.anthropic.com client), so the egress gate, budget guard
  and output ceilings all apply to rotated calls. Cloudflare at Groq/Cerebras
  blocks default-library UAs (1010, looks like a revoked key) — the injected
  client sends a real product UA.
- **Test hygiene:** `flexfactor_tests.py` redirects `AI_ROTATE_CATALOG` /
  `AI_ROTATE_STATE` to the tempdir at import — this dev machine's REAL catalog
  would otherwise flip every `build_audit_providers` test into rotation mode
  and stamp the owner's shared rotation state. `TestSessionIsolationTests`
  guards it.

### "stale catalog" was printed on EVERY rotated route (2026-08-19)

A live 5-program run emitted ~30 lines of
`[rotation] openrouter/... [free-tier/light] stale catalog`, burying everything
else. `Selection.describe()` appended the note per ROUTE while staleness is a
fact about the catalog **FILE**, and `_announce` prints once per distinct route —
so the more work a run did, the more it repeated itself.

- `describe()` no longer renders `catalog_stale`. The FIELD stays (consumers
  need the answer); only the per-route rendering is gone.
- `flexfactor_rotation.catalog_staleness_note()` is the one-per-run replacement
  and is **actionable**: it names the file, its age in hours, the limit, and the
  exact command — `python -m aitime.catalog`. `_build_rotating_provider` prints
  it once, guarded by `_claim_stale_warning()` / `_ROTATION_STALE_PRINTED` keyed
  on the catalog PATH so a batch run says it once, not once per program. That
  claim is **LOCKED** (unlike the older `_ROTATION_REASON_PRINTED` beside it): a
  `--parallel` batch builds providers from several threads, and an
  unsynchronized check-then-add reintroduces duplicate warnings in miniature.
- **It is printed BELOW the "no usable route" bail-out**, because the sentence
  says a stale route "can still be selected" — said above the bail-out it would
  describe a rotation that never happens.
  `test_the_warning_is_not_claimed_when_no_route_is_usable` pins the placement.
- **Neither dishonest fix is allowed.** FlexFactor must never RUN that refresh
  (AI Time owns the catalog; silently regenerating another program's state is
  not this tool's call), and the warning must never be SUPPRESSED (a stale
  catalog can still be offering a route whose quota died hours ago, which
  otherwise becomes an unexplained error tour). Both are pinned by tests.

## Dismissing a panel from the dashboard (owner request 2026-08-19)

> "give me an 'x' to delete a program out of flexfactor, like in the situation
> of Iplay just now, to leave room for the graphics of the other programs."

Five programs ran concurrently; IPlay STOPPED early on a red baseline and its
dead panel kept a fifth of the window. The "x" lives in
**`flexfactor_dashboard.py`** — the surface `_launch_dashboard` actually starts,
and the only one with graphics to free. `ConsoleMeter` deliberately did NOT get
it: a one-line console meter has nothing to click and no space to reclaim.
(`flexfactor_dashboard_v2.py` / `flexfactor_web.py` are hand-run only and were
left alone; adding it there needs a different click model.)

- **VIEW ACTION, AND THE ARCHITECTURE IS WHY.** This file is a pure READER of
  `status.json` — it never opens it for writing, holds no handle on any audit,
  and the end-of-run summary comes from flexfactor.py's own in-process totals
  without consulting this module. So dismissing cannot kill a run, cannot mutate
  state, and cannot make a stopped program's outcome unreportable.
  `test_dismissing_never_mutates_or_drops_the_runs_own_state` pins it.
- `_DISMISSED` is **in memory only** — per dashboard session, nothing persisted.
- **A dismissed program that starts working again REAPPEARS.** Dismissal is
  recorded against the program's *activity signature* and holds only while that
  signature does. A finished/stopped program never moves again, so it stays gone
  (the point of the request); one that resumes comes straight back, because this
  panel is the owner's only live view and hiding a working program would make
  the display lie. `activity_signature` deliberately EXCLUDES heartbeat fields
  (`updated`) — a re-serialized status entry is not activity, or the dismissal
  would bounce back on the next poll.
- Never a silent disappearance: the header always shows
  `N dismissed - click to show`, which restores everything.
- Covered headless by `DashboardDismissTests` and by
  `python flexfactor_dashboard.py --selftest`; the per-panel lambda must keep
  `p=p` binding or every "x" dismisses the last panel drawn.

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

### The `[no-op]` marker is SPLIT (2026-08-14) — it hid two opposite outcomes

Run 5: **19 no-ops against 41 fixes**, a ratio that says nothing, because one
marker covered both a *success of judgement* and a *failure of capability*:

- `[no-op: finding rejected]` — the author inspected the file and refused to
  change working code. Live: `SamErrorPanel.jsx` rejected a finding alleging a
  conflict between two `setStatus` calls that are in **separate component
  scopes**. Refusing was correct.
- `[no-op: no fix found]` — a real defect the loop could not land.
- `[no-op]` — the note did not clearly say. `_classify_noop` **falls back rather
  than guessing**, and a note matching both families is treated as unclear.

The author model already stated its reason; the information existed and was
being thrown away. **The rejected count is the run's REVIEW PRECISION signal** —
the number that says whether review is helping or manufacturing work that would
damage the program. `noop_stats` rides into the audit dict, the report
(`_noop_split_lines`, which also prints a rejected-vs-landed precision ratio) and
the run manifest.

**BOTH remain non-successes** in the anti-no-op accounting — `errors += 1` for
every branch, and the report says "none are successes". Letting "rejected"
become a success would recreate the 2026-08-11 defect the exit-code-3 rule
exists to prevent. A rejected finding is a defect in REVIEW, not a win for FIX.

## Large files are fixable: SHRINK THE UNIT, don't raise the ceiling (2026-08-16)

Live GrantFlow, mid-run: `[skip] src/pages/SmartMatcher.jsx: fix generation
failed (Model output hit the 16384-token budget (file too large to regenerate in
one response); raise max_tokens for this call.)`, repeatedly, with
**reviewed 8 / defects 155 / fixed 1 / errors 8** - the error count outrunning
the fix count because every large file was structurally unfixable.

The edit-block path exists precisely so output scales with the CHANGE, not the
file. It was being thrown away:

- **The demotion was backwards.** A budget overrun in EDIT mode was caught by a
  bare `except Exception` that printed `[edit-fallback]` and switched the file
  to WHOLE-FILE regeneration. Whole-file output is strictly *larger* than an
  edit, so the fallback was a guaranteed second failure. Every large file walked
  straight into `[skip]`.
- **The ceiling was gpt-4o's.** `OpenAIProvider.structured` clamped every model
  to a hardcoded `16384`, so newer models were silently capped at a fraction of
  what they can emit. `_openai_output_ceiling()` is now a longest-prefix lookup
  (`OPENAI_OUTPUT_CEILINGS`), and an UNKNOWN id still gets 16384 on purpose:
  over-requesting output is a hard API rejection that kills the call, while
  under-requesting costs one shrink-and-retry.
- **The detector was a string search.** `"token budget" in str(ex)` decided
  whether a file was oversized. It is now the TYPE `OutputBudgetError`, raised by
  both providers on `stop_reason == "max_tokens"` / `finish_reason == "length"`.
  The message still contains the old phrase so nothing older breaks, but no new
  code should match on text.

`generate_edits_shrinking()` is the fix: on `OutputBudgetError` it keeps the
**worst-severity half** of the findings and asks again, bounded by
`_EDIT_SHRINK_STEPS` (3) and stopping at one finding. A budget overrun means we
asked for too many changes at once - not that the file cannot be fixed. Dropped
findings are still reported and the until-clean loop picks them up next cycle.
Both the inline path and the `--fix-prefetch` path use it, or a prefetched first
attempt would hand the main thread an error that had never been retried.

**Accounting: `oversized` is a DISTINCT non-success, not an `errors` entry.**
The standing rule is that a non-success is never quietly reclassified, and this
does not break it: the file is printed loudly per-file AND in a one-line summary
at the end of the sweep, added to `notes`, and recorded in `oversized` - which
`audit_one_program` already folds into `errors_total` exactly once. What it stops
is the opposite dishonesty: "errors 8, fixed 1" read as eight failed fixes when
the truth was "this model cannot emit these files", which is a capability limit
with a different remedy (a larger-output model), not a defect in the fix loop.

The targeted-edit path is only safe because `_apply_edits` requires every anchor
to match **exactly once** - zero matches and two matches are both refused with a
reason, never applied blindly. That invariant is what makes shrinking safe to
lean on, and it has its own tests.

## `text=True` WITHOUT an encoding ends the whole audit on Windows (2026-08-16)

Live GrantFlow, mid-cycle:

```
GrantFlow: ERROR - unsupported operand type(s) for +: 'NoneType' and 'str'
totals: 0/1 program(s) OK | 0 defect(s) found | 0 file(s) fixed
```

preceded in the log by two subprocess reader-thread tracebacks ending in
`UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d`.

`_run` called `subprocess.run(..., capture_output=True, text=True)` with **no
encoding**, so Windows decoded child output with the locale codec (cp1252). One
smart quote or em dash from npm / vite / eslint raised inside subprocess's reader
**thread**; the exception died there, `cp.stdout` came back `None`, and the first
`stdout + "..."` downstream raised the TypeError that ended the run. Note the
shape: the crash message named a type error, and the real cause was three frames
and one thread away.

- All three `capture_output=True` sites now pass `encoding="utf-8",
  errors="replace"` (`_run` plus the two PowerShell shortcut readers).
- `_run` additionally coerces `stdout`/`stderr` from `None` to `""`. `_run`'s
  contract is "returns a CompletedProcess, never raises"; that is worthless if
  the fields can be `None` when every caller concatenates them.
- `test_every_capture_call_site_pins_an_encoding` greps the module source, so a
  new capture site without an encoding reddens the suite instead of waiting to
  kill a run on a machine with non-ASCII build output.

The other three tests drive REAL child processes emitting `0x9d` and utf-8
punctuation, so they exercise the decode path rather than asserting on a string.
Verified: reverting the encoding on `_run` alone fails 3 of the 5.

### The other half of the oversized-file fix: STAY ANCHORED

`[edit-fallback]` demoted a file to whole-file regeneration whenever its edit
anchors failed to apply. On a large file that converts a recoverable anchor
failure into a guaranteed `[skip] ... token budget`, because whole-file output is
strictly larger than the edit that just failed. `_whole_file_is_plausible()`
(model-aware, via `_provider_output_ceiling`, deliberately using a conservative
3.0 chars/token so it errs toward staying anchored) now gates the demotion: files
that cannot be regenerated in one response keep retrying ANCHORED EDITS, which
can succeed at any file size.

That change made a **latent crash** reachable, now closed: the attempt loop can
end on a `continue` path that never sets `outcome`, and `outcome[0]` on `None` is
a TypeError that would take down the audit. It is now named as `oversized` (file
too large to regenerate) or `skip` (attempts exhausted) and re-queued.

## A file key is an IDENTITY — canonicalize it (2026-08-14)

`rel` is not merely a path in this tool: it is the identity a file is tracked by
in `done_set`, the brain's `clean_files` skip set, and the findings map. **Two
spellings mean two files.**

`os.path.relpath` emits **backslashes** on Windows, while every other producer
normalizes to forward slashes (`_gap_to_finding`, the purpose-bridging list,
`clean_files`). Measured from the live GrantFlow run's log: of 28 per-file
outcome lines, **19 backslash / 9 forward**, and **eight files appeared under
BOTH spellings**, each processed twice in one run. `NotificationBell.jsx` and
`GrantPortalAssistant.jsx` were **`[fixed]` twice** — the second pass re-applying
findings the first pass had already resolved. The author model caught the rest:
*"already fixed in the current file content"*, *"the findings appear to describe
a different (broken) revision of this file than the one provided"*. That is how
a "fix" reintroduces a bug that was already repaired.

- `_canon_rel()` is the canonical form: backslashes → forward, whole leading
  `./` segments stripped. **Never `lstrip("./")`** — that strips a character SET
  and turns `.github/wf.yml` into `github/wf.yml`.
- `_enumerate_source_files` emits canonical keys.
- `_fix_files` folds its incoming `file_findings` through `_canon_rel` as
  defence in depth, **merging** both spellings' findings rather than letting one
  win — canonicalizing must never drop a real defect.

Side effect worth knowing: the purpose-first sweep ordering
(`pf = [f for f in purpose_files if f in set(files)]`) compared forward-slash
gap paths against backslash enumeration keys, so it silently matched **nothing**.
With canonical keys it actually orders the sweep now.

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

**That ordering was investigated and is NOT a defect (measured 2026-08-14).**
Comparing both paths on five truncated payload shapes: where extraction returns
something (cut envelope with a complete inner array; cut inside `summary` after
the array) it yields the *same* findings count salvage would; where extraction
returns `None` (cut mid-element, cut array after a leading `summary`) salvage
correctly runs and recovers the leading elements. Reordering would change
nothing. Do not "fix" it.

The probe did surface a **different** hazard, now guarded: `_extract_json_object`
returns the FIRST balanced `{...}` span, so `Here you go: {"ok":1}\n{"findings":[…`
hands back the **decoy**. That dict flowed on as a review with ZERO findings —
and an empty *successful* review marks the file CLEAN in `reviewed_clean`, so it
is never looked at again. **A silent false-clean is the worst outcome this tool
has.** `_check_structured_type` now raises when a dict carries **none** of the
schema's `required` keys, sending it down the existing retry/another-backend
path. Narrow on purpose: missing *some* required keys (findings but no summary)
is a normal partial answer and still passes, and a schema with no `required` can
never trip it. No production instance was observed — this is a guard against a
measured mechanism, not a fixed incident.

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
budget, default 150), `--fix-prefetch N` (parallel first-attempt fixes, default 3),
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
  dirty tree (the GrantFlow failure). NOTE: `--snapshot-dirty` is NO LONGER A CLI
  FLAG - passing it is argparse exit 2. What follows describes the MECHANISM
  (`_snapshot_dirty_tree`, default ON in prodready, OFF in audit), not a switch you
  can set. It commits the pre-existing changes verbatim as the sandbox branch's
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
  budget, default $150/program)
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
  **Third instance (found + fixed 2026-08-14, live Family Castle Clash):**
  `set_phase`/`record_cycle`/`record_spend` had the SAME fate — present in
  `flexfactor_runstate.py`, tested, called from nowhere — so every checkpoint
  carried `phase='starting', files_total=0, cycle=0, spend 0.0` for its whole
  run; the live run read as wedged-at-start 7 hours and 87 reviewed files in.
  Now wired at the real phase boundaries in `audit_one_program` (purpose
  baseline, cycle start, fixing, unit tests) + `files_total` after
  enumeration; `CheckpointPhaseWiringTests` reddens if the call sites are
  deleted. Same commit: test-gen retries ONCE at 64k on a 32k output-budget
  hit (`_gen_unit_tests` — the old code skipped the module while its own
  error said "raise max_tokens"), and `_wrap_path_map` salvages a bare
  path→contents map into a schema's single path-ish array property (the
  decoy guard's `{"ok":1}`/`{"ok":"yes"}` raises are regression-tested).
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
- SHIP TO MAIN (owner order 2026-08-10, extended to audit 2026-08-11, protected-
  trunk hole closed 2026-08-19): push+merge default ON for BOTH audit --apply and
  prodready — verified results go back to main automatically. Since sandbox
  branches were removed, "merge to main" is literally a commit on the branch the
  repo is already on plus `git push -u origin <branch>`; **nothing is ever
  force-pushed** (the old `--force-with-lease` note described the deleted sandbox
  topology, and `test_push_is_never_forced` pins its absence).
  **This doc claimed "protected mains fall back to a PR with auto-merge" for
  nine days while that was FALSE on the live path.** The fallback existed only
  inside the `prev_branch != branch` merge block, which is dead in the current
  topology — so a protected `main` that rejected the direct push left verified,
  cross-model-reviewed work committed LOCALLY, unmerged, with no PR and nothing
  asking anyone to finish it, under the single status fragment "branch push
  failed". Now the live push path does it: on rejection it publishes the same
  commits as `flexfactor/land-<sha8>` and calls `_gh_pr_automerge` to open a PR
  with auto-merge onto the trunk, so the work still reaches production through
  the repo's own required checks instead of around them.
  `test_a_rejected_protected_trunk_still_lands_through_a_PR` drives the real
  `_commit_and_sync` and asserts the landing-branch argv, the PR target and the
  absence of any force flag — verified to FAIL when the fallback is removed.
  `--no-push`/`--no-merge` (raw-argv checked) opt out. Every audit/prodready run
  applies (review-only removed 2026-08-11), so push+merge defaults are live on
  every run.
  The `--apply` confirmation banner was corrected in the same pass: it promised
  to "create a '<branch_prefix>*' branch", a safety buffer that has not existed
  since 2026-08-11. It now says the fixes are committed onto the branch each repo
  is already on and pushed to origin — which on a trunk means in production.

## Gotchas
- **Launchers must stay ASCII** (PS 5.1 + no-BOM = CP1252; em-dashes break strings).
- Desktop .lnk files (G:\One Drive\Desktop: FlexFactor / Scout a Program /
  Audit a Program) point HERE (`C:\Users\firer\flexfactor\`) — moving/renaming
  files means re-saving the shortcuts.
- Keys come from env (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) — never hardcode.
- Tests use the hermetic load pattern: `sys.modules["flexfactor"] = module`
  BEFORE `exec_module`, or dataclasses with future annotations die.

## 0.5.0 (2026-08-21): one runtime, every guard on a real call path

Read `docs/PURPOSE.md`, `docs/CURRENT_STATE_GAP.md`, `docs/ARCHITECTURE.md`,
`docs/THREAT_MODEL.md`, `docs/EXECUTION_CONTAINMENT.md`, `docs/EVIDENCE_MODEL.md`,
`docs/RECOVERY_AND_ROLLBACK.md` and `docs/migration-notes-0.5.0.md` before
touching any of this. The one-paragraph map:

- **Entry:** `flexfactor.run_cli()` is THE process entry (console script,
  `python -m flexfactor`, `flexfactor_run.py` shim, launchers via
  `$PSScriptRoot`). `--runtime-manifest` prints version/modes/module
  importability and the `wired` guard table; `flexfactor_entrypoint_tests.py`
  compares it across every entry AND a fresh-venv wheel install run from outside
  the checkout. `flexfactor_directed` is the single owner of the unfit-route
  patterns, skip-dir test and work-theme block (no launcher monkey-patching).
- **Execution broker (`flexfactor_sandbox`) behind `_run`/`_spawn`** for
  cmdpolicy classes `install`/`build`/`test` (pip/uv/poetry/cargo/go/dotnet/
  mvn/gradle/make are classified now - they used to fall through to `unknown`
  and bypass every gate). Windows = Job Object (process tree, memory, process
  count, CPU time - all verified live); **network is best-effort env poisoning
  only and is reported as such**. Untrusted repo on a host without an OS
  sandbox => rc 126 + `flexfactor_containment_blocked`, BEFORE anything runs.
  Authorize with `FLEXFACTOR_TRUSTED_REPOS`, `~/.flexfactor/policy.json`
  `trusted_repos`, or `--trust-repo` (run-level, recorded). The test suite
  trusts `gettempdir()` + this checkout at import. `python -c` / `node -e` /
  `--check` / `-m py_compile` are the only interpreter carve-outs, by argument
  shape (`_tool_authored_syntax_check`).
- **Owner WIP (`flexfactor_wip`)**: `--allow-dirty` snapshots uncommitted work
  to an ORPHAN ref `refs/flexfactor-wip/<sha>`, resets the tree to HEAD for
  the run, restores byte-for-byte in `audit_one_program`'s `finally`
  (fingerprint-verified; ref retained when restoration cannot be proven), and
  `_wip_publish_guard` fronts every push (audit AND scout `apply_integration`).
  TRAP: never `git clean` here - cmdpolicy marks it destructive; captured
  untracked paths are unlinked individually. The old "first commit on the
  sandbox branch" snapshot is deleted.
- **Partial output is failure evidence (`flexfactor_partial`)**: salvaged JSON
  is stamped at the provider sites (`_mark_partial`); `_judge` refuses
  clean/keep/approve; `review_file` raises `PartialOutputError` on an empty
  salvaged review (file stays INCOMPLETE); purpose criteria become UNKNOWN;
  the manifest carries `partial_output_events`.
- **Exact final review (`flexfactor_ledger`)**: the complete patch is reviewed
  in content-addressed chunks with a completeness ledger; any missing/blocked/
  partial chunk or a reviewer naming another commit blocks approval; HEAD
  moving after review revokes it. TRAP: the ledger's GitRunner takes a FULL
  argv - pass `_git_argv`, never `_git` (that produced `git git rev-parse` and
  would have revoked every approval; a test caught it).
- **Direct coverage (`flexfactor_coverage`)**: the `function-coverage` gate
  passes ONLY on direct tool evidence (coverage.py / c8 / lcov / go / jacoco /
  cobertura) or owner-declared blocked functions
  (`.flexfactor-coverage-blocked.json` {id: reason}); module execution never
  counts. Files above the parser cap get `analyzed-in-chunks` ledgers.
- **Journeys**: the Playwright engine lives in
  `flexfactor_assets/flexfactor_explorer.js` (package data, via
  `flexfactor_journeys`). `FLEXFACTOR_E2E_ROLES` (JSON), `_VIEWPORTS`,
  `_MAX_PAGES`; real submissions + failure cases only with
  `FLEXFACTOR_E2E_ISOLATED=1`, otherwise every skip is NAMED and the run is
  incomplete.
- **Purpose**: `gather_purpose_evidence` (manifests, docs, tests, schemas,
  routes, integrations, deploy, git history, PRs/issues) is cited in the
  inference prompt; `purpose_confidence` gates gap-driven fixes
  (weakly-inferred/unresolved => gaps reported, not bridged); the resume
  policy key is `POLICY_VERSION|purpose:<hash>`.
- **Tests:** `flexfactor_tests.py` (888) + `flexfactor_entrypoint_tests.py` +
  `test_flexfactor_{sandbox,wip,partial,ledger,coverage,purpose,journeys,trust}.py`
  - CI runs all of them on Windows + Linux and installs Playwright on Linux.
  Bash-tool TRAP for agents: large heredocs with backslashes/quotes get
  mangled - write patch scripts with the Write tool and run them.

## Purpose sight, error ledger, reasoning knobs (2026-08-23, first live IPlay run)

Read `recall rotation purpose ledger` in the vault for the measured story. The
mechanics a future change must not break:

- **The rotator fits the route to the call.** `flexfactor_rotation.CallIntent`
  (role author|reviewer|judge|vision, hard `needs`, `avoid_family`, `purpose`)
  rides on every rotated call. `_judge()` derives it from WHICH schema it is
  given (AUDIT_FINDINGS -> reviewer; ADVERSARIAL_VERIFY -> reviewer + honest;
  else judge); `generate_file_fix_edits` is an author. Fit is applied BEFORE
  pool-first selection; pool order itself never changes. Catalog routes carry
  `capabilities` (measured for local via `glimmer/tools/bench_battery.py`,
  declared for cloud); empty = unknown, never excludes. A reviewer auto-avoids
  the last author's family. `_set_rotation_purpose` runs once per program;
  purpose-derived needs bind to the VISION role only (a program that PRODUCES
  video must not narrow its code authors to image models -- learned live).
- **Results feed back.** `_report_route_quality(provider, role, signal)` with
  verified | rejected | noop | build_failed is called from `_fix_files`; yield
  orders routes INSIDE the LRU pool; a route below 0.25 yield after 5 attempts
  for a purpose is cooled down for that purpose only.
- **Error ledger.** `flexfactor_errors.py` writes `<run dir>/errors.md|json`
  after every record (phase, error, responsible file:line+source / program
  file / route, kind, suggested fix). Hooks: review retry/skip, fix skip,
  baseline STOP, setup refusals, autoclean, and every rotated route failure
  (`RotatingProvider(on_error=)`). A route failure is the PROVIDER's even
  when our HTTP frame is on the stack. The model fallback is never asked
  about route failures and cannot re-enter the ledger. Add a `SIGNATURES`
  row when a new failure shape is understood; never add "watch the log".
- **Reasoning knobs.** Local: `OllamaProvider._chat` sends `think=false`
  (`FLEXFACTOR_OLLAMA_THINK=1` restores); a reasoning-only reply raises
  `ReasoningBudgetExhausted`, never returns "". Cloud: `_reasoning_extra_body`
  sets OpenRouter `reasoning.effort=low` / NIM `chat_template_kwargs.thinking=
  false` on rotated OpenAIProviders (`FLEXFACTOR_CLOUD_REASONING=full`
  disables). Other backends untouched: an unknown body field can be a 400.
- **Test-boundary trap.** Tests monkeypatch `_judge` with two-argument fakes
  and use fake Rotators without `intent`; derive inside, pass `intent=` only
  when non-None. Sibling test modules pop `AITIME_STATE_DIR` -- glimmer tests
  re-isolate it per test.
- **Unfit additions:** `realtime`, `deep-research` (404 "not a chat model" /
  400 "Interactions API only", both seen live).

## The error box: every run reports its own failures, per program (2026-08-23)

> "Can you set the error reports for flexfactor as communication in a box I can
> see below each program being run?" - owner

The ledger already wrote the truth (`flexfactor_errors.py` -> `<run
dir>/errors.{md,json}`); what was missing was a way to SEE it during the run.
The live panel said `errors: 3` and nothing about WHAT.

- **One reader, three surfaces.** `flexfactor_errors` gained the reading half:
  `find_run_dir` (newest run for a program - the FALLBACK), `load_entries`,
  `counts_by_kind`, `where_of`, `ui_entries` (newest first, flat strings) and
  `headline`. The Tk dashboard, the phone dashboard and errors.md therefore
  cannot disagree; a second formatter would have been a second thing to drift.
- **`flexfactor_dashboard.py`** draws a box in the bottom `ERR_BOX_H` px of every
  panel: headline (`3 errors: 1 flexfactor-defect, 1 provider, 1 budget`), then
  per entry the three facts the owner asked for - what failed / `code:` which
  code is responsible / `fix:` what to do - and a click that opens errors.md.
  Reads are TTL-cached (`_ERR_TTL_S`); redraw runs at ~25fps and the standing
  rule is NO per-frame disk I/O.
- **`flexfactor_web.py`** carries the same rows in `/api/state` (`ledger`) and
  renders them as cards, so the box is on the phone too.
- **TWO ERROR NUMBERS, BOTH LABELLED.** The panel's counter is now
  `file errors: N` (files that errored in review/fix); the box is the run
  ledger (every recorded failure, provider retries included). Unlabelled, `2`
  next to `40` reads as a bug in one of them - the same two-scopes-side-by-side
  defect that made a 0.6%-reviewed program read as "100%".
- **A model-sourced suggestion renders as `(unverified)`.** The signature table
  is knowledge; a model guess is a guess, and the box says which it is.

### What made the box HONEST, and must not regress

- **Per-program routing.** `_ERROR_LEDGER` was a bare process global while
  `--parallel N` audits several programs in ONE process, so the last program to
  start owned every other program's errors. It is now `_ERROR_LEDGER_VAR`
  (a ContextVar) resolved by `_current_error_ledger()`, and the audit-path pools
  are `_CtxThreadPoolExecutor`, which copies the submitting thread's context
  into its workers (a plain pool worker starts with an EMPTY context - that is
  the whole bug). `flexfactor_ledger_routing_tests.py` pins both halves and
  includes a control test showing a stock executor mis-files.
- **`audit_one_program` publishes `run_dir` + `errors_ledger`** into the
  ProgressBus right after the ledger opens, so a viewer is told where to look
  instead of guessing from the program name.
- **`draw_frame` was lifted out of `_main`'s closure** so a test can draw a
  frame and READ THE CANVAS BACK. `flexfactor_dashboard_tests.py` asserts the
  painted text, and asserts geometrically that nothing paints outside its box.
  The three older dashboard greps in `flexfactor_tests.py` follow it there.
- **Truncation MEASURES the font** (`_measure`, cached `tkinter.font.Font`). The
  first screenshot of this box had the fix line painting over the next program
  because a characters-per-pixel guess is wrong at any DPI but the one it was
  tuned on. The cache rebuilds a Font whose Tk interpreter has died - otherwise
  every measure raises and it silently falls back to guessing.
- **Rows follow the geometry.** A fixed three entries ran out of the bottom of
  the box on a short window; the count is computed from the space available and
  the remainder is COUNTED in the footer (`+N more`), never silently dropped.

## Configuration surface (`.env.example`, 2026-08-23)

FlexFactor's own readiness rubric has a `config_documented` gate, and it was
FAILING on FlexFactor. `.env.example` now documents every environment variable
the runtime reads, with defaults and the measurement behind the dangerous ones
(the 307.8s free-queue floor, the 900s per-file fix ceiling). `.gitignore` now
ignores `.env` / `.env.*` (keeping the template) and `runs/`.
`flexfactor_config_surface_tests.py` keeps it true in BOTH directions: no
documented-but-never-read fiction, and no runtime variable missing from the
template (it found eight on the first run). It also asserts git actually ignores
`.env` - the outcome, not the presence of a line.

## FOURTH written-but-not-wired instance: purpose evidence never gathered (2026-08-23)

Measured on this repo, live: `_gather_from_folder` put

    [purpose evidence gathering failed: AttributeError: 'CompletedProcess'
     object has no attribute 'splitlines']

into the prompt **in place of the entire deterministic evidence block** -
manifests, docs, tests, schemas, routes, integrations, deploy, git history,
PRs/issues, every one of them CITED. `_PURPOSE_EVIDENCE_CACHE` stayed EMPTY, so
`_purpose_confidence_for` - which gates gap-driven fixing - was grading purpose
confidence on nothing.

Two injected runners, both breaking `gather_purpose_evidence`'s stated contract
("`git_runner(args, cwd)` / `gh_runner(args, cwd)` return stdout or None"; the
module calls `.splitlines()` on the result):

- `git_runner=lambda a, cwd: _git_argv(a, cwd)` returned a **CompletedProcess**.
  The FIRST git call raised inside the module and the whole gather aborted in
  the call site's own `except`.
- `_gh_runner` ran `_run(list(a), ...)`, **dropping the `gh` executable**. On
  this machine `_run(["pr", "list", ...])` executes `/usr/bin/pr`, the text
  paginator - it fails, and the result is filed as "GitHub evidence
  unavailable", so PR/issue signal never once reached an audit.

Both now go through `_stdout_or_none()` and the same `_git`/`_run` policy
chokepoint, with `["gh", *a]` spelled out. **Measured before -> after on this
repository: 0 sources -> 78** (50 commit subjects, 6 branches, 30 pull requests,
4 honest unknowns).

`flexfactor_purpose_wiring_tests.py` drives the REAL injection over a real
throwaway git repo. The module's own unit tests could never have caught this -
they inject their own correct fakes; only a test of THE WIRING can. All four
tests were verified to fail against the original code before being trusted.

**This is the fourth instance of the same trap** (after `flexfactor_runstate`,
the `set_phase`/`record_cycle`/`record_spend` group, and `_UI_EXPLORER_JS`).
When a module takes an injected callable, TEST THE INJECTION, not just the
module: grep every symbol it exports and drive the real call site once.

## Structural (cross-file) fixes (2026-08-23)

Owner order: "It sure would be nice if flexfactor would fix errors it found."
A `[no-op: no fix found]` defect now gets ONE bounded cross-file escalation
(`attempt_structural_fix`): the author model plans repo-contained operations -
new files, rewrites of files it was SHOWN (primary + an optional one-round
`need_files` read), renames - applied transactionally (all paths snapshotted,
`_gate_file` syntax gate on every written code file, optional cross-model veto
fail-open, full rollback on ANY failure). Bounds: 8 writes / 3 renames /
8 need_files per plan, 10 escalations per fix pass. `--no-structural-fixes`
disables. Classifier note: `_NOOP_NO_FIX_PATTERNS` now matches the canonical
"cannot be fixed in this file alone" wording its own schema asks for - it
previously classified as UNCLEAR, which would have starved this escalation.
Tests: `flexfactor_structural_tests.py` (offline, 10 tests).
