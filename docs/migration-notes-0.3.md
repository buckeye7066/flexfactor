# Migration notes: evidence runtime

This change is additive. Existing `refactor`, `scout`, `audit`, `prodready`, launcher option numbers, purpose-contract registry, checkpoint files, and target-repository behavior remain valid.

- Fresh package installs now include every runtime sibling module, including `flexfactor_runstate`, dashboards, web dashboard, and `flexfactor_evidence`.
- Audit/prodready now create external evidence under `~/.flexfactor/evidence/` and events under `~/.flexfactor/events/`. No target migration is required.
- Completion is stricter. A previously green static review may now exit non-zero when tests collected zero cases, function/workflow evidence is incomplete, changed-file rescan or blast-radius analysis fails, or the independent reviewer cannot certify the exact commit.
- Playwright exploration records route/control/form rows, screenshots, a trace, accessibility checks, and performance smoke timings. Unnamed or destructive controls remain explicit blockers unless safely addressed.
- Consumers of the existing run manifest can ignore the new evidence keys. New consumers should use `evidence_run_id`, `final_commit`, `workflow_coverage`, `quality_gates`, and `evidence_artifacts`.
