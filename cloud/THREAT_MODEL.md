# FlexFactor Cloud threat model

## Assets

- GitHub OAuth access and refresh tokens.
- Optional OpenAI and Anthropic keys.
- Private repository names, refs, run metadata, artifacts, and steering instructions.
- The caller-workflow and engine-version pins that determine which code runs.

## Trust boundaries and controls

1. **APK to cloud.** Only HTTPS is accepted. Requests and responses are bounded and `no-store`.
   Authentication is an OAuth bearer session encrypted at rest by Android Keystore. Each access
   token, refresh token, and expiry tuple is committed as one encrypted record, so an interrupted
   rotation cannot mix values from two generations.
2. **Cloud to GitHub.** The origin and API version are fixed. Repository, ref, UUID, mode, provider,
   budgets, file paths, and steering text are validated again server-side. GitHub error bodies are
   not reflected wholesale.
3. **Provider credentials.** Provider keys remain encrypted on the phone until a run needs one. The
   APK validates each newly entered value independently against the provider's fixed HTTPS origin
   before saving it and again before transmission. It seals only credentials required by the effective run policy with the
   repository public key. The cloud validates every supplied sealed credential before any write,
   writes phone keys under full-request-UUID secret names, never replaces an owner-managed
   canonical secret, and forwards only sealed ciphertext and key IDs to GitHub,
   and never receives a plaintext provider key. Phone-supplied secrets are recorded in the
   request's durable claim and deleted only after proven matching terminal completion or rollback
   by the invocation owning a definitively rejected/pre-dispatch request. A 404 or ambiguous
   dispatch failure retains credentials and the claim. Cleanup removes steering before secrets
   and the claim last; partial failure is retriable and prevents queue advancement.
4. **Artifacts.** The request UUID and numeric run ID must match the fetched FlexFactor run before
   listing artifacts. Only that request's exact `mobile-phone-<request_id>` artifact is selected. Redirects must be
   HTTPS and use GitHub's signed storage host families. The download is capped at 2 MiB and the OAuth
   bearer is not sent to the signed storage URL. The APK separately caps each extracted entry.
5. **Execution.** Target code runs inside the selected repository's ephemeral GitHub-hosted runner
   with the caller pinned to the Android release tag. It does not run in the cloud control-plane
   function or Android sandbox.
6. **Mutation.** A user confirms a run before dispatch. Workflow installation writes the exact pinned
   caller; protected branches fall back to a PR and fail with a pending state when repository rules
   require approval. The selected checkout ref must resolve to a GitHub commit before workflow or
   credential mutation begins. A repository variable atomically claims the request UUID before the
   non-idempotent GitHub dispatch call; retries recover that claim/run instead of starting a second
   workflow. No generic GitHub proxy endpoint exists.
7. **Recovery.** Recorded claims bypass history scans. Ambiguous claims are never age-deleted:
   GitHub variable updates/deletes expose no documented conditional ownership transfer. Missing
   or saturated evidence yields an explicit recovery-required state rather than redispatch.
8. **OAuth resource use.** Each process limits all device/poll/refresh handlers together to 120
   admitted requests per minute and eight concurrent exchanges, returning 429 and `Retry-After`.
   This bounds one instance only; it provides neither fleet-wide rate limiting nor an identity
   quota. Logs omit request bodies, credentials, query strings, and upstream exception details.

## Deliberate non-features

- No server-side token or provider-key database.
- No arbitrary upstream URL, GitHub path, secret name, workflow name, or engine ref supplied by a
  client.
- No direct GitHub API fallback in the APK.
- No success response when OAuth rotation is not configured.
