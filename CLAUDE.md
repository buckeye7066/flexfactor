# CLAUDE.md — FlexFactor

Local dual-provider code tool: refactor / scout / audit. Single-file core
(`flexfactor.py`, ~4.3k lines) + Tkinter dashboard + PowerShell launchers.
No app deployment — "prod" = the desktop shortcuts working.

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
- `assess_readiness()` — 12 deterministic gates (no model calls). Status is
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
  branch prefix `flexfactor/prodready-`. Explicit `--report-only`/`--dry-run`
  still win — checked against RAW argv because they share a dest with `--apply`.
- **Dirty-tree walk-away (2026-08-10).** Prodready no longer faceplants on a
  dirty tree (the GrantFlow failure): `--snapshot-dirty` (default ON in prodready,
  OFF in audit) preserves pre-existing changes as a private ORPHAN commit under
  `refs/flexfactor-wip/*` (`flexfactor_wip.capture_orphan_wip_snapshot` via
  `_snapshot_dirty_tree`) so they are NEVER an ancestor of a pushed/merged
  sandbox branch; the worktree is hard-reset to the clean base so per-cycle
  `git add -A` commits contain only FlexFactor's changes. Every cleanup path is
  fail-closed: the empty-branch drop paths call `_drop_branch_restoring_wip`
  which restores the orphan snapshot back onto the original branch as plain
  uncommitted changes BEFORE `branch -D`; if the restore fails the WIP ref is
  PRESERVED (never delete the only ref holding owner WIP). Snapshot-commit
  failure refuses to run + unwinds. `_commit_and_sync` calls
  `publish_allowed` before push/merge. `--allow-dirty` keeps legacy sweep-it-in
  behavior and wins over snapshot mode.

Trap: `MAX_REVIEW_BYTES` had to go 400k -> 600k because this file outgrew it
again; when it does, `flexfactor.py` silently drops out of its own audit
(`test_flexfactor_can_review_itself` is the guard).

## Map (all in flexfactor.py)
- Constants: `DEFAULT_MODELS` (author tier), `JUDGE_MODELS` (cheap tier),
  `ECONOMY_MODELS` (audit `--economy`: author = claude-sonnet-5 at $3/$15 vs
  Opus 4.8's $5/$25, near-Opus code quality; launcher defaults economy ON),
  `MODEL_PRICING` (incl. Claude 5 family), `CostMeter` (hard `--max-cost`
  budget, default $50/program)
- Providers: `AnthropicProvider` / `OpenAIProvider` / `OllamaProvider`
  (`complete`/`grade`/`structured`/`ping`). Ollama (2026-07-25, ULTRAPLAN
  1.2) = LOCAL-ONLY: refuses non-loopback `OLLAMA_BASE_URL` (fail closed),
  no egress gate (nothing leaves the machine), bills `ollama:<model>` ids at
  $0 via the `MODEL_PRICING["ollama"]` prefix entry, and
  `build_audit_providers` never adds a cloud secondary when primary=ollama.
  Defaults: author `deepseek-coder:33b`, judge `llama3.2:latest`;
  `_cached_system()` marks Anthropic system prompts cacheable; `_judge()` routes
  classification calls to the judge tier
- Audit loop: `run_audit` → `audit_one_program` (cycle loop, until-clean) →
  `_review_all` (parallel, judge tier, 35% budget frac) → `_fix_files` →
  `_commit_and_sync`; sandbox branch `flexfactor/audit-<slug>`
- Adversarial fix-verify (fable<->sol): with a 2nd provider present, `_fix_files`
  runs `_adversarial_verify_fix` (`ADVERSARIAL_VERIFY_SYSTEM`/`_SCHEMA`) instead of
  the legacy single-shot `_cross_verify_fix`. The reviewer ASSUMES the fix is wrong,
  hunts residual/new/uncovered defects, and each `needs_work` verdict feeds the
  residual list back so the author re-fixes; loops until a genuinely CLEAN verdict
  or `--adversarial-rounds` (default 2) is hit (then reject+rollback). Fail-CLOSED:
  a downed verifier REJECTS the fix and rolls the candidate back (pre-change
  tree restored; never keeps an UNVERIFIED fix from a verifier outage). Partial/
  truncated structured salvage is stamped `partial=true` and also cannot
  authorize CLEAN/READY/merge/push.
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
  `MAX_REVIEW_BYTES` raised 300k->400k so flexfactor.py (now ~310k) stays reviewable.
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

## Gotchas
- **Launchers must stay ASCII** (PS 5.1 + no-BOM = CP1252; em-dashes break strings).
- Desktop .lnk files (G:\One Drive\Desktop: FlexFactor / Scout a Program /
  Audit a Program) point HERE (`C:\Users\firer\flexfactor\`) — moving/renaming
  files means re-saving the shortcuts.
- Keys come from env (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) — never hardcode.
- Tests use the hermetic load pattern: `sys.modules["flexfactor"] = module`
  BEFORE `exec_module`, or dataclasses with future annotations die.
