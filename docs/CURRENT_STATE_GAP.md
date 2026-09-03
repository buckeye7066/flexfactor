# FlexFactor current gap register

This register describes product limits that remain after the 0.6.2 / Android
3.5.3 / cloud 1.1.2 architecture change. Historical measurements belong in
release reports and are not current-state claims.

## Closed by this release

- Ten-target parallel execution is replaced by one durable 30-target sequential
  orchestrator.
- Audit/Production Ready are capped at six semantic passes: whole repository,
  top-three competitor gate, then exact verified edit deltas.
- Paid/free/provider routes are replaced by one strongest-paid-to-free
  availability ladder.
- Separate ladder instances share all author-family identities; a final reviewer
  from any author family is ineligible.
- Intermediate checkpoints cannot push. A changed run completes only after the
  exact independently reviewed SHA is proven on the authoritative remote
  default branch.
- Refactor, Scout apply, Audit, and Production Ready preflight publication
  prerequisites before model-backed mutation.
- Android has a durable sequential queue and idempotent crash recovery.
- The local Termux browser launch/provider endpoints are retired; managed mobile
  is the only phone launch product.

## Deliberate limits

1. **Windows network containment:** process-tree and resource controls are
   enforced, but raw-socket network isolation remains best effort. The runtime
   reports this and requires explicit trust where containment is insufficient.
2. **Target evidence quality:** a repository with no runnable build/test,
   incomplete direct behavior evidence, or an unavailable independent model
   family cannot be declared complete.
3. **Protected-branch approval:** FlexFactor never bypasses branch rules. A PR
   awaiting a required person or check remains incomplete.
4. **Provider exhaustion:** when every paid, subscription, and free/local
   allowance is unavailable, the run checkpoints and blocks rather than
   fabricating progress.
5. **Target-specific deployments:** FlexFactor proves its reviewed code reached
   the authoritative branch; deployment of an arbitrary target remains subject
   to that target's own declared release gates.

## Binding evidence

Static documentation does not close a release gate. The binding proof is:

- the complete local and CI test matrix on the candidate SHA;
- independent complete-patch review tied to that SHA;
- GitHub merge and remote-default ancestry evidence;
- deployed cloud health and OAuth device-flow evidence;
- a signed Android tag, APK, update manifest, and live end-to-end mobile run.

If any item is absent, status is BLOCKED or INCOMPLETE.
