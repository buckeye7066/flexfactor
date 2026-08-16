# FlexFactor 0.3.8 migration notes

No checkpoint or target-repository migration is required.

- Paid OpenAI clients now make one request with a hard 300-second SDK deadline
  instead of inheriting long automatic retries. Set
  `FLEXFACTOR_OPENAI_CALL_TIMEOUT` to a positive value up to 1800 seconds only
  when an account is known to require a longer request window.
- Purpose-assessment sample failures are retained in the audit evidence. If the
  configured sample set is incomplete, or either the baseline or final purpose
  assessment is unavailable, the run cannot converge or exit successfully.
- Verified target-program fixes remain committed and resumable when this gate
  fails; rerunning after the provider recovers continues from their SHA-checked
  checkpoint.

The v0.3.7 single-provider review-worker and review/fix batch controls are
unchanged.
