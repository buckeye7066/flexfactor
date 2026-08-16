# Migration notes: self-reconciliation reliability 0.3.2

Version 0.3.2 makes audit findings and repository publication fail closed.

- Semantic findings now require an exact source excerpt, a reachable trigger,
  and an observable failure. Invalid line citations and ungrounded claims never
  reach the fixer or become a clean verdict.
- A failed semantic provider is quarantined for the run. Three consecutive
  zero-progress batches stop immediately with a resumable checkpoint instead
  of spending time on downstream UI and suite phases.
- Repository verification is transactional. A red or unavailable publication
  gate restores the last verified tree and creates no local commit or push.
- Git-visible dependency-bootstrap output is excluded from repair commits.
- Accepted competitor ideas use the canonical `problem` and `fix` fields, so
  they reach the normal repair pipeline.
- Red native-suite evidence includes the command exit code and output tail in
  console and evidence artifacts.
- Clearly marked fake/example credentials in docs and test fixtures are kept
  visible with an accepted contextual disposition; unresolved credential shapes
  continue to fail the security gate.

The clean-file policy version changed, so previously remembered clean files are
reviewed again under the evidence-bound finding contract.
