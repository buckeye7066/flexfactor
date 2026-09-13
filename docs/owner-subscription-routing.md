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
