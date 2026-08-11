# FlexFactor doctrine memory — provenance

These documents are the **owner's own definition of what "done" means** for the
Axiom application portfolio. FlexFactor reads them (via `flexfactor_purpose.py`)
so a run is measured against *the job each program was created to do*, not
against a generic quality checklist.

Installed 2026-08-11 on the owner's instruction: *"Maybe it would help to place
these in Flex Factor's memory so it has an idea of what I mean."*

| File here | Original (source of truth) |
|---|---|
| `portfolio-parallel-orchestration-directive.md` | `G:\One Drive\Desktop\Prompt2.docx` |
| `axiom-master-prompt-claude-code.md` | `G:\One Drive\Desktop\claude1p.docx` |
| `axiom-master-prompt-chatgpt.md` | `G:\One Drive\Desktop\chatgptp1.docx` |
| `axiom-master-prompt-cursor.md` | `G:\One Drive\Desktop\cursorp1.docx` |

The `.md` copies are plain-text extractions of the `.docx` originals. If fidelity
is ever in doubt, re-extract from the `.docx`. Do not edit these files to make a
program look finished — they are the yardstick, not the report.

## What FlexFactor actually consumes

Four things, and only these four:

1. **Status Vocabulary** (§4 of each master prompt) → `flexfactor_purpose.STATUS_VOCABULARY`
   and `NOT_PRODUCTION_READY_CLAIMS`. The owner ruled that "tests pass", "build
   passes", "merged", "deployed", "health endpoint returns 200", "works locally",
   "PR opened" are **not** Production Ready. FlexFactor's own status reporting now
   obeys that, which is the direct fix for its silent-overclaim bug class.
2. **Purpose and Acceptance Contract** (§5) → the schema of
   `flexfactor_purpose.PurposeContract`. The field list is the owner's, not ours.
3. **Definition of Production Ready** (§6 / Prompt2 §7) → the gate conditions in
   `PRODUCTION_READY_CONDITIONS`.
4. **Assigned Applications** (line ~261 of each master prompt) → the seeded
   per-program `Goal:` and `Acceptance:` text in `memory/purpose_contracts.json`.

The single sentence that governs the whole feature is Prompt2 lines 44-45:

> The goal is not to make every program resemble the same generic application.
> The goal is to make every program successfully perform the particular job it
> was created to perform.

## What FlexFactor deliberately does NOT consume

The master prompts also carry **orchestration mechanics** aimed at the human-driven
executors of an earlier effort (August 2026):

- "Do not launch subagents / no nested-agent or instance fan-out" (claude1p §2)
- The single `ACTIVE_APP` lock and one-program-at-a-time execution rule
- "Create one dedicated Cursor agent per program" (Prompt2 §4)
- Lane ownership ("Claude Code owns these nine applications and no others")

Those were instructions to *those executors*, not properties of the programs.
They are kept here as historical context and are **not** loaded as FlexFactor
runtime constraints — FlexFactor already has its own concurrency control
(`~/.flexfactor/audit-<slug>.lock`, `--parallel`), and adopting a portfolio-wide
lock would break the launcher's 5-program mode that the owner uses.

The line drawn: **purpose, acceptance, status vocabulary and definition-of-done are
ingested; agent-topology and lane-ownership rules are not.**
