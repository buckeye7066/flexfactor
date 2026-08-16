# FlexFactor 0.3.4 migration notes

No user action is required.

This release closes an option-3 audit orchestration failure observed against
GrantFlow. Purpose-gap and competitor research could independently prioritize
the same file, causing that path to enter a semantic batch twice. FlexFactor
then mistook its own duplicate-input rejection for a provider outage.

FlexFactor now canonicalizes and de-duplicates prioritized review paths while
preserving their first-seen order. The semantic review engine enforces the same
invariant defensively, and recovered resume findings cannot duplicate an item
already present in the first batch. A duplicate nomination is visible in the
run log and is processed exactly once; it no longer quarantines a healthy
provider.
