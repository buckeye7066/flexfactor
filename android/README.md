# FlexFactor Android app

Version 3.2 is a standalone Android control plane. Tapping the FlexFactor icon
opens the complete four-mode launcher; a PC, Termux, a loopback web server, and
an on-phone model server are not part of its runtime.

The phone sends authenticated requests to GitHub's API. GitHub Actions supplies
the disposable Python, Git, Node, browser, and build environment needed to audit
real repositories. This is materially different from pretending an APK can
safely execute every supported desktop toolchain inside Android's application
sandbox.

## First launch

Open **Credentials** and enter:

1. A GitHub token for the owner account with repository/workflow access.
2. Optionally, an OpenAI and/or Anthropic API key. GitHub Copilot and the hosted
   open-source model use no separate vendor key.

FlexFactor validates the GitHub account before saving anything. A pinned Ollama
runtime and coding model run in the disposable Actions runner by default; an
optional OpenAI key is validated live when saved. Values are encrypted at rest with a
non-exportable Android Keystore key. The app then encrypts them with the
repository's GitHub Actions public key and writes them to protected Actions
secrets; neither value is sent as a workflow input, URL, command argument,
artifact, or log field.

After setup, choose any writable public or private repository and use any original
mode. FlexFactor installs a small pinned caller workflow into that selected
repository and runs there, so private names, inputs, logs, and artifacts stay in
the private repository rather than crossing the public control repository.

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
GitHub Actions record. During Audit or Production Ready, **Steer this build**
queues an authenticated owner comment that the engine consumes at its next audit
phase boundary through the same containment and verification gates as desktop.

## In-app updates

Tap **Update**. FlexFactor checks the latest signed Android release, downloads
it over HTTPS, and verifies the package name, version, SHA-256, and signing
certificate lineage before opening Android's installer. Android requires the
user to enable **Allow from this source** once and confirm each installation.

Version 2.2.0 and later use the permanent release key, so 3.2 installs in place
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

- The Android app connects only to HTTPS GitHub and, when selected, OpenAI endpoints plus the
  HTTPS allowlist used by the signed updater.
- Credentials are masked in the UI, encrypted locally, and transferred to
  GitHub only through the official LibSodium-sealed Actions secrets API.
- Target code executes on an ephemeral GitHub-hosted runner, not on the phone.
- Public and private targets run from the pinned caller workflow installed in
  the selected target repository; private metadata remains in that repository.
- Workflow inputs are validated again on the runner before checkout or
  execution.
- The legacy `scripts/phone/` Termux engine remains available for command-line
  operators, but the Android app neither detects nor invokes it.
