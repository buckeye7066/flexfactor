# FlexFactor 0.3.5 migration notes

No user action is required.

This release closes the next option-3 audit failure observed against GrantFlow:

- A semantic unit containing one file now uses the simpler per-file review
  contract instead of wrapping that file in the nested batch schema.
- One file-local failure no longer globally quarantines the only usable provider.
  FlexFactor tries the next unit, while its existing three-zero-batch circuit
  still stops a genuine outage quickly and fail-closed.
- Exhausted structured-stream retries now report the exact final SDK exception
  type and message. The run log no longer hides the cause behind a generic
  transport/deadline label.
