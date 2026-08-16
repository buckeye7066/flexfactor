# FlexFactor

A local, self-improving code tool with four modes, driven from desktop shortcuts
or the command line. Dual-provider (Anthropic + OpenAI), build-gated, budget-capped.

## Install and verify

```text
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[all]"
.venv\Scripts\python flexfactor_tests.py
```

Linux/macOS use `python3.12 -m venv .venv` and `.venv/bin/python` for the same
commands. A fresh install includes the CLI, durable run-state module, dashboards,
purpose/readiness engines, and deterministic evidence runtime. Provider SDKs are
optional; `.[all]` installs both cloud adapters. Ollama/local mode requires no
paid service.

## Modes

- **refactor** (default) - self-grading rewrite loop on one source file:
  rewrite -> grade -> accept at threshold.
- **scout** - profiles a program (folder / .lnk / URL / description), searches the
  local Repo Rewards service for useful open-source repos, LLM-judges each repo's
  benefit, and writes a report. Proposal-only by default: `--apply` emits
  integration PROPOSALS on a `flexfactor/adopt-<repo>` branch, verified by the
  project's own build with hard rollback; actually mutating the target needs the
  separate FlexFactor apply approval (`.flexfactor-apply-approval.json`).
- **prodready** - point it at any program and walk away. Detects every toolchain
  in the tree (13 ecosystems, monorepo-aware), installs the project's own
  dependencies so the build gate measures the CODE rather than a missing
  `node_modules`, runs the full audit fix loop down to medium severity, then
  scores the result against a 13-gate production-readiness rubric and writes a
  scorecard naming exactly what still blocks release. Asks nothing beyond the
  program. Every run is REAL (owner order 2026-08-11): there is no
  report-only/dry-run mode - invoking prodready or audit means fixes get
  applied.
- **audit** - aggressive line-by-line defect hunt and auto-fix across a whole
  codebase (up to 5 programs in parallel). Dual-model adversarial review, per-file
  build gate, cross-model veto, unit + e2e test generation, converges with
  `--until-clean`, hard `--max-cost` budget (default $50/program), live Tkinter
  dashboard, persistent per-project memory ("brain") in `~/.flexfactor/`.
  Large files are reviewed in complete line-numbered chunks rather than truncated
  or omitted. Function-test generation covers every first-party module by default,
  and web targets are started locally and driven route-by-route/control-by-control;
  unavailable or incomplete execution is a blocker, never a silent pass.
  **Resume is automatic**: every completed per-file review is checkpointed
  (sha-keyed) as the sweep runs, and fixes commit per cycle - if a run dies
  mid-flow (crash, Ctrl-C, credits), re-running the same command picks up
  where it left off instead of re-paying for finished work. `--recheck`
  discards the memory and starts fresh.

## Executable completion evidence

Every audit builds a pre-change and exact-final code index. The final pass records
every tracked/relevant source file and content hash, symbols, imports, routes,
controls, configuration boundaries, changed-file rescans, and reverse-dependency
blast radius. It emits a machine-readable purpose graph; file/function/workflow
coverage ledgers; normalized fail-closed gates; SARIF; a secret-redacted event
trace; Playwright screenshots, accessibility/performance results and a trace for
web targets; and an independent review of the exact final commit.

Evidence is stored under `~/.flexfactor/evidence/<project>/<run>/`; resumable
state remains under `~/.flexfactor/runs/`. Missing tools, zero tests collected,
unexecuted material paths, an incomplete rescan, or a reviewer/commit mismatch
blocks completion instead of becoming a pass.

## Token economics (how cost is kept down)

- High-volume classification calls (review, grading, cross-verify, judging) run on
  the cheap JUDGE tier (`claude-haiku-4-5` / `gpt-4o-mini`); only code GENERATION
  uses the frontier author model.
