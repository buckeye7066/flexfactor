# FlexFactor Mobile 3.5.0

FlexFactor Mobile is the native managed interface for all four FlexFactor
modes. It does not require a PC, a personal access token, Termux, or a local
model server.

## Sign-in and setup

Tap **Sign in with GitHub**, enter the displayed device code on GitHub's fixed
device-authorization page, and approve the registered **FlexFactor Mobile**
OAuth application. Access, refresh, and expiry values are committed as one
Android-Keystore-encrypted record.

OpenAI and Anthropic keys are optional. Each is validated independently, stored
encrypted on the phone, and sealed to a selected repository's Actions public
key before transmission. The managed service receives ciphertext and key IDs,
never plaintext provider credentials.

## Run contract

The home screen exposes:

1. **Refactor a file**
2. **Scout improvements**
3. **Audit and repair**
4. **Make production ready**

Refactor accepts up to 30 repository-relative files. The other modes accept up
to 30 repositories. The durable mobile orchestrator dispatches exactly one
target, waits for its terminal GitHub run, then dispatches the next.

Every request carries a stable UUID. If the app process stops after dispatch but
before recording the returned run ID, retrying with that UUID recovers the
existing workflow run across GitHub's paginated history and cannot dispatch a
duplicate. Queue state is synchronously committed before network work.

Every request uses the one best-available model ladder: strongest paid or
subscription capacity first, then lower paid tiers as credits are exhausted,
then free/local capacity. There is no provider, paid, free, or economy route
selector. The hosted fallback includes separate Qwen and DeepSeek code-model
families so the final reviewer need not reuse a candidate author's family.

Audit and Production Ready allow no more than six semantic passes:

- pass 1 covers the complete repository;
- the top-three competitor capability gate runs after pass 1;
- passes 2–6 cover exactly the preceding verified edit delta;
- no target or pass may overlap another.

## Completion

A successful code-changing run must pass the target's build and strongest
suite, deterministic evidence gates, and independent exact-commit review by a
model family that authored none of the candidate. It then must prove that exact
SHA is reachable from the repository's authoritative default branch.

An open PR, branch-only commit, missing test command, reviewer outage, partial
output, failed gate, or unproven publication remains incomplete in the app.
Run details and the bounded in-app artifact remain correlated to the request
UUID.

## Build

JDK 21, Android SDK 36, and Gradle 8.13 are required. The app targets Java 17:

```bash
gradle --no-daemon -p android testDebugUnitTest lintDebug assembleDebug
```

Pull requests receive an exact-commit debug artifact. A version change merged
to `main` creates the matching release tag and publishes the signed production
APK and update manifest.

## Release signing and updates

The protected `android-release` environment supplies:

- `ANDROID_KEYSTORE_BASE64`
- `ANDROID_STORE_PASSWORD`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`

Missing signing material fails the release; it never falls back to a debug key.
The updater checks the fixed release origin, package name, version, SHA-256, and
signing-certificate lineage before opening Android's installer.

## Boundaries

- The APK's product API is the fixed HTTPS FlexFactor Cloud origin.
- The service validates every request again and has no generic GitHub proxy.
- Target code runs on an ephemeral GitHub-hosted runner in the selected
  repository, not in the APK or cloud API process.
- The runner checks out the exact tagged engine and the selected target ref.
- The legacy Termux engine is not invoked by the app; browser launch endpoints
  are retired.
