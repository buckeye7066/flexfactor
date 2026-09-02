# FlexFactor Cloud threat model

## Assets

- GitHub OAuth access and refresh tokens.
- Optional OpenAI and Anthropic keys.
- Private repository names, refs, run metadata, artifacts, and steering instructions.
- The caller-workflow and engine-version pins that determine which code runs.

## Trust boundaries and controls

1. **APK to cloud.** Only HTTPS is accepted. Requests and responses are bounded and `no-store`.
   Authentication is an OAuth bearer session encrypted at rest by Android Keystore.
2. **Cloud to GitHub.** The origin and API version are fixed. Repository, ref, UUID, mode, provider,
   budgets, file paths, and steering text are validated again server-side. GitHub error bodies are
   not reflected wholesale.
3. **Provider credentials.** Provider keys remain encrypted on the phone until a run needs one. The
   APK uses a LibSodium sealed box with the repository public key, and the cloud forwards only the
   sealed ciphertext and key ID to GitHub. The service never receives a plaintext provider key.
4. **Artifacts.** Only the run-correlated `mobile-phone-*` artifact is selected. Redirects must be
   HTTPS and use GitHub's signed storage host families. The download is capped at 2 MiB and the OAuth
   bearer is not sent to the signed storage URL. The APK separately caps each extracted entry.
5. **Execution.** Target code runs inside the selected repository's ephemeral GitHub-hosted runner
   with the caller pinned to the Android release tag. It does not run in the cloud control-plane
   function or Android sandbox.
6. **Mutation.** A user confirms a run before dispatch. Workflow installation writes the exact pinned
   caller; protected branches fall back to a PR and fail with a pending state when repository rules
   require approval. No generic GitHub proxy endpoint exists.

## Deliberate non-features

- No server-side token or provider-key database.
- No arbitrary upstream URL, GitHub path, secret name, workflow name, or engine ref supplied by a
  client.
- No direct GitHub API fallback in the APK.
- No success response when OAuth rotation is not configured.
