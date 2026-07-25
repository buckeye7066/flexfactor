# CLAUDE.md — FlexFactor

Local dual-provider code tool: refactor / scout / audit. Single-file core
(`flexfactor.py`, ~4.3k lines) + Tkinter dashboard + PowerShell launchers.
No app deployment — "prod" = the desktop shortcuts working.

## Run / test
```bash
pip install anthropic openai                   # only deps; there is NO requirements.txt
python flexfactor.py --file <f> --goal "..."   # refactor mode
python flexfactor.py scout --program <p>
python flexfactor.py audit --program <p>
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

## Map (all in flexfactor.py)
- Constants: `DEFAULT_MODELS` (author tier), `JUDGE_MODELS` (cheap tier),
  `ECONOMY_MODELS` (audit `--economy`: author = claude-sonnet-5 at $3/$15 vs
  Opus 4.8's $5/$25, near-Opus code quality; launcher defaults economy ON),
  `MODEL_PRICING` (incl. Claude 5 family), `CostMeter` (hard `--max-cost`
  budget, default $50/program)
- Providers: `AnthropicProvider` / `OpenAIProvider` (`complete`/`grade`/`structured`);
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
  a downed verifier accepts the fix but marks it `[unverified]` (never a clean pass).
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
