# CLAUDE.md — FlexFactor

Local dual-provider code tool: refactor / scout / audit. Single-file core
(`flexfactor.py`, ~4.7k lines) + Tkinter dashboard + PowerShell launchers.
No app deployment — "prod" = the desktop shortcuts working.

## Run / test
```bash
python flexfactor.py --file <f> --goal "..."   # refactor mode
python flexfactor.py scout --program <p>
python flexfactor.py audit --program <p>
python flexfactor_tests.py                     # unit tests, no API keys needed
python flexfactor_dashboard.py --selftest
```

## Map (all in flexfactor.py)
- Constants: `DEFAULT_MODELS` (author tier), `JUDGE_MODELS` (cheap tier),
  `MODEL_PRICING`, `CostMeter` (hard `--max-cost` budget, default $50/program)
- Providers: `AnthropicProvider` / `OpenAIProvider` (`complete`/`grade`/`structured`);
  `_cached_system()` marks Anthropic system prompts cacheable; `_judge()` routes
  classification calls to the judge tier
- Audit loop: `run_audit` → `audit_one_program` (cycle loop, until-clean) →
  `_review_all` (parallel, judge tier, 35% budget frac) → `_fix_files` →
  `_commit_and_sync`; sandbox branch `flexfactor/audit-<slug>`
- `_fix_files` pipelines generation: `--fix-prefetch N` (default 3, 0=serial)
  first-attempt generations run in background threads while the current file is
  applied/gated/verified; retries + all tree writes/commits stay serial. Scout
  benefit-judging is parallel (8 workers). brain.json capped at
  `MAX_BRAIN_PROJECTS` (40) most-recent projects.
- Fix generation: `generate_file_fix_edits` (search/replace edit blocks,
  DEFAULT — output scales with the change) + `_apply_edits` (exact-unique-match,
  fails closed) → fallback `generate_file_fix` (whole file, 128k). Flag
  `--whole-file-fixes` = legacy. Cross-verify judges `_fix_diff` unified diff.
- Scout: `run_scout` → repo-rewards service (localhost:3000, auto-started from
  `C:\Users\firer\repo-rewards\scripts\launch.ps1`) → `generate_integration` /
  `apply_integration` with `_rollback`
- Subprocess chokepoint: `_run` + `_winify` (PATHEXT-aware; npm/npx are .cmd shims —
  removing _winify breaks every Node-repo audit with WinError 2). `_run` never raises.
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
