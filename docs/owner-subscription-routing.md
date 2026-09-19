# Owner subscription-first routing

Owner decision: 2026-09-13. For owner-operated coding work, consume eligible
OpenAI/ChatGPT and Anthropic/Claude subscription capacity before metered API
keys. Retain existing API budgets and genuinely free/local fallback.

FlexFactor selects subscription tiers before metered tiers in its automatic
best-available mode. Factory Deck / Purpose Foundry prepend a subscription-only
rotation tier to the automatic model ladder and recheck it after quota resets.
Explicit free-only and diagnostic provider selection remain separate.

Use the owner's existing official Codex and Claude Code sign-ins. CLI child
processes omit inherited OpenAI/Anthropic API credentials and cloud-provider
overrides; parent SDK fallback credentials are not deleted or modified.
A configured API-key helper or provider billing/extra-usage setting must still
be checked independently; subscription access is not unlimited API credit.

These shared coding routers can work on any selected repository. This is NOT
an account-token export or a customer-facing inference gateway. Application
server SDK calls, images/audio/embeddings, and hosted customer requests remain
on their supported API integrations. No personal account secret belongs in Git.

## Private owner-only ChatGPT enrollment

Set FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY=1 and FLEXFACTOR_OWNER_CODEX_HOME to an absolute private official-Codex configuration directory on the owner installation only. FLEXFACTOR_OWNER_CODEX_MODEL defaults to gpt-6-astra. This mode uses the official Codex executable, verifies ChatGPT login, rejects API-labelled or incomplete output, preserves usage metadata, and removes metered routes even when allow_paid or a paid pin is requested. It never reads exported OAuth tokens or invokes an undocumented subscription HTTP endpoint. Other installations are unenrolled and retain their own provider credentials.

The worker requires Node and the official Codex CLI. Its stdin carries the prompt; no prompt or credential is put in command arguments. The sanitized subprocess cannot inherit API keys, plugins or user tools. Quota/authentication failure may use a configured free/local model, never metered owner fallback. Both CLI and rotating-provider entry points enforce the owner route policy.

Existing UI/API guidance is carried by the encrypted request-specific steering mailbox and consumed by the worker; no second prompt mechanism was added. Executed tests: 16 subscription-priority cases, 81 CLI/steering cases and 126 cloud tests passed. A real official-CLI call returned FlexFactor subscription OK in 5.37 seconds with subscription:codex, billing_mode=subscription and usage counters. This proof covers the local owner executor; it does not claim a hosted cloud runner has access to the owner's computer.
