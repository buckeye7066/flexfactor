# FlexFactor 0.3.7 migration notes

No checkpoint or target-repository migration is required.

- Paid option-3 audits now forward the provider selected in the Windows launcher
  and add `--single`, so selecting OpenAI produces an OpenAI-only run instead of
  silently falling back to another configured vendor.
- When a credential on a route permitted by `--model-mode paid` fails preflight,
  FlexFactor now reports the credential/credit failure instead of incorrectly
  claiming that paid mode excluded the route.
- The authored FlexFactor purpose contract now matches the executable rule that
  audit and prodready are real apply operations; the obsolete report-only
  acceptance criterion has been removed.
- README audit limits now match the executable defaults: ten programs and a
  $150 per-program hard cap.

All resume records remain policy-version compatible. Failed preflight creates no
new paid review work and does not relabel older evidence as current.
