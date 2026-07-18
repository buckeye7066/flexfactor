# Changelog — Portfolio Hardening (2026-07-18)

Branch: `claude/portfolio-hardening-2026-07-18`. Local commit only (not pushed).

These changes harden FlexFactor's safety-critical defaults and correctness. Several
are **behavior changes** — read the migration notes.

## Safety-critical default changes (ACTION MAY BE NEEDED)

### Scout is now REPORT-ONLY by default
- Before: `flexfactor scout --program X` profiled, generated, committed, AND pushed
  integrations automatically.
- After: scout only writes a report. To make changes you must pass `--apply`, and
  scout prompts for an interactive `apply` confirmation (or pass `--yes`/`-y` to skip
  it in automation). On a non-interactive terminal without `--yes`, apply is refused.
- `--report-only` still works (it is now the default, kept for back-compat).
- Migration: append `--apply --yes` to any script/shortcut that relied on scout
  changing code. The Scout desktop launcher now defaults to "report"; choose "apply".

### Pushing is now opt-in (scout AND audit)
- Before: both modes pushed the work branch to `origin` by default (only `--no-push`
  suppressed it).
- After: nothing is pushed unless you pass `--push`. Commits still land on the
  dedicated `flexfactor/adopt-*` / `flexfactor/audit-*` branch locally.
- `--no-push` is retained as a back-compat no-op.
- Migration: add `--push` where you want the old auto-push behavior.

### Unknown model pricing now fails closed
- An unrecognized `--model` / model id is billed at the **highest known rate**
  ($10/$50 per 1M tokens) instead of the old Opus-tier guess ($5/$25), so
  `--max-cost` can never be silently overrun by a mispriced model. Add new models to
  `MODEL_PRICING` (and bump `PRICING_VERSION`) to price them correctly; a one-time
  stderr warning names any unknown id.

## Correctness / safety fixes (transparent)

- **Budget reservation before concurrent calls.** `CostMeter` now reserves estimated
  cost atomically before each prefetch/parallel generation, so background workers
  can no longer collectively blow past `--max-cost`.
- **Clean-file memory is content-hashed.** A file the brain marked "clean" is only
  skipped on a later run while its content hash is unchanged. Edit a previously-clean
  file and it will be re-reviewed automatically (no more permanent blind spot). The
  `clean_files` shape in `~/.flexfactor/brain.json` changed to
  `{"policy", "tool", "files": {path: sha256}}`; **old brain records are read but
  their clean sets are ignored (safe — those files get re-reviewed once)**. Bumping
  `POLICY_VERSION` invalidates the clean set intentionally.
- **brain.json is crash- and concurrency-safe.** Atomic write (temp + fsync +
  os.replace), in-process + cross-process locking, and corrupt files are moved aside
  to `brain.json.corrupt` instead of wiping memory.
- **No fabricated git branch.** `_git_current_branch` returns the real branch, the
  exact commit SHA on a detached HEAD, or `""` on hard failure — it never assumes
  `main`, so the post-run checkout can't switch you to the wrong branch.
- **`_run` failures can't be read as success.** All subprocess launch failures return
  a non-zero result tagged `flexfactor_launch_error`; callers gate on
  `returncode == 0`.
- **Prompt-injection fencing.** Untrusted third-party repo text and program context
  are wrapped in injection-resistant markers before being sent to a model.
- **`pyproject.toml`** added: `requires-python >= 3.12`, provider SDKs as pinned
  optional extras (`pip install .[all]`), `flexfactor` console entry point.

## Compatibility summary

| Change | Back-compat path |
|---|---|
| Scout report-only default | `--apply` (+ `--yes` for automation) |
| Push opt-in | `--push` |
| Unknown model pricing | add to `MODEL_PRICING` + bump `PRICING_VERSION` |
| brain `clean_files` shape | old records read; clean set re-derived safely |

No changes to: `_winify` Windows command resolution, edit-block default apply mode,
the dual-model review/veto/rollback safety net, or the PowerShell 5.1 ASCII launchers
(verified still ASCII + parse-clean).

## Follow-up round (2026-07-18) — parallel AUDIT path hardened

A review found the first round hardened SCOUT but left the parallel AUDIT path with
the same holes. Six more fixes (one regression test each):

### Audit is now REPORT-ONLY by default (ACTION MAY BE NEEDED)
- Before: a bare `flexfactor audit --program X` created a branch, wrote fixes, and
  committed automatically.
- After: audit only reviews + reports unless you pass `--apply` (with an interactive
  confirmation, or `--yes` for automation; a non-TTY without `--yes` stays
  report-only). The Audit desktop launcher now defaults to "report".
- Migration: append `--apply --yes` to any script/shortcut that relied on audit
  fixing code.

