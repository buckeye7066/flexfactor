# Migration notes: single-provider semantic recovery 0.3.3

Version 0.3.3 corrects provider-health classification measured in the GrantFlow
option-3 run.

- When only one semantic-review provider is usable, review units run serially so
  FlexFactor does not manufacture an outage by exceeding that provider's single
  capacity lane.
- A failed multi-file structured batch degrades to the simpler exact per-file
  review schema on the same provider before the provider is quarantined.
- Missing, invalid, or ungrounded per-file verdicts still fail closed and are
  never recorded as clean.
