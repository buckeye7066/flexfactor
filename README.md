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
  integration PROPOSALS onto the branch the repo is already on (no adopt branch),
  verified by the project's own build with hard rollback; actually mutating the
  target needs the separate FlexFactor apply approval
  (`.flexfactor-apply-approval.json`). Builds/tests of target code run through
  the execution broker (see docs/EXECUTION_CONTAINMENT.md) and require a
  trusted repository on hosts without an OS sandbox.
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
  codebase (up to 10 programs in parallel). Dual-model adversarial review, per-file
  build gate, cross-model veto, unit + e2e test generation, converges with
  `--until-clean`, hard `--max-cost` budget (default $150/program), live Tkinter
  dashboard with a **per-program error box** (see below), persistent per-project
  memory ("brain") in `~/.flexfactor/`.
  Large files are reviewed in complete line-numbered chunks rather than truncated
  or omitted. Bounded files are grouped into semantic review batches, with an
  opt-in concurrency control for known-capacity single-provider runs
  (`--single-provider-review-workers`; serial by default), and with an exact
  response required for every requested path; missing rows and sustained provider
  outages fail closed instead of becoming clean results. Focused test generation
  covers behavior changed by the run without overwriting project tests, while the
  project's full native suite remains mandatory. Web targets are started locally
  and driven route-by-route/control-by-control; unavailable or incomplete execution
  is a blocker, never a silent pass.
  **Resume is automatic**: every completed per-file review is checkpointed
  (sha-keyed) as the sweep runs, and fixes commit per cycle - if a run dies
  mid-flow (crash, Ctrl-C, credits), re-running the same command picks up
  where it left off instead of re-paying for finished work. `--recheck`
  discards the memory and starts fresh.
  **Competitor research runs by default** (see below).

## Competitor research (audit + prodready, ON by default)

An audit used to be able to drive a program to 10/10 against its own acceptance
criteria while it still shipped less than anything its users could switch to.
Phase 1b closes that. Before the generic sweep, FlexFactor finds the program's
real competitors - commercial products AND inspectable open-source projects -
extracts the single most valuable adoptable idea from each, and judges that idea
against the program's OWN purpose contract.

- **Sources.** Scout's Repo Rewards search plus Firecrawl v2 (the official cloud
  uses `FIRECRAWL_API_KEY`; a custom `FLEXFACTOR_FIRECRAWL_URL` can be keyless
  or use its separately scoped `FLEXFACTOR_FIRECRAWL_API_KEY`), followed by
  self-hosted SearXNG, DuckDuckGo Lite, and Wikipedia fallbacks, plus GitHub
  repository search, which supplies the SPDX licence id. A cloud credential is
  never forwarded to a custom host. Every backend that fails is a NAMED skip in the report -
  "no reachable source" is reported as a research gap, never as "this program
  has no competitors", and a name nothing corroborates is marked `unverified`
  and never acted on.
- **The purpose contract is the authority.** A competitor idea that does not
  advance this program's stated job is REJECTED and reported as rejected. The
  goal is not to make every program resemble its competitors.
- **The licence decides the reuse mode, mechanically.** Verified-permissive =>
  `direct-code-reuse`. Copyleft/restricted, or a closed-source product with no
  inspectable source => `clean-room-from-documented-behavior` (public behaviour
  only, never the source). Unknown or unverifiable licence => `reference-only`.
  Source is never copied from an unknown or incompatible licence, and the mode
  plus the evidence URLs are recorded in the report and in the fix instructions.
- **Bounded action.** The top five competitors are considered by default, one
  strongest idea each. Only accepted, corroborated, licence-permitted,
  code-fixable ideas that map back to this program's purpose/acceptance
  contract enter the fix stream, capped by `--competitor-fixes` (default 5)
  and still subject to `--fix-severity`, the build gate and the adversarial
  verifier like any other change.
- Flags: `--no-competitors`, `--competitor-count N` (default 5),
  `--competitor-fixes N` (default 5).

