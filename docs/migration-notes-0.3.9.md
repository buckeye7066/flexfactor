# FlexFactor 0.3.9 migration notes

No checkpoint or target-repository migration is required.

- Incomplete semantic reviews now remain in a run-wide ledger instead of being
  forgotten when a later cycle narrows to files that were fixed.
- A later completed review clears its own ledger entry; unresolved entries are
  requeued with the next cycle and keep the run non-converged until proven.
- Reports, manifests, checkpoints, and the process exit code now use the same
  persistent incomplete-review count.

This closes a false-success path found while running GrantFlow through option 3:
model findings without valid source evidence were correctly rejected, but their
incomplete status could previously disappear after another file was fixed.
