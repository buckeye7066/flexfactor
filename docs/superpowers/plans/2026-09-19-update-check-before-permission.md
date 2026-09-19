# Update Check Before Permission Implementation Plan

**Goal:** A current direct-install Android app can check for updates without granting installation permission.
**Architecture:** Keep the existing updater. Fetch and validate the manifest, compare the installed version, then request installation permission only for a newer release and before downloading it. MainActivity handles an explicit callback; Android still confirms every installation.
**Tech stack:** Existing Java Android application and Python repository regression suite, no new dependencies.
**Spec:** Observed signed3.5.13 Update-button behavior and the existing explicit-update/no-silent-install contract in test_android_standalone.py.

## Constraints
No OS permission changes, no token/session changes, no weaker download/hash/package/signature checks, no Play-store in-app updater, no update version bump independent of the coordinated Scout release.

## Task: Correct the permission boundary
- [x] Add failing source-wiring tests: MainActivity must not preempt a manifest check, AppUpdater must compare versions before permission and permission before download.
- [x] Run those tests against unchanged implementation and record both failures.
- [x] Add Callback.onInstallPermissionRequired(); move the existing UI permission explanation into that callback.
- [x] In checkAndInstall, after returning for a current version, post that callback and return when Android permission is missing. Keep the existing worker finally/shutdown behavior.
- [ ] Run complete Android/release/source-update regressions, compile and lint the Android project, and independently execute the actual update method with isolated platform stubs.
- [ ] Review, publish through normal PR policy, coordinate inclusion in the signed release, and repeat the actual Update-button test.

## Review Focus
Current version without permission; newer version without permission; malformed manifest; permission granted with mandatory signature verification; Play build without direct updates.

## Verification record
Both new regressions failed before the repair and pass afterward. The complete local Android/source-update selection passed87 tests, zero failures, in204.346s. An isolated Java harness compiled the exact production checkAndInstall method: original source failed, repaired source passed five version/permission/error/verification-order scenarios. Platform and transport are test doubles, not a live install claim.

Ruling: use the existing hosted Android workflow for complete build/lint/unit validation. The previously named local JDK17 executable is absent, while the available Android Studio runtime is Java25; no global Java configuration or dependency installation is justified for this narrow repair.

Both optional local external review CLIs returned credit errors before performing review. These are not successful reviews; normal protected PR checks and review must complete before merging. Native signed-release retesting follows coordinated3.5.14 publication.
