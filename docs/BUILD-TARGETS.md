# Build targets

```sh
python scripts/build-target.py --target auto --dry-run
python scripts/build-target.py --target android --dry-run
python scripts/build-target.py --help
```

This Python-only dispatcher requires no Node.js. Auto reads the actual operating
system (Windows, macOS or Linux); an explicit target selects the recipient.
`--host win32|darwin|linux` is available only with `--dry-run` for testing.
Unsupported targets exit nonzero, including dry runs; successful plans print
JSON with the exact command, artifact format and output. Remove `--dry-run`
to execute a supported build. Builder failures stop immediately.

## Inventory and capabilities

Existing pyproject.toml setuptools backend and CI wheel command; Android Gradle project with documented Gradle 8.13, JDK 21, Android SDK 36 and build-tools 35.0.0. Python package output is a wheel, not a Windows EXE or macOS app. Android output is a debug APK, not a signed store release.

| Target | Build capability |
| --- | --- |
| windows | Python wheel (requires Python >=3.12 and separately installed runtime dependencies) |
| android | Android debug APK |
| web | Unsupported: no configured package builder |
| ios | Unsupported: no configured package builder |
| macos | Python wheel (requires Python >=3.12 and separately installed runtime dependencies) |
| safari | Unsupported: no configured package builder |
| linux | Python wheel (requires Python >=3.12 and separately installed runtime dependencies) |

Wheel builds require pip, setuptools >=68 and wheel already installed. Build isolation and package-index access are disabled; this does not install dependencies. Android builds use the installed Gradle and offline cache, with SDK auto-download disabled. Release signing and deployment remain separate. A debug APK can be transferred to an Android device for deliberate installation; Python wheels require a Python environment on the recipient. Neither output is an iOS app or Safari extension.

The dispatcher never runs setup/launch/update scripts, starts a service, invokes
an AI model, renders media, installs tools, deploys, or copies owner settings,
credentials or data into a new source archive. Existing configuration and user
data are not migrated or overwritten. Readiness and runtime compatibility are
separate from a build plan.

Offline behavioral verification: `python -m unittest test_build_target -v`.
