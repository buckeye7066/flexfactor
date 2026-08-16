# Migration notes: audit reliability 0.3.1

This patch preserves the CLI, launcher option numbers, purpose contracts, and
checkpoint format. Existing audit checkpoints remain valid when the file hash and
review policy still match.

- Audit review now batches bounded files and requires exactly one result for every
  requested path. Omitted or duplicate results are retried with the next configured
  provider and never recorded as clean.
- Three consecutive semantic batches with no completed reviews stop the run with a
  resumable, non-zero failure instead of consuming the remaining budget or starting
  another convergence cycle.
- Purpose assessment retrieves evidence for each acceptance criterion instead of
  sampling the largest files. Insufficient evidence is reported as unknown.
- Generated unit tests target source behavior changed by the current run. They never
  overwrite existing tests and are removed if they fail the native test command.
  The complete native project suite remains a mandatory completion gate.
- Coverage evidence distinguishes module-load proof from direct function coverage,
  excludes test helpers from product totals, and follows local import closures from
  passing native and end-to-end tests.
