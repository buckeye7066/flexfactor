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

## Rollback

- Everything is on branch `claude/portfolio-hardening-2026-07-18` with a single
  commit. To revert: `git checkout main` (or `git revert <hash>`).
- `~/.flexfactor/brain.json` is machine-local state, not in the repo. If you roll the
  code back, the old code reads the new-shape `clean_files` dict as `set(dict)` =
  its top-level keys (`policy`/`tool`/`files`), which match no real source files — so
  effectively nothing is wrongly skipped; a run just re-reviews the full set once. A
  `brain.json.corrupt` sidecar, if present, can be safely deleted.
