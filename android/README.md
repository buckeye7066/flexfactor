# FlexFactor Android app

Version 3.4 is the Android client for the managed FlexFactor Cloud control plane. Tapping the FlexFactor icon
opens the complete four-mode launcher; a PC, Termux, a loopback web server, and
an on-phone model server are not part of its runtime.

The phone sends authenticated product-level requests only to FlexFactor Cloud.
The managed service owns GitHub OAuth rotation, repository discovery, workflow
installation, dispatch, results, and steering. Under that control plane, GitHub
Actions supplies the disposable Python, Git, Node, browser, and build environment
needed to audit real repositories. The APK contains no direct GitHub API fallback.

## First launch

Tap **Sign in with GitHub**. FlexFactor displays a one-time GitHub device code,
opens GitHub's authorization page, and stores the resulting short-lived session
encrypted by Android Keystore. Access token, refresh token, and expiry are one
atomic encrypted record. The app refreshes that session automatically;
the user never creates or pastes a personal access token.

After sign-in, **Provider settings** optionally accepts:

1. An OpenAI and/or Anthropic API key. GitHub Copilot and the hosted
   open-source model use no separate vendor key.

FlexFactor Cloud validates the GitHub account before saving anything. A pinned Ollama
runtime and coding model run in the disposable Actions runner by default. Optional
provider values are encrypted at rest with a non-exportable Android Keystore key and
each newly entered value is validated independently against its provider before the
phone saves it and again before a run transmits it. Only keys required by the selected run are sealed on
the phone with the repository's GitHub Actions public key and written to protected
Actions secrets by the service, which receives ciphertext rather than plaintext
during dispatch. They are never workflow inputs, URLs, command arguments, artifacts,
or log fields.
The owner OAuth session is not copied into repositories: each run uses GitHub's
short-lived, repository-scoped `GITHUB_TOKEN`, including for Copilot requests.

After setup, choose any writable public or private repository and use any original
mode. FlexFactor installs a small pinned caller workflow into that selected
repository and runs there, so private names, inputs, logs, and artifacts stay in
the private repository rather than crossing the public control repository.
The cloud resolves the selected checkout ref before it installs a workflow or
writes a provider secret, so a deleted or renamed branch fails without mutation.

1. **Refactor a file** — improve a selected file toward a stated goal and open a
   publication PR when the verified output changes it.
2. **Scout improvements** — research useful competitive/open-source capabilities
   in report mode or emit gated integration proposals.
3. **Audit and repair** — run the full review/fix/test/publication path.
4. **Make production ready** — run the complete purpose, build, browser, test,
   competitive-gap, and readiness pipeline.

Audit and Production Ready can select up to ten repositories and launch their
independent workflows in parallel, matching the desktop multi-program path.
**Active and recent runs** preserves and polls each correlated run rather than
discarding the earlier run when another starts.

The latest run is polled by ID and survives activity recreation or process
restart. **View results and error ledger** reads a bounded, correlated result
artifact directly inside the app; **Open run details** opens the authoritative
compute record. During Audit or Production Ready, **Steer this build**
queues an authenticated owner comment that the engine consumes at its next audit
phase boundary through the same containment and verification gates as desktop.

## In-app updates

Tap **Update**. FlexFactor checks the latest signed Android release, downloads
it over HTTPS, and verifies the package name, version, SHA-256, and signing
certificate lineage before opening Android's installer. Android requires the
user to enable **Allow from this source** once and confirm each installation.

Version 2.2.0 and later use the permanent release key, so 3.4 installs in place
without uninstalling the existing app.

## Build

JDK 21, Android SDK 36, and Gradle 8.13 are required. The app itself still
targets Java 17 bytecode for Android compatibility:

```bash
gradle --no-daemon -p android testDebugUnitTest lintDebug assembleDebug
```

CI publishes an exact-commit debug artifact for every pull request. Merging a
versioned Android change to `main` creates the matching release tag and publishes the
signed production APK plus its update manifest.

## Release signing

Never commit a keystore or its passwords. The protected `android-release`
environment supplies `ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`,
`ANDROID_KEY_ALIAS`, and `ANDROID_KEY_PASSWORD`. A release fails closed when any
signing value is absent; it never falls back to a debug key.

## Boundaries

- The Android app's product API is the fixed HTTPS FlexFactor Cloud origin. It also opens
  GitHub's fixed device-authorization page and uses the signed updater allowlist.
- Credentials are masked in the UI and encrypted locally. Optional vendor keys
  use the official LibSodium-sealed Actions secrets API. The owner OAuth session is
  sent transiently as the cloud request bearer and is never persisted by the service.
- Target code executes on an ephemeral GitHub-hosted runner behind the managed control plane,
  not on the phone or the cloud API function.
- Public and private targets run from the pinned caller workflow installed in
  the selected target repository; private metadata remains in that repository.
- Workflow inputs are validated again on the runner before checkout or
  execution.
- The legacy `scripts/phone/` Termux engine remains available for command-line
  operators, but the Android app neither detects nor invokes it.
