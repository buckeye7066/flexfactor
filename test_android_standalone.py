#!/usr/bin/env python3
"""Repository-level invariants for the independent Android app."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent
ANDROID = ROOT / "android" / "app" / "src" / "main"


def _android_version_name() -> str:
    """The versionName android-client.yml turns into the `android-v*` tag."""
    gradle = (ROOT / "android" / "app" /
              "build.gradle.kts").read_text(encoding="utf-8")
    match = re.search(r'^\s*versionName\s*=\s*"([^"]+)"', gradle, re.MULTILINE)
    assert match is not None, "android/app/build.gradle.kts has no versionName"
    return match.group(1)


class EngineRefIsOneVersionEverywhere(unittest.TestCase):
    """A phone run reaches the engine through THREE version pins.

    `android-client.yml` releases the app under the tag
    `android-v${versionName}`. The app writes a caller workflow into the target
    repository that calls `mobile-run.yml@<ENGINE_REF>`, and that reusable
    workflow checks the engine out at its own `ref:`. If any of the three
    disagree, every phone run either executes an engine the build was never
    tested against or names a tag that does not exist yet -- and the owner sees
    a run that dies at its first step.

    That is not hypothetical. The shipped `android-v3.2.1` app carried
    `ENGINE_REF = "android-v3.2.0"`, so it installed a caller that ran the
    3.2.0 engine, whose request validator read the caller's event payload
    instead of the reusable workflow's inputs. `target_repository` is computed
    by the caller (`${{ github.repository }}`) and so is absent from that
    payload, which made it the empty string, which failed the repository regex.
    Live runs 33253519755 and 33255312894 (buckeye7066/FutureU, 2026-08-29)
    both ended `invalid repository` at step 2 of 17.

    `MobileWorkflow.ENGINE_REF` is now derived from `BuildConfig.VERSION_NAME`,
    so the Java half cannot drift. These tests hold the two pins that live
    outside the Java compiler's reach.
    """

    def test_the_engine_ref_is_derived_from_the_build_not_typed_again(self):
        source = (ANDROID / "java" / "com" / "firer" / "console" / "flexfactor" /
                  "MobileWorkflow.java").read_text(encoding="utf-8")
        self.assertIn(
            'ENGINE_REF = "android-v" + BuildConfig.VERSION_NAME', source)
        # A hand-typed version literal anywhere in this file is the defect.
        self.assertIsNone(
            re.search(r'"android-v\d', source),
            "MobileWorkflow must not hardcode an engine version",
        )

    def test_the_reusable_workflow_checks_out_this_versions_engine(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        engine = workflow.split("- name: Check out the exact FlexFactor engine", 1)
        self.assertEqual(len(engine), 2, "the engine checkout step is missing")
        engine = engine[1].split("- name: ", 1)[0]
        self.assertIn("repository: buckeye7066/flexfactor", engine)
        self.assertIn(f"ref: android-v{_android_version_name()}", engine)


class StandaloneAndroidInvariants(unittest.TestCase):
    def test_launcher_declares_no_termux_runtime_permission(self):
        manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.assertNotIn("com.termux", manifest.lower())
        self.assertNotIn("RUN_COMMAND", manifest)

    def test_activity_exposes_every_original_mode(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        for label in (
            "1 · Refactor a file",
            "2 · Scout improvements",
            "3 · Audit and repair",
            "4 · Make production ready",
        ):
            self.assertIn(label, activity)

    def test_android_network_policy_is_https_only(self):
        policy = (ANDROID / "res" / "xml" /
                  "network_security_config.xml").read_text(encoding="utf-8")
        self.assertIn('cleartextTrafficPermitted="false"', policy)
        self.assertNotIn('cleartextTrafficPermitted="true"', policy)
        self.assertNotIn("localhost", policy)
        self.assertNotIn("127.0.0.1", policy)

    def test_activity_has_no_loopback_or_shell_engine(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ANDROID / "java" / "com" / "firer" / "console" /
                         "flexfactor").glob("*.java")
        )
        for forbidden in ("127.0.0.1", "localhost:8765", "EngineRecoveryScript",
                          "TERMUX_PACKAGE", "RUN_COMMAND_STDIN"):
            self.assertNotIn(forbidden, source)

    def test_mobile_workflow_is_present_and_requires_protected_secrets(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        for mode in ("refactor", "scout", "audit", "prodready"):
            self.assertIn(mode, workflow)
        self.assertNotIn("FLEXFACTOR_MOBILE_GITHUB_TOKEN", workflow)
        self.assertIn("copilot-requests: write", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("secrets.OPENAI_API_KEY", workflow)
        self.assertIn("secrets.ANTHROPIC_API_KEY", workflow)
        self.assertIn("@github/copilot@1.0.81", workflow)
        self.assertIn("qwen2.5-coder:7b", workflow)
        self.assertIn('--judge-model "$FLEXFACTOR_PHONE_MODEL"', workflow)
        self.assertIn("ollama serve", workflow)
        self.assertIn("88e0d36bd90121595e5516c84f6ab61b546368fbd2d825b4aae70999c949649d", workflow)
        self.assertIn('--provider "$PROVIDER"', workflow)
        self.assertNotIn("${{ inputs.github_token }}", workflow)
        self.assertNotIn("${{ inputs.openai", workflow.lower())
        self.assertNotIn("inputs.target_repository }} ·", workflow)
        self.assertNotIn("find target -type f -name '*.bak'", workflow)
        self.assertIn("args+=(--apply --yes)", workflow)
        self.assertIn("target/*_repo_rewards_report.md", workflow)
        self.assertIn("target/*_audit_report.md", workflow)
        self.assertIn("target/*_readiness.md", workflow)
        self.assertIn("Collect the in-app result and error ledger", workflow)
        self.assertIn("mobile-phone-${{ inputs.request_id }}", workflow)
        summary = workflow.split("- name: Write the phone-readable run summary", 1)[1]
        summary = summary.split("- name: Collect the in-app result and error ledger", 1)[0]
        self.assertNotIn("${{ inputs.", summary)

    def test_reusable_validator_reads_effective_workflow_call_inputs(self):
        """Computed `with:` values are absent from the caller's event payload."""
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        self.assertNotIn('os.environ["GITHUB_EVENT_PATH"]', workflow)
        self.assertIn(
            "INPUT_TARGET_REPOSITORY: ${{ inputs.target_repository }}",
            workflow,
        )
        self.assertIn(
            'repository = os.environ["INPUT_TARGET_REPOSITORY"]',
            workflow,
        )

    def test_every_validated_value_comes_from_the_effective_input_context(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        expected = {
            "REQUEST_ID": "request_id",
            "MODE": "mode",
            "PROVIDER": "provider",
            "TARGET_REPOSITORY": "target_repository",
            "TARGET_REF": "target_ref",
            "FILE": "file",
            "GOAL": "goal",
            "MAX_COST": "max_cost",
            "THRESHOLD": "threshold",
            "MAX_ITERATIONS": "max_iterations",
        }
        for env_name, input_name in expected.items():
            self.assertIn(
                f"INPUT_{env_name}: ${{{{ inputs.{input_name} }}}}",
                workflow,
            )

    def test_repository_picker_paginates_and_supports_private_targets(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        self.assertIn('per_page=100&page=" + page', api)
        self.assertIn('row.optBoolean("private", false)', api)
        self.assertNotIn('if (row.optBoolean("private"', api)
        self.assertIn("ensureTargetWorkflow", api)
        self.assertIn("MobileWorkflow.FILE_NAME", api)
        self.assertIn("installWorkflowThroughPullRequest", api)
        self.assertIn("GitHub's configured approvals", api)

    def test_credentials_are_validated_and_never_deleted_when_switching_provider(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        configure = api.split("ConfigurationResult configure", 1)[1]
        configure = configure.split("List<Repository> repositories", 1)[0]
        self.assertIn("verifyOpenAi(openAi)", configure)
        self.assertIn("verifyAnthropic(anthropic)", configure)
        self.assertNotIn("putRepositorySecret", configure)
        self.assertNotIn("deleteRepositorySecret", api)

    def test_dispatch_correlates_the_standard_empty_github_response(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        dispatch = api.split("RunState dispatch", 1)[1]
        dispatch = dispatch.split("RunState run", 1)[0]
        self.assertIn("locateDispatchedRun", dispatch)
        self.assertNotIn("workflow_run_id", dispatch)
        self.assertIn("display_title", api)
        self.assertIn("request.requestId", api)

    def test_completed_runs_expose_the_error_ledger_inside_the_app(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("RunDetails runDetails", api)
        self.assertIn("mobile-phone-", api)
        self.assertIn("errors.md", api)
        self.assertIn("View results and error ledger", activity)

    def test_active_audits_accept_authenticated_phone_steering(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        self.assertIn("Steer this build", activity)
        self.assertIn("submitSteering", api)
        self.assertIn("FLEXFACTOR_STEERING_", api)
        self.assertIn("flexfactor_steering.submit", workflow)
        self.assertIn('source="android"', workflow)

    def test_audit_and_prodready_support_parallel_repository_runs(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("Run up to 10 repositories in parallel", activity)
        self.assertIn("showBatchRepositoryList", activity)
        self.assertIn("dispatchBatch", activity)
        self.assertIn("Active and recent runs", activity)
        self.assertIn("RUN_HISTORY", activity)

    def test_pre_32_run_ids_migrate_to_the_legacy_control_repository(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("!preferences.contains(LAST_RUN_REPOSITORY)", activity)
        self.assertIn("GitHubApi.CONTROL_REPOSITORY", activity)

    def test_mobile_runner_matches_desktop_provider_and_verification_controls(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        for provider in ("ollama", "openai", "anthropic", "copilot"):
            self.assertIn(provider, workflow)
        self.assertIn('--threshold "$THRESHOLD"', workflow)
        self.assertIn('--max-iterations "$MAX_ITERATIONS"', workflow)
        self.assertIn("audit_args+=(--economy)", workflow)
        self.assertIn("provider_args+=(--economy)", workflow)
        self.assertIn("audit_args+=(--single)", workflow)
        # ...but ONLY where a pair is impossible or was not asked for. A phone
        # run whose second key is merely missing must reach the engine's paid
        # refusal, not be quietly handed --single: that removes the approval
        # step paid mode is named for while the run still reports as ordinary,
        # which is the exact downgrade the desktop path refuses.
        self.assertNotIn('elif [ "$PROVIDER" = openai ] && [ -z "$ANTHROPIC_API_KEY" ]',
                         workflow)
        self.assertNotIn('elif [ "$PROVIDER" = anthropic ] && [ -z "$OPENAI_API_KEY" ]',
                         workflow)
        self.assertIn("--auto-clean", workflow)
        self.assertNotIn("--no-auto-clean", workflow)

    def test_owner_pat_is_not_persisted_and_cross_model_keys_are_available(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        dispatch = api.split("RunState dispatch", 1)[1].split("RunState run", 1)[0]
        self.assertNotIn('"FLEXFACTOR_MOBILE_GITHUB_TOKEN"', dispatch)
        self.assertIn("request.useBoth", dispatch)
        provider = api.split("private void prepareProviderSecret", 1)[1]
        provider = provider.split("private boolean ensureTargetWorkflow", 1)[0]
        self.assertIn('putRepositorySecret(token, repository, "OPENAI_API_KEY"', provider)
        self.assertIn('putRepositorySecret(token, repository, "ANTHROPIC_API_KEY"', provider)

    def test_android_release_gate_proves_the_default_hosted_provider(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "android-client.yml").read_text(encoding="utf-8")
        self.assertIn("qwen2.5-coder:7b", workflow)
        self.assertIn("ollama serve", workflow)
        self.assertIn("sha256sum --check --strict", workflow)
        self.assertIn("FLEXFACTOR_READY", workflow)
        self.assertIn("bundlePlay", workflow)
        self.assertIn("app-play.aab", workflow)
        self.assertIn("Prove strict bundle signature policy", workflow)
        self.assertIn("Strict verification accepted a partially signed archive", workflow)
        self.assertIn("jarsigner -verify -strict", workflow)
        self.assertIn("-storepass:env FLEXFACTOR_ANDROID_STORE_PASSWORD", workflow)
        self.assertNotIn("bundle/play/app-release.aab", workflow)
        build_gate = workflow.split("- name: Unit tests, lint, and debug APK", 1)[1]
        build_gate = build_gate.split("- name: Verify the default phone model provider live", 1)[0]
        self.assertIn("testPlayUnitTest", build_gate)
        self.assertIn("bundlePlay", build_gate)

    def test_play_bundle_omits_the_direct_apk_self_installer(self):
        play_manifest = (ROOT / "android" / "app" / "src" / "play" /
                         "AndroidManifest.xml").read_text(encoding="utf-8")
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        gradle = (ROOT / "android" / "app" /
                  "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn("REQUEST_INSTALL_PACKAGES", play_manifest)
        self.assertIn('tools:node="remove"', play_manifest)
        self.assertIn('create("play")', gradle)
        self.assertIn('!"play".equals(BuildConfig.BUILD_TYPE)', activity)

    def test_startup_update_check_runs_before_installer_permission_gate(self):
        updater = (ANDROID / "java" / "com" / "firer" / "console" /
                   "flexfactor" / "AppUpdater.java").read_text(encoding="utf-8")
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        launch = activity.split("private void checkForUpdateOnLaunch", 1)[1]
        launch = launch.split("private void resetUpdateButton", 1)[0]
        self.assertIn("new AppUpdater(this).check", launch)
        self.assertIn("onUpdateAvailable", launch)
        self.assertIn("Allow updates", launch)
        self.assertIn("void check(CheckCallback callback)", updater)


if __name__ == "__main__":
    unittest.main(verbosity=2)
