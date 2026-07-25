# FlexFactor

A local, self-improving code tool with three modes, driven from desktop shortcuts
or the command line. Dual-provider (Anthropic + OpenAI), build-gated, budget-capped.

## Modes

- **refactor** (default) - self-grading rewrite loop on one source file:
  rewrite -> grade -> accept at threshold.
- **scout** - profiles a program (folder / .lnk / URL / description), searches the
  local Repo Rewards service for useful open-source repos, LLM-judges each repo's
  benefit, and (by default) APPLIES the ADOPT-tier integrations on a
  `flexfactor/adopt-<repo>` branch, verified by the project's own build, with hard
  rollback on failure. `--report-only` to just get the report.
- **audit** - aggressive line-by-line defect hunt and auto-fix across a whole
  codebase (up to 5 programs in parallel). Dual-model adversarial review, per-file
  build gate, cross-model veto, unit + e2e test generation, converges with
  `--until-clean`, hard `--max-cost` budget (default $50/program), live Tkinter
  dashboard, persistent per-project memory ("brain") in `~/.flexfactor/`.

## Token economics (how cost is kept down)

- High-volume classification calls (review, grading, cross-verify, judging) run on
  the cheap JUDGE tier (`claude-haiku-4-5` / `gpt-4o-mini`); only code GENERATION
  uses the frontier author model.
- **`--economy` (audit)** authors fixes/tests with `claude-sonnet-5` ($3/$15 per 1M
  tokens vs Opus 4.8's $5/$25 - near-Opus code quality) instead of `claude-opus-4-8`.
  The build gate + cross-model veto + rollback safety net is unchanged, so a weak
  fix is vetoed and retried, never shipped. The Audit launcher asks and defaults
  economy ON; explicit `--model` overrides it. No-op on the openai provider.
- Fix generation returns minimal **search/replace edit blocks** (output scales with
  the size of the change, not the file), with automatic whole-file regeneration
  fallback when an edit anchor fails to apply. `--whole-file-fixes` restores legacy
  behavior.
- Cross-model fix verification always judges a **unified diff** (capped at 96k
  chars) - never two full copies of the file, which used to cost ~100k input
  tokens on whole-file rewrites.
- **Adversarial fix-verify loop (fable<->sol, default ON).** When a second provider
  is present, each build-passing fix is not merely spot-checked but ADVERSARIALLY
  verified: the secondary model is told to assume the author's fix is wrong and to
  hunt for any residual target defect, new regression, uncovered variant of the same
  bug class, or unhandled edge case. Its structured residual findings feed back to
  the author, which produces a corrected fix, and the loop re-verifies until the
  reviewer returns a genuinely CLEAN verdict or `--adversarial-rounds` (default 2) is
  exhausted (then the fix is rejected and rolled back - never silently kept). Unlike
  the legacy check this path is fail-CLOSED: if the verifier itself is unreachable the
  fix is accepted but marked `[unverified]`, so a downed reviewer is never reported as
  a clean pass. Use `--no-adversarial` for the legacy single-shot, fail-open veto.
- Anthropic system prompts are cache-marked (`cache_control: ephemeral`); note the
  minimum cacheable prefix on haiku-4-5/opus-4-8 is 4096 tokens, so these short
  prompts don't actually cache today (harmless - a miss bills normal price).
- **Secret/PII egress gate (default ON).** Before ANY repo text reaches a cloud
  model, a deterministic pre-send scan (`flexfactor_egress.py`) checks for
  private keys, vendor API tokens, credential-like assignments, secret env
  lines, and SSN-shaped PII. A finding REFUSES the call (fail closed, marked
  `flexfactor_egress_blocked` — the file is skipped, never sent). Escape
  hatches: `--redact` (mask the spans and send the rest), `--allow-sensitive`
  (send anyway), or allow single categories via `FLEXFACTOR_ALLOW_EGRESS` /
  `~/.flexfactor/policy.json {"allow_egress": [...]}`. The block tier is
  high-confidence patterns only, so lockfile hashes and `token = "sentinel"`
  test fixtures never block a legitimate audit.
- Review sweep stops at 35% of the budget cap so there is always money left to fix.
- The brain skips files marked clean in prior runs (`--recheck` to re-review).

## Run

```bash
python flexfactor.py --file <path> --goal "..."        # refactor
python flexfactor.py scout --program <path|lnk|url>
python flexfactor.py audit --program <path> [--program <path2> ...] [--parallel N]
python flexfactor_tests.py                              # unit tests (no API keys needed)
python flexfactor_dashboard.py --selftest               # dashboard self-check
```

Desktop launchers (in `G:\One Drive\Desktop`): **FlexFactor.lnk** (menu),
**Scout a Program.lnk**, **Audit a Program.lnk** -> the `.ps1` launchers here.

## Requirements

- Python 3.12+, `pip install anthropic openai` (each imported lazily - one key is enough).
- `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in the environment (never hardcoded).
- Scout mode auto-starts the Repo Rewards service from
  `C:\Users\firer\repo-rewards\scripts\launch.ps1` (expects it at `localhost:3000`).

## Gotchas

- **Keep the `.ps1` launchers ASCII.** PowerShell 5.1 reads no-BOM files as CP1252;
  a UTF-8 em-dash can decode as a stray quote and break parsing.
- **`_winify` is load-bearing.** Windows npm/npx/yarn are `.cmd` shims that
  `subprocess` can't find without PATHEXT-aware resolution; if an audit dies
  instantly with WinError 2, check `_winify` and its `shutil` import survived.
- `~/.flexfactor/` holds `brain.json` (per-project run memory) and `status.json`
  (dashboard bus) - machine-local state, not part of this repo.
