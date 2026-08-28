# FlexFactor Android client

This directory is the source of the FlexFactor Android dashboard. Version
2.2.0 connects only to an authenticated engine on Android loopback. The engine
and the repositories it audits live in Termux on the same phone; a PC is not
required.

## Install the on-phone engine

Install **Termux**, **Termux:API**, and **Termux:Boot** from F-Droid. Do not use
the obsolete Play Store Termux build. In Termux:

```bash
pkg update -y
pkg install -y git gh
gh auth login --web --git-protocol https
mkdir -p "$HOME/phone-console"
gh repo clone buckeye7066/flexfactor "$HOME/phone-console/flexfactor"
bash "$HOME/phone-console/flexfactor/scripts/phone/setup.sh"
flexfactor-engine start
```

The last command sends the authenticated `127.0.0.1` URL to this app. Open
FlexFactor and approve the on-phone connection. If Android does not deliver
the broadcast, open the gear button and paste the URL printed by
`flexfactor-engine start`. Never paste that token into an issue, chat, or log.

Running audits also requires an on-phone provider. Re-run setup with
`WITH_SDK=1` and configure a supported cloud credential, or configure a
loopback Ollama provider. The dashboard and engine do not use the PC in either
case.

## Build

JDK 17, Android SDK 36, and Gradle 8.13 are required:

```bash
gradle --no-daemon -p android testDebugUnitTest lintDebug assembleDebug
```

CI publishes `flexfactor-android-debug-<commit>` for every pull request. Debug
artifacts are test builds, not production releases.

## Release signing and migration

Android accepts an in-place update only when the application id and signing
certificate match the installed app. This project retains the existing id,
`com.firer.console.flexfactor`, but the private key for the sideloaded 2.1.0
app is not in this repository. Recover that key before producing a release
build. If it cannot be recovered, uninstall 2.1.0 once and install a release
signed with a new, durably backed-up key; subsequent updates must use that same
key.

Never commit a keystore or its passwords. Release signing belongs in protected
CI secrets. Configure the `android-release` GitHub environment with
`ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and
`ANDROID_KEY_PASSWORD`, then push an `android-v*` tag. The release workflow
publishes the exact source commit, APK SHA-256, and signing-certificate digest.
Missing signing material fails the release; it never falls back to a debug key.

## Security boundary

- Only `localhost` or `127.0.0.1` endpoints with a dashboard token are
  accepted, and automatic handoffs require confirmation in the app.
- Cleartext traffic is disabled globally and allowed only for loopback.
- WebView file/content access is disabled.
- Main-frame navigation and every subresource are restricted to the paired
  origin.
- Android backup is disabled so the bearer token is not exported in backups.