- **`--economy` (audit)** authors fixes/tests with `claude-sonnet-5` ($3/$15 per 1M
  tokens vs Opus 4.8's $5/$25 - near-Opus code quality) instead of `claude-opus-4-8`.
  The build gate + cross-model veto + rollback safety net is unchanged, so a weak
  fix is vetoed and retried, never shipped. The Audit launcher asks and defaults
  economy ON; explicit `--model` overrides it. No-op on the openai provider.
- **`--model-mode local|paid|auto` (audit/prodready)** makes the cost/privacy
  boundary executable. `local` permits only loopback FCC/Ollama routes and
  disables paid rescue; `paid` permits credentialed vendor APIs and forbids
  free/local fallback; `auto` prefers local/free routes. An unavailable
  requested class fails explicitly instead of crossing the boundary.
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
  candidate is rolled back to the exact pre-change tree and rejected — never kept as
  an UNVERIFIED success, never committed as a clean pass. Use `--no-adversarial` for
  the legacy single-shot, fail-open veto.
- Anthropic system prompts are cache-marked (`cache_control: ephemeral`); note the
  minimum cacheable prefix on haiku-4-5/opus-4-8 is 4096 tokens, so these short
  prompts don't actually cache today (harmless - a miss bills normal price).
- **Local-only provider (`--provider ollama`).** All four modes can run
  against a local Ollama server (default `deepseek-coder:33b` author +
  `llama3.2` judge; override with `--model`/`--judge-model` to match
  `ollama list`). ZERO cloud egress: the provider refuses any non-loopback
  `OLLAMA_BASE_URL`, audit never silently adds a cloud cross-checker to an
  ollama run, and local tokens are metered at $0 (budgets unaffected). The
  egress gate is deliberately not applied - payloads never leave the machine.
  Quality vs frontier models is a real tradeoff; every safety net (build
  gate, rollback, deterministic scout gates) is unchanged.
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
python flexfactor.py audit --program <path> --model-mode local [--program <path2> ...] [--parallel N]
python flexfactor.py prodready --program <path> --model-mode auto  # detect + install + fix + score
python flexfactor.py policy init                        # write deny-by-default owner policy
python flexfactor.py policy show                        # effective gate policy (file + env)
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

## What "production ready" is allowed to mean

The verdict is a checklist with evidence, not a judgement call. 13 deterministic
gates (no model involved) cover buildability, tests, committed secrets,
dependency pinning, config externalisation, CI, docs, licence and a deployable
artifact. A gate is `pass`, `fail`, `na`, or `unknown` — and `unknown` is
deliberately not a synonym for `pass`: an unevaluated **critical** gate blocks
the verdict, because a property nobody checked is not evidence of safety.

Two claims the tool will refuse to make:

- If no build system is detected, or a detected one has no usable build command,
  it reports `Build verification: NOT AVAILABLE` and marks the run's fixes as
  NOT build-verified. It will not report the old vacuous pass.
- If a per-file syntax gate can't run (interpreter absent, blocked by policy,
  timed out), that file is `unverified`, never "broken" — the difference matters
  because "broken" causes a rollback that would discard a correct fix.

Supported toolchains: Node (npm/pnpm/yarn/bun), Deno, Python (pip/poetry/uv/pdm/
pipenv), Go, Rust, Java (maven/gradle), .NET, Ruby, PHP, Elixir, Dart/Flutter,
Swift, C/C++ (cmake/meson/make).

## Gotchas

- **Dependency lifecycle scripts are OFF during bootstrap.** Installing a tree
  runs that tree's `postinstall` hooks — third-party code executing on your
  machine because you pointed the tool at a repo. `--allow-scripts` opts in
  (some native packages genuinely need it).
- **Keep the `.ps1` launchers ASCII.** PowerShell 5.1 reads no-BOM files as CP1252;
  a UTF-8 em-dash can decode as a stray quote and break parsing.
- **`_winify` is load-bearing.** Windows npm/npx/yarn are `.cmd` shims that
  `subprocess` can't find without PATHEXT-aware resolution; if an audit dies
  instantly with WinError 2, check `_winify` and its `shutil` import survived.
- `~/.flexfactor/` holds `brain.json` (per-project run memory) and `status.json`
  (dashboard bus) - machine-local state, not part of this repo.

Architecture and operations: [execution architecture](docs/architecture.md),
[purpose-contract schema](docs/purpose-contract.schema.json),
[troubleshooting](docs/troubleshooting.md), and
[migration notes](docs/migration-notes-0.3.md).
