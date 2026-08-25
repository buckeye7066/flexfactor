# Purpose and competitive-fit invariant

FlexFactor preserves the app's created purpose as the authority for every change. Competitor features are candidates, not requirements: a feature is selected only when current, corroborated sources show the behavior; the purpose contract says it materially helps this app; the app does not already provide it; adoption risk and mitigation are recorded; and the source/license decision permits either direct reuse or clean-room implementation.

## Fail-closed completion contract

A run cannot report convergence, finish its resumable checkpoint, or claim production readiness unless `flexfactor.product-invariants.v1` passes all of these gates:

1. Purpose analysis is enabled and grounded in an owner-authored or strongly inferred purpose.
2. Baseline and final purpose assessments completed without provider or sampling errors.
3. Every purpose criterion is fulfilled with no unknown or open gap.
4. Competitor research ran within the last 30 days and corroborated the configured target count with source URLs.
5. Every candidate has a purpose-fit, duplicate, provenance, wiring, verification, and adoption-risk decision.
6. Reuse mode is mechanically bounded by source ownership and license provenance; reference-only evidence cannot drive code changes.
7. Every selected capability entered the normal build-gated fix stream, changed its named target in that competitor phase, was wired through the described boundary, gained focused executable tests mapped to that exact source target, and passed the repository's full verification suite and exact-tree quality gates.

It is valid to reject every competitor feature when the recorded purpose-fit, duplication, provenance, or risk review supports that decision. It is not valid to copy a competitor roadmap, silently drop an accepted candidate, or report a selected capability as delivered when only research or scaffolding exists.

## Evidence surfaces

The invariant, every gate, selected and rejected capability ledgers, provenance URLs, implementation status, and remediation are included in the console summary, per-program report, immutable run manifest, result payload, and dashboard evidence. Disabled, stale, short, failed, or unverifiable research remains a named blocker rather than being interpreted as “no competitors.”
