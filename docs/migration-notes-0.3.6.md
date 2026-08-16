# FlexFactor 0.3.6 migration notes

No user action is required.

Anthropic's live structured-output endpoint rejects the JSON Schema keyword
`maxItems`. FlexFactor 0.3.6 removes that unsupported keyword from the schema
sent over the Anthropic transport. The canonical schema is not mutated, and
FlexFactor still enforces the same maximum of three findings per file and eight
file verdicts per semantic batch in its own deterministic logic.

This resolves the explicit `invalid_request_error` captured by GrantFlow
option-3 run #20 without weakening review or allowing omitted files to be
treated as clean.