### Other fixes (transparent)
- Parallel review calls now reserve budget before spending, so a concurrent review
  sweep can't exceed `--max-cost` (previously it could overshoot by up to
  review-worker-count calls — measured $0.82 against a $0.30 cap at baseline).
- Fix-generation reservations now reflect the call's real output ceiling (edit 32k /
  whole-file 128k) instead of a ~1k guess.
- `_commit_and_sync` now checks every git checkout/merge result and stops the audit
  (raising `BranchStateError`) if it can't return to the audit branch — it can no
  longer silently continue and commit the next cycle onto the wrong branch.
- Unknown-but-lookalike model ids (`ft:gpt-4o-mini:…`, `my-gpt-4o-mini`,
  `azure/gpt-4o-mini`) now fail closed to the highest rate instead of inheriting the
  cheap base price; legitimate date/version suffixes (`claude-opus-4-8-20260101`)
  are still priced correctly.
- Audit review/fix/verify/test-gen prompts now fence the file's source/diff as
  untrusted data, so a hostile comment can't instruct the model to suppress findings.

No new user-facing flags beyond `audit --apply` / `audit --yes`. `audit --report-only`
remains (now the default). Push stays opt-in (`--push`).

## Round 3 (2026-07-18) — chokepoints

Four more fixes, done as single chokepoints so whole classes of call/write can't
regress (2 HIGH, including a real sandbox escape). All transparent — no new flags.

- **Budget reservation now lives in the provider call itself.** Every model call
  (fix, whole-file fallback, cross-verify, review, unit/e2e test gen, integration,
  refactor) reserves before spending, so nothing can exceed `--max-cost` — not just
  prefetched first attempts.
- **Sandbox-escape fix:** all model-generated file paths (unit-test gen, e2e specs,
  scout integration, fix-apply) now pass through a containment check that rejects
  absolute / drive-relative / UNC / `..` paths. Previously `audit --apply` could
  overwrite files OUTSIDE the target repo (on Windows an absolute `C:\…` path
  discarded the project dir). A scout integration that tries to escape now rolls back.
- **`_commit_and_sync` now checks git return codes:** a failed `git add` or a
  `git diff --cached` error hard-fails the audit instead of silently committing stale
  or unstaged content.
- **Retry feedback is now fenced** as untrusted data (it can contain source excerpts
  from build logs / reviewer comments), along with model-generated finding text — so
  it can't inject instructions into the author prompt.

## Round 4 (2026-07-18) — closing the last budget/safety holes

Four more fixes (2 HIGH), all transparent — no new flags:

- **OpenAI calls now cap output to the reserved amount.** `complete`/`grade` pass an
  explicit `max_tokens` matching their reservation, so the API can't bill more output
  than reserved (which had let concurrent workers exceed `--max-cost`).
- **A failed `git commit` now stops the audit** (raises) instead of being reported as
  text and continued past — a hook/identity/index failure is not a safe checkpoint.
  The final "committed" status is only claimed when the tree is confirmed clean.
- **Preflight health pings are now budgeted and lock-guarded:** they reserve/record
  against the shared cost meter (so `--max-cost` is a true hard cap) and the health
  cache is protected against duplicate concurrent pings.
- **The scout integration prompt now fences all untrusted/model text** — the first
  model's plan, the raw project source, `package.json`, and the file tree — not just
  the repo summary, so injected instructions can't drive unsafe in-repo edits.

## Round 5 (2026-07-18) — exhaustive sibling sweeps

Three sibling fixes of earlier work, done as full audits (all transparent):

- **Every provider method now reserves exactly what it requests.** `OpenAI.structured`
  reserved the un-clamped `max_tokens` while sending the 16384-clamped value; it now
  reserves the clamped value. All six provider methods audited: reserve == request cap.
- **All prompts whose output is written to disk now fence every untrusted field.**
  Scout integration now fences the program profile + improvement need (not just the
  repo/source); refactor mode now fences the current file, prior feedback, and the
  graded candidate. (fix-edits/whole-file/unit-test gen were already fenced; e2e-spec
  gen has no untrusted input.)
- **Preflight health pings now go through the provider adapter and are single-flight.**
  Pings use a new `ping()` adapter method (so they're budgeted + metered like any
  call), and concurrent audits issue exactly one ping per provider.

## Rollback

- Everything is on branch `claude/portfolio-hardening-2026-07-18` with a single
  commit. To revert: `git checkout main` (or `git revert <hash>`).
- `~/.flexfactor/brain.json` is machine-local state, not in the repo. If you roll the
  code back, the old code reads the new-shape `clean_files` dict as `set(dict)` =
  its top-level keys (`policy`/`tool`/`files`), which match no real source files — so
  effectively nothing is wrongly skipped; a run just re-reviews the full set once. A
  `brain.json.corrupt` sidecar, if present, can be safely deleted.
