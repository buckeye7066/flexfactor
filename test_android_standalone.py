#!/usr/bin/env python3
"""Repository-level invariants for the managed FlexFactor mobile product."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent
ANDROID = ROOT / "android" / "app" / "src" / "main"
CLOUD = ROOT / "cloud"


def _android_version_name() -> str:
    """The versionName android-client.yml turns into the `android-v*` tag."""
    gradle = (ROOT / "android" / "app" /
              "build.gradle.kts").read_text(encoding="utf-8")
    match = re.search(r'^\s*versionName\s*=\s*"([^"]+)"', gradle, re.MULTILINE)
    assert match is not None, "android/app/build.gradle.kts has no versionName"
    return match.group(1)


class EngineRefIsOneVersionEverywhere(unittest.TestCase):
    """A phone run reaches the engine through the release and cloud pins.

    `android-client.yml` releases the app under the tag
    `android-v${versionName}`. FlexFactor Cloud writes a caller workflow into
    the target repository, and the reusable workflow checks the engine out at
    its own `ref:`. If any pin
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

    The APK no longer contains a workflow writer. These tests bind the cloud
    pin and reusable-workflow pin to the Android release version.
    """

    def test_the_cloud_engine_ref_matches_the_android_release(self):
        source = (CLOUD / "lib" / "config.js").read_text(encoding="utf-8")
        self.assertIn(
            f'ENGINE_REF = "android-v{_android_version_name()}"', source)
        self.assertFalse(
            (ANDROID / "java" / "com" / "firer" / "console" / "flexfactor" /
             "MobileWorkflow.java").exists(),
            "workflow installation belongs to FlexFactor Cloud, not the APK",
        )

    def test_the_reusable_workflow_checks_out_this_versions_engine(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        engine = workflow.split("- name: Check out the exact FlexFactor engine", 1)
        self.assertEqual(len(engine), 2, "the engine checkout step is missing")
        engine = engine[1].split("- name: ", 1)[0]
        self.assertIn("repository: buckeye7066/flexfactor", engine)
        self.assertIn(f"ref: android-v{_android_version_name()}", engine)


class ManagedAndroidInvariants(unittest.TestCase):
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

    def test_apk_has_one_managed_https_api_and_no_direct_github_api_fallback(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        gradle = (ROOT / "android" / "app" /
                  "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn("https://flexfactor-cloud.vercel.app", gradle)
        self.assertIn("BuildConfig.FLEXFACTOR_CLOUD_URL", api)
        self.assertIn("/api/runs/dispatch", api)
        self.assertNotIn("https://api.github.com", api)
        self.assertNotIn("/actions/workflows/", api)
        self.assertNotIn("installWorkflowThroughPullRequest", api)

    def test_managed_device_oauth_rotates_without_any_client_secret(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        service = (CLOUD / "lib" / "service.js").read_text(encoding="utf-8")
        self.assertIn("offline_access", service)
        self.assertIn("client_id: OAUTH_CLIENT_ID", service)
        self.assertNotIn("GITHUB_OAUTH_CLIENT_SECRET", service)
        self.assertIn("refreshOAuthToken", api)
        self.assertIn("token.refreshToken", activity)
        self.assertIn("api.refreshOAuthToken(session.refreshToken)", activity)
        saved = activity.split("private synchronized void saveGitHubSession", 1)[1]
        saved = saved.split("private synchronized String githubToken", 1)[0]
        self.assertIn("SecureStore.GITHUB_SESSION", saved)
        self.assertIn('record.put("access_token"', saved)
        self.assertIn('record.put("refresh_token"', saved)
        self.assertIn('record.put("expires_at"', saved)
        self.assertNotIn("secrets.put(SecureStore.GITHUB_TOKEN", saved)
        self.assertNotIn("GITHUB_OAUTH_CLIENT_SECRET", api)
        self.assertNotIn("OAUTH_CLIENT_ID", api)

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
        self.assertIn("deepseek-coder:6.7b", workflow)
        self.assertIn("ollama pull deepseek-coder:6.7b", workflow)
        self.assertIn("ollama serve", workflow)
        self.assertIn("88e0d36bd90121595e5516c84f6ab61b546368fbd2d825b4aae70999c949649d", workflow)
        model_install = workflow.split(
            "- name: Install and start the hosted open model", 1)[1].split(
                "- name: Install GitHub Copilot CLI", 1)[0]
        self.assertIn('rm -f "$archive"', model_install)
        self.assertLess(model_install.index('sudo tar --zstd -xf "$archive"'),
                        model_install.index('rm -f "$archive"'))
        self.assertLess(model_install.index('rm -f "$archive"'),
                        model_install.index("ollama pull qwen2.5-coder:7b"))
        self.assertLess(model_install.index('rm -f "$archive"'),
                        model_install.index("ollama pull deepseek-coder:6.7b"))
        self.assertIn("options: [auto]", workflow)
        self.assertNotIn('--provider "$PROVIDER"', workflow)
        self.assertIn("publication_complete", workflow)
        self.assertIn("merge-base --is-ancestor", workflow)
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
        service = (CLOUD / "lib" / "service.js").read_text(encoding="utf-8")
        self.assertIn("page <= 100", api)
        self.assertIn('"/api/repositories?page=" + page', api)
        self.assertIn("per_page=${REPOSITORY_PAGE_SIZE}&page=${page}", service)
        self.assertIn("has_more", service)
        self.assertIn('row.optBoolean("private", false)', api)
        self.assertIn("item?.permissions?.admin", service)
        self.assertIn("ensureTargetWorkflow", service)
        self.assertIn("installWorkflowThroughPullRequest", service)
        self.assertIn("GitHub's configured approvals", service)

    def test_provider_credentials_stay_local_until_sealed_dispatch(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        service = (CLOUD / "lib" / "service.js").read_text(encoding="utf-8")
        configure = service.split("export async function configure", 1)[1]
        configure = configure.split("export async function repositories", 1)[0]
        self.assertNotIn("openai_key", configure.lower())
        self.assertNotIn("anthropic_key", configure.lower())
        self.assertNotIn("verifyVendorKey", service)
        self.assertNotIn("putRepositorySecret", configure)
        self.assertNotIn("deleteRepositorySecret", api)
        self.assertNotIn("deleteRepositorySecret", service)
        self.assertIn("validateProviderKeys(openAiKey, anthropicKey)", api)
        self.assertIn("https://api.openai.com/v1/models", api)
        self.assertIn("https://api.anthropic.com/v1/models", api)
        self.assertIn("githubToken(), openAi, anthropic", (ANDROID / "java" / "com" /
                      "firer" / "console" / "flexfactor" / "MainActivity.java").read_text(
                          encoding="utf-8"))
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        setup = activity.split("private void showCredentialSetup", 1)[1]
        setup = setup.split("private void showCredentialLinks", 1)[0]
        self.assertNotIn("openAiValue = secrets.get", setup)
        self.assertNotIn("anthropicValue = secrets.get", setup)
        configure = activity.split("private void configureCredentials", 1)[1]
        configure = configure.split("private void showCredentialLinks", 1)[0]
        self.assertIn("if (!openAi.isEmpty())", configure)
        self.assertIn("if (!anthropic.isEmpty())", configure)

    def test_dispatch_uses_the_authoritative_run_id_with_legacy_correlation_fallback(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        service = (CLOUD / "lib" / "service.js").read_text(encoding="utf-8")
        self.assertIn("/api/runs/dispatch", api)
        self.assertIn("locateDispatchedRun", service)
        self.assertIn("workflow_run_id", service)
        self.assertIn("metadata?.default_branch", service)
        dispatch = service.split("export async function dispatch", 1)[1]
        dispatch = dispatch.split("function validateRunIdentity", 1)[0]
        self.assertLess(dispatch.index("assertTargetRef"),
                        dispatch.index("ensureTargetWorkflow"))
        self.assertLess(dispatch.index("assertTargetRef"),
                        dispatch.index("applyProviderSecrets"))
        self.assertIn("display_title", service)
        self.assertIn("request.request_id", service)
        self.assertIn("DISPATCH_READ_TIMEOUT_MS = 330_000", api)

    def test_every_run_operation_has_a_deployed_api_entry_point(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/runs/", ignore)
        self.assertNotIn("runs/", ignore)
        routes = {
            "dispatch.js": "dispatch",
            "status.js": "runStatus",
            "details.js": "runArtifact",
            "steer.js": "submitSteering",
        }
        for filename, operation in routes.items():
            source = (CLOUD / "api" / "runs" / filename).read_text(encoding="utf-8")
            self.assertIn(operation, source)
            self.assertIn("export default endpoint", source)

    def test_completed_runs_expose_the_error_ledger_inside_the_app(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("RunDetails runDetails", api)
        service = (CLOUD / "lib" / "service.js").read_text(encoding="utf-8")
        self.assertIn("mobile-phone-", service)
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
        service = (CLOUD / "lib" / "service.js").read_text(encoding="utf-8")
        self.assertIn("FLEXFACTOR_STEERING_", service)
        self.assertIn("flexfactor_steering.submit", workflow)
        self.assertIn('source="android"', workflow)

    def test_all_modes_support_a_durable_thirty_target_sequential_queue(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        queue = (ANDROID / "java" / "com" / "firer" / "console" /
                 "flexfactor" / "MobileRunQueue.java").read_text(encoding="utf-8")
        self.assertIn("Choose up to 30 repositories (run one at a time)", activity)
        self.assertIn("Repository-relative files, one per line (up to 30)", activity)
        self.assertIn("showBatchRepositoryList", activity)
        self.assertIn("dispatchBatch", activity)
        self.assertIn("Active and recent runs", activity)
        self.assertIn("RUN_HISTORY", activity)
        self.assertIn("MAX_TARGETS = 30", queue)
        self.assertIn("activeRunId", queue)
        saved = activity.split("private synchronized void saveRunQueue", 1)[1]
        saved = saved.split("private void resumeRunQueue", 1)[0]
        self.assertIn(".commit()", saved)
        self.assertNotIn(".apply()", saved)
        self.assertIn("could not be saved durably", saved)
        polling = activity.split("private void pollLastRun", 1)[1]
        polling = polling.split("private void refreshRunLabel", 1)[0]
        self.assertIn("catch (RuntimeException failed)", polling)
        self.assertIn("queueAdvanceFailure", polling)
        self.assertIn("polling = false", polling)
        self.assertIn("kept the next target stopped", polling)

    def test_pre_32_run_ids_migrate_to_the_legacy_control_repository(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("!preferences.contains(LAST_RUN_REPOSITORY)", activity)
        self.assertIn("GitHubApi.CONTROL_REPOSITORY", activity)

    def test_mobile_runner_matches_desktop_provider_and_verification_controls(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        for provider in ("ollama", "openai", "anthropic", "copilot"):
            self.assertIn(provider, workflow.lower())
        self.assertIn('--threshold "$THRESHOLD"', workflow)
        self.assertIn('--max-iterations "$MAX_ITERATIONS"', workflow)
        self.assertIn("--model-mode best", workflow)
        self.assertNotIn("--economy", workflow)
        self.assertNotIn("--single", workflow)
        self.assertIn("--auto-clean", workflow)
        self.assertNotIn("--no-auto-clean", workflow)
        self.assertIn("publication_complete", workflow)
        self.assertIn("merge-base --is-ancestor", workflow)

    def test_oauth_session_is_encrypted_and_provider_keys_are_sealed_before_cloud_dispatch(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        dispatch = api.split("RunState dispatch", 1)[1].split("RunState run", 1)[0]
        self.assertNotIn('"FLEXFACTOR_MOBILE_GITHUB_TOKEN"', dispatch)
        self.assertIn("encryptedProviderSecrets", dispatch)
        self.assertNotIn("openai_key", dispatch.lower())
        provider = api.split("private JSONObject encryptedProviderSecrets", 1)[1]
        provider = provider.split("private JSONObject seal", 1)[0]
        self.assertNotIn("request.useBoth", provider)
        self.assertNotIn("request.provider ==", provider)
        self.assertIn("boolean sendOpenAi = !openAi.isEmpty()", provider)
        self.assertIn("boolean sendAnthropic = !anthropic.isEmpty()", provider)
        self.assertIn("validateProviderKeys(sendOpenAi ? openAi : \"\"", provider)
        self.assertIn("OPENAI_API_KEY", provider)
        self.assertIn("ANTHROPIC_API_KEY", provider)
        self.assertIn("cryptoBoxSeal", api)
        store = (ANDROID / "java" / "com" / "firer" / "console" /
                 "flexfactor" / "SecureStore.java").read_text(encoding="utf-8")
        self.assertIn("AndroidKeyStore", store)

    def test_android_release_gate_proves_both_independent_hosted_families(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "android-client.yml").read_text(encoding="utf-8")
        self.assertIn("qwen2.5-coder:7b", workflow)
        self.assertIn("deepseek-coder:6.7b", workflow)
        self.assertIn("division_by_zero", workflow)
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
        build_gate = build_gate.split(
            "- name: Verify both independent free model families live", 1)[0]
        self.assertIn("testPlayUnitTest", build_gate)
        self.assertIn("bundlePlay", build_gate)

    def test_release_gate_runs_the_managed_cloud_contract(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "android-client.yml").read_text(encoding="utf-8")
        self.assertIn("npm ci --prefix cloud", workflow)
        self.assertIn("npm test --prefix cloud", workflow)

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
