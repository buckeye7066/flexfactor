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
   repository public key. The cloud preflights every required repository secret before any write,
   forwards only sealed ciphertext and key IDs to GitHub, and never receives a plaintext provider key.
4. **Artifacts.** Only the run-correlated `mobile-phone-*` artifact is selected. Redirects must be
   HTTPS and use GitHub's signed storage host families. The download is capped at 2 MiB and the OAuth
   bearer is not sent to the signed storage URL. The APK separately caps each extracted entry.
5. **Execution.** Target code runs inside the selected repository's ephemeral GitHub-hosted runner
   with the caller pinned to the Android release tag. It does not run in the cloud control-plane
   function or Android sandbox.
6. **Mutation.** A user confirms a run before dispatch. Workflow installation writes the exact pinned
   caller; protected branches fall back to a PR and fail with a pending state when repository rules
   require approval. The selected checkout ref must resolve to a GitHub commit before workflow or
   credential mutation begins. No generic GitHub proxy endpoint exists.

## Deliberate non-features

- No server-side token or provider-key database.
- No arbitrary upstream URL, GitHub path, secret name, workflow name, or engine ref supplied by a
  client.
- No direct GitHub API fallback in the APK.
- No success response when OAuth rotation is not configured.