**Repo Rewards endpoint (changed 2026-08-16):** local `localhost:3000` wins when
it is actually up; otherwise the production deployment is used automatically.
The endpoint in use is always printed and lands in the report. Opt out with
`--no-remote-repo-rewards` or `FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS=0`.

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
- **`--model-mode free|paid` (audit/prodready)** makes the cost/privacy boundary
  executable, and there are exactly two of them (owner order 2026-08-24).
  `free` (the DEFAULT) permits free routes only - the cloud free tiers (NVIDIA
  NIM, Gemini, Groq, Cerebras, OpenRouter free) plus loopback FCC/Ollama - and
  paid routes are FILTERED OUT of the catalog rather than merely ordered last,
  because ordering is a preference and only a filter is a promise. `paid`
  permits the owner's own Anthropic and OpenAI accounts and nothing else: the
  metered keys, the Claude subscription, and the local `claude`/`codex` CLI
  lanes - not reseller credits (OpenRouter, Cursor), not free tiers, not Ollama.
  An unavailable requested class fails explicitly instead of crossing the
  boundary. The retired `local` and `auto` spellings still PARSE and run as
  `free` with a warning, so a saved command never dies on argparse exit 2.
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
python flexfactor.py audit --program <path> --model-mode free [--program <path2> ...] [--parallel N]
python flexfactor.py prodready --program <path> --model-mode paid  # detect + install + fix + score
python flexfactor.py policy init                        # write deny-by-default owner policy
python flexfactor.py policy show                        # effective gate policy (file + env)
python flexfactor_tests.py                              # unit tests (no API keys needed)
python flexfactor_dashboard.py --selftest               # dashboard self-check
python flexfactor_dashboard_tests.py                    # draws a frame, reads it back
python flexfactor_web.py --print-url                    # same view, phone-reachable
```

### Android: standalone phone app

The Android app is in [`android/`](android/). Version 3.0 opens the complete
four-option FlexFactor launcher directly from the phone icon: Refactor, Scout,
Audit, and Production Ready. It requires neither a PC nor Termux/Ollama. The
signed APK is the native control plane and a disposable GitHub Actions runner
provides the multi-toolchain execution environment.

The first-launch Credentials screen verifies a GitHub token and OpenAI key,
encrypts both with Android Keystore, and installs LibSodium-sealed copies as
protected GitHub Actions secrets. Provider keys are never workflow inputs.
Choose a writable repository, select one of the four modes, and monitor the
exact correlated Actions run from the same app. See
[`android/README.md`](android/README.md) for the operating and release model.

The older `scripts/phone/` Termux engine remains an optional command-line path;
the Android app does not depend on, detect, or invoke it.

### Errors are reported IN the run, not in a log

Every run writes an error ledger - what failed, which code is responsible, and a
suggested fix - to `~/.flexfactor/runs/<run-id>/errors.md` (and `.json`). The
live dashboard shows the newest entries in a box **under each program being
run**, one box per program, and the phone dashboard (`flexfactor_web.py`) shows
the same thing from the same file. Nobody has to watch a log to find out what
went wrong:

```
3 errors: 1 flexfactor-defect, 1 provider, 1 budget      newest first
#3 flexfactor-defect / fix
BadRequestError: Unsupported parameter: 'max_tokens'
code: flexfactor.py:2489 structured()
fix: Newer OpenAI models reject max_tokens; send max_completion_tokens ...
                     click for all 3 in errors.md
```

The panel's `file errors` counter and the box are DIFFERENT scopes and are
labelled as such: the counter counts files that errored during review/fix, the
box counts every recorded failure including the provider retries rotation
absorbed. A suggestion that came from a model rather than the signature table is
prefixed `(unverified)`.

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


## Live operator steering

The authenticated web dashboard includes a **Steer this build** comment box for each active target app. A comment is durably queued, scoped to that exact program and repository, and picked up at audit phase boundaries. FlexFactor adds it to the target's purpose context, interprets it as concrete testable requirements, and routes feasible changes through the same containment, build, adversarial-review, and commit gates as every other repair. The dashboard shows whether each comment is pending, active, completed, or needs attention. Interrupted-run comments are reclaimed by the next run rather than silently lost.
