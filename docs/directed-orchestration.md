# Directed orchestration (shared rule)

**One open issue. Coding models only. Never wander.**

This rule is identical across:

- **Factory Deck** — `buckeye7066/local-ai-factory` (server + agents)
- **Purpose Foundry** — same repo, `src/server/foundry/` (uses the same provider registry + WorkTheme)
- **FlexFactor** — `buckeye7066/flexfactor` (`flexfactor_directed` / `flexfactor_run.py`)

## The rule

1. Every concurrent / rotated model call on one job carries the **same theme** and the **same open issue**.
2. Catalog routes that cannot author or review code (prompt-guard, TTS, vision-only, embed, content-safety, etc.) are **excluded** before rotation picks them.
3. Failure repair never targets generated trees (`node_modules/`, `dist/`, `build/`, `.next/`, `out/`, `coverage/`). Edit the source that produced them.
4. Correct the failing line. Do not ship launchers or boundary-only patches as a substitute for the broken path.

## How each surface applies it

| Surface | Mechanism |
|---|---|
| Factory Deck | `workTheme.ts` + `themeBind` / `underWorkTheme` at start/resume/epic; `ThemedProvider` on `resolveLive`; `routeFitness` in `filterRoutableCatalog` |
| Purpose Foundry | Same registry (`app: "purpose-foundry"`); Crucible and other LLM stations run under `underWorkTheme`; FlexFactor process default is `flexfactor_run.py` |
| FlexFactor | `flexfactor_directed.install` via `flexfactor_run.py` (and desktop launchers); `_directed_work_theme_block`; skip-dir failure paths; unfit-route filter. Prefer `python flexfactor_run.py` over bare `flexfactor.py` so install is always live. |

## Obsidian

Cross-agent decisions live in `G:\Obsidian Vault\AI Bus\messages.jsonl` (topics `#factory-deck`, `#flexfactor`, `#local-ai-factory`). Durable note: `Correct-not-avoid-Factory-Deck-FlexFactor-2026-08-20`.
