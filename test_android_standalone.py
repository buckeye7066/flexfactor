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

    def test_documented_android_release_matches_apk_version(self):
        version = _android_version_name()
        for name, pattern in (
            ("README.md", r"^Android (\d+\.\d+\.\d+) is a native phone interface"),
            ("android/README.md", r"^# FlexFactor Mobile (\d+\.\d+\.\d+)$"),
        ):
            with self.subTest(document=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                match = re.search(pattern, source, re.MULTILINE)
                self.assertIsNotNone(match, f"{name} has no release identity")
                self.assertEqual(match.group(1), version)

    def test_cloud_engine_ref_is_current_or_explicitly_staged_one_patch(self):
        source = (CLOUD / "lib" / "config.js").read_text(encoding="utf-8")
        match = re.search(r'ENGINE_REF = "(android-v[^"]+)"', source)
        self.assertIsNotNone(match, "cloud config has no engine release pin")
        cloud_ref = match.group(1)
        android_ref = f"android-v{_android_version_name()}"
        marker = CLOUD / "ENGINE_ROLLOUT_PENDING"
        if cloud_ref == android_ref:
            self.assertFalse(
                marker.exists(),
                "remove the completed engine rollout marker",
            )
        else:
            self.assertTrue(
                marker.is_file(),
                "a cloud/Android version split requires an explicit rollout marker",
            )
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), android_ref)
            cloud_parts = cloud_ref.removeprefix("android-v").split(".")
            android_parts = _android_version_name().split(".")
            self.assertEqual(len(cloud_parts), 3)
            self.assertEqual(len(android_parts), 3)
            self.assertTrue(all(part.isdigit() for part in cloud_parts))
            self.assertTrue(all(part.isdigit() for part in android_parts))
            cloud_version = tuple(map(int, cloud_parts))
            android_version = tuple(map(int, android_parts))
            self.assertEqual(cloud_version[:2], android_version[:2])
            self.assertEqual(cloud_version[2] + 1, android_version[2])
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

    def test_live_journey_reports_the_version_it_actually_exercises(self):
        proof = (ROOT / ".github" / "scripts" /
                 "mobile_cloud_live_proof.py").read_text(encoding="utf-8")
        self.assertIn('Path("android/app/build.gradle.kts")', proof)
        self.assertIn('releases/latest/download/', proof)
        self.assertIn("verify_release_identity(", proof)
        self.assertIn('CLIENT_VERSION != SOURCE_VERSION', proof)
        self.assertIn('"X-FlexFactor-Client-Version": CLIENT_VERSION', proof)
        self.assertIn('"client_version": CLIENT_VERSION', proof)
        self.assertNotRegex(proof, r'Live 3\.\d+\.\d+ acceptance proof')


class CloudDeploymentIntegrityTests(unittest.TestCase):
    def test_promotion_preserves_the_verified_environment_and_build(self):
        source = (ROOT / ".github/workflows/cloud-production-deploy.yml").read_text(encoding="utf-8")
        build = source.split("deployment_url=$(vercel deploy", 1)[1].split("| tail -n 1)", 1)[0]
        self.assertIn("--prod", build)
        self.assertIn("--skip-domain", build)
        self.assertIn("FLEXFACTOR_CLOUD_SOURCE_SHA=$cloud_sha", build)

    def test_production_alias_verification_bounds_propagation_retries(self):
        source = (ROOT / ".github/workflows/cloud-production-deploy.yml").read_text(encoding="utf-8")
        verify = source.split("- name: Prove the production alias", 1)[1]
        self.assertIn("for attempt in {1..30}", verify)
        self.assertIn("sleep 2", verify)
        self.assertIn("health.source_revision, process.env.CLOUD_SOURCE_SHA", verify)
        self.assertIn("health.deployment_url, process.env.DEPLOYMENT_URL", verify)
        self.assertIn("exit 1", verify)


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
        self.assertIn("@github/copilot@1.0.86", workflow)
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
        android_workflow = (ROOT / ".github" / "workflows" /
                            "android-client.yml").read_text(encoding="utf-8")
        control_plane = android_workflow.split(
            "- name: Verify the current managed control plane", 1)[1].split(
                "- uses: gradle/actions/setup-gradle", 1)[0]
        self.assertIn("[ -f cloud/ENGINE_ROLLOUT_PENDING ]", control_plane)
        self.assertIn('pending_engine" != "$android_engine', control_plane)
        self.assertIn("cloud_patch + 1", control_plane)
        self.assertEqual(
            control_plane.count(r"=~ ^[0-9]+\.[0-9]+\.[0-9]+$"), 2)
        self.assertIn('expected_engine="$declared_engine"', control_plane)
        self.assertIn('rollout_engine="$pending_engine"', control_plane)
        self.assertIn(
            "the deployed cloud still advertises\n"
            "            # declared_engine",
            control_plane,
        )
        self.assertIn(
            '$rollout != "" and .engine_ref == $rollout', control_plane)
        self.assertIn(
            'declared_engine" != "$android_engine', control_plane)
        self.assertIn('if provider != "auto":', workflow)
        self.assertIn("options: [auto]", (CLOUD / "lib/workflow.js").read_text(encoding="utf-8"))
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

    def test_run_details_carries_the_selected_request_identity(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        details = api.split("RunDetails runDetails", 1)[1].split(
            "void submitSteering", 1)[0]
        self.assertIn("String requestId", details)
        self.assertIn('requireCanonicalUuid(requestId, "Run request ID")', details)
        self.assertIn('"&request_id=" + encode(requestId)', details)
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        view = activity.split("private void viewLastRunResults", 1)[1].split(
            "private void steerLastRun", 1)[0]
        self.assertIn('preferences.getString(LAST_RUN_REQUEST_ID, "")', view)
        self.assertIn("githubToken(), repository, requestId, id", view)

    def test_invalid_legacy_history_is_terminal_and_queue_identity_is_preserved(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        record = activity.split("private static final class RunRecord", 1)[1].split(
            "private void viewLastRunResults", 1)[0]
        self.assertIn("requireCanonicalUuid(requestId", record)
        self.assertIn("BLOCKED", record)
        polling = activity.split("private void pollLastRun", 1)[1].split(
            "private void refreshRunLabel", 1)[0]
        self.assertIn("record.matches(activeRequest)", polling)
        self.assertIn("record.matches(queue.activeRequest())", polling)

    def test_managed_steering_and_owner_probe_preserve_their_security_boundaries(self):
        workflow = (ROOT / ".github/workflows/mobile-run.yml").read_text(encoding="utf-8")
        self.assertNotIn("  workflow_dispatch:", workflow)
        self.assertIn("  workflow_call:", workflow)
        self.assertIn("STEERING_PRIVATE_KEY: ${{ secrets.STEERING_PRIVATE_KEY }}", workflow)
        probe = (ROOT / ".github/workflows/rotation.yml").read_text(encoding="utf-8")
        self.assertIn("github.triggering_actor == github.repository_owner", probe)
        self.assertIn("inputs.probe_copilot && 'live-probe' || 'ci'", probe)

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
        self.assertIn("ownedSteeringMailbox", service)
        self.assertIn("node engine/.github/scripts/mobile_steering_launch.mjs", workflow)
        self.assertNotIn("/actions/variables/", workflow)
        reader = (ROOT / ".github" / "scripts" / "mobile_steering_poll.mjs").read_text(encoding="utf-8")
        self.assertIn("submit_session_routing", reader)
        self.assertIn("source='android'", reader)
        self.assertNotIn("STEERING_PRIVATE_KEY:", workflow.split("- name: Run FlexFactor", 1)[1])

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
        # NOT `assertIn("--model-mode best")`: that flag is RETIRED and the
        # runtime prints an inert-flag notice for it on the raw argv, so a
        # workflow that passes it tells every mobile run that its request is
        # being ignored. Desktop parity is the single shared ladder, which is
        # what you get by passing nothing.
        self.assertNotIn("--model-mode", workflow)
        self.assertNotIn("--paid-models", workflow)
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
        self.assertIn("qwen2.5-coder:1.5b", workflow)
        self.assertIn("deepseek-coder:1.3b", workflow)
        self.assertNotIn("qwen2.5-coder:7b", workflow)
        self.assertNotIn("deepseek-coder:6.7b", workflow)
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
        model_install = workflow.split(
            "- name: Verify both independent free model families live", 1)[1].split(
                "- name: Record exact APK checksum", 1)[0]
        self.assertIn('rm -f "$archive"', model_install)
        self.assertLess(model_install.index('sudo tar --zstd -xf "$archive"'),
                        model_install.index('rm -f "$archive"'))
        model_loop = model_install.index(
            'for model in ("qwen2.5-coder:1.5b", "deepseek-coder:1.3b")')
        self.assertLess(model_install.index('rm -f "$archive"'), model_loop)
        self.assertIn(
            'subprocess.run(["ollama", "pull", model], check=True)', model_install)
        self.assertIn(
            'subprocess.run(["ollama", "stop", model], check=False)', model_install)
        self.assertIn(
            'subprocess.run(["ollama", "rm", model], check=True)', model_install)
        self.assertIn('"num_ctx": 2048', model_install)
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
        self.assertIn('.setPositiveButton("Update",', launch)
        self.assertIn('.setNegativeButton("Later",', launch)
        self.assertNotIn("startUpdate();", launch)
        self.assertIn("void check(CheckCallback callback)", updater)

class MobileFailureDiagnosticTests(unittest.TestCase):
    def test_mobile_workflow_bounds_and_redacts_failure_diagnostics(self):
        workflow = (ROOT / ".github" / "workflows" / "mobile-run.yml").read_text(
            encoding="utf-8")
        diagnostic = workflow.split(
            "- name: Emit bounded redacted failure diagnostics", 1)[1].split(
                "- name: Write the phone-readable run summary", 1)[0]
        self.assertIn("if: failure()", diagnostic)
        self.assertIn("maximum = 64 * 1024", diagnostic)
        self.assertIn("stream.seek(max(0, stream.tell() - maximum))", diagnostic)
        self.assertIn("from flexfactor_egress import redact_text", diagnostic)
        self.assertIn("[-24:]", diagnostic)
        self.assertIn("publication_reason", diagnostic)
        self.assertNotIn("read_bytes()", diagnostic)


class MobileReleaseIdentityTests(unittest.TestCase):
    """Exercise source binding with real local git history, not mocked refs."""

    def setUp(self):
        import importlib.util
        import subprocess
        import tempfile
        self.process = subprocess
        self.temporary = tempfile.TemporaryDirectory(prefix="ff-release-proof-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        module = ROOT / ".github" / "scripts" / "mobile_release_identity.py"
        self.assertTrue(module.is_file(), "component-bound release verifier is required")
        spec = importlib.util.spec_from_file_location("mobile_release_identity", module)
        self.identity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.identity)
        self.git("init", "-q", "-b", "main")
        self.write("android/app/build.gradle.kts", 'versionName = "3.5.8"\n')
        self.write("cloud/lib/config.js", 'export const SERVICE_VERSION = "1.1.6";\nexport const ENGINE_REF = "android-v3.5.7";\n')
        self.write("cloud/ENGINE_ROLLOUT_PENDING", "android-v3.5.8\n")
        self.write("flexfactor.py", "print('released engine')\n")
        self.release = self.commit("signed Android source")
        self.git("tag", "android-v3.5.8")
        self.write("cloud/lib/config.js", 'export const SERVICE_VERSION = "1.1.7";\nexport const ENGINE_REF = "android-v3.5.8";\n')
        (self.root / "cloud/ENGINE_ROLLOUT_PENDING").unlink()
        self.head = self.commit("activate published engine")
        self.manifest = {"schema": "flexfactor-update-v1", "channel": "stable",
                         "status": "active", "versionName": "3.5.8",
                         "sourceRevision": self.release}

    def git(self, *args):
        return self.process.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=self.process.STDOUT).strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "--all")
        self.git("-c", "user.name=Release proof tests", "-c",
                 "user.email=release-proof@example.invalid", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def verify(self, expected=None):
        return self.identity.verify_release_identity(
            self.root, expected or self.head, self.manifest, "3.5.8")

    def test_cloud_rollout_keeps_exact_signed_release_binding(self):
        result = self.verify()
        self.assertEqual(result["release_source_sha"], self.release)
        self.assertEqual(result["verification_sha"], self.head)
        self.assertNotEqual(self.release, self.head)
        self.assertEqual(result["engine_ref"], "android-v3.5.8")
        self.assertEqual(result["cloud_version"], "1.1.7")

    def test_whitespace_in_paths_cannot_create_a_cloud_exception(self):
        self.write(" cloud/unreleased.py", "print('not cloud runtime')\n")
        self.head = self.commit("unreleased path with leading whitespace")
        with self.assertRaisesRegex(ValueError, "unreleased runtime"):
            self.verify()

    def test_unreleased_engine_changes_block_the_proof(self):
        self.write("flexfactor.py", "print('not released')\n")
        self.head = self.commit("unreleased engine change")
        with self.assertRaisesRegex(ValueError, "unreleased runtime"):
            self.verify()

    def test_renaming_engine_into_cloud_does_not_hide_unreleased_deletion(self):
        self.git("mv", "flexfactor.py", "cloud/flexfactor.py")
        self.head = self.commit("rename engine into cloud")
        with self.assertRaisesRegex(ValueError, "unreleased runtime"):
            self.verify()

    def test_same_version_cloud_change_requires_its_actual_source_revision(self):
        old_source = self.head
        self.write("cloud/new-runtime.js", "export const newBehavior = true;\n")
        self.head = self.commit("change cloud without bumping display version")
        expected = self.verify()
        health = {"ok": True, "oauth_device_configured": True,
                  "version": "1.1.7", "engine_ref": "android-v3.5.8",
                  "source_revision": old_source,
                  "deployment_url": "https://flexfactor-cloud-old-team.vercel.app"}
        with self.assertRaisesRegex(ValueError, "source"):
            self.identity.verify_cloud_health(health, expected)
        health["source_revision"] = self.head
        self.identity.verify_cloud_health(health, expected)

    def test_proof_only_commit_preserves_the_cloud_source_revision(self):
        cloud_source = self.head
        self.write(".github/scripts/mobile_cloud_live_proof.py", "# proof only\n")
        self.head = self.commit("proof only")
        self.assertEqual(self.verify()["cloud_source_sha"], cloud_source)

    def test_every_response_must_match_the_initial_cloud_deployment(self):
        from email.message import Message
        initial = {"source_revision": self.head,
                   "deployment_url": "https://flexfactor-cloud-one-team.vercel.app"}
        headers = Message()
        headers["x-flexfactor-cloud-source"] = self.head
        headers["x-flexfactor-deployment"] = initial["deployment_url"]
        self.identity.verify_cloud_response(headers, initial)
        for name, changed in (("x-flexfactor-cloud-source", self.release),
                              ("x-flexfactor-deployment", "https://flexfactor-cloud-two-team.vercel.app")):
            with self.subTest(header=name):
                copy = Message()
                for key, value in headers.items(): copy[key] = changed if key.lower() == name else value
                with self.assertRaises(ValueError):
                    self.identity.verify_cloud_response(copy, initial)
        with self.assertRaises(ValueError):
            self.identity.verify_cloud_response(Message(), initial)

    def test_live_request_checks_response_identity_before_reading_body(self):
        import ast
        import json
        import types
        import urllib.error
        from email.message import Message
        source = (ROOT / ".github/scripts/mobile_cloud_live_proof.py").read_text(encoding="utf-8")
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "request")
        initial = {"source_revision": self.head,
                   "deployment_url": "https://flexfactor-cloud-one-team.vercel.app"}
        class Response:
            status = 200
            reads = 0
            def __init__(self, url):
                self.headers = Message()
                self.headers["X-FlexFactor-Cloud-Source"] = initial["source_revision"]
                self.headers["X-FlexFactor-Deployment"] = url
                self.headers["Content-Type"] = "application/json"
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                self.reads += 1
                return b"{}"
        for changed in (False, True):
            with self.subTest(promoted=changed):
                response = Response("https://flexfactor-cloud-two-team.vercel.app" if changed
                                    else initial["deployment_url"])
                environment = {"json": json, "BASE": "https://flexfactor-cloud.vercel.app",
                               "HEADERS": {}, "deployed_health": initial,
                               "verify_cloud_response": self.identity.verify_cloud_response,
                               "urllib": types.SimpleNamespace(
                                   error=urllib.error,
                                   request=types.SimpleNamespace(Request=lambda *a, **k: None,
                                                                 urlopen=lambda *a, **k: response))}
                exec(compile(ast.Module(body=[function], type_ignores=[]), "<live-request>", "exec"), environment)
                if changed:
                    with self.assertRaises(ValueError): environment["request"]("GET", "/api/configure")
                    self.assertEqual(response.reads, 0)
                else:
                    self.assertEqual(environment["request"]("GET", "/api/configure")[0], 200)
                    self.assertEqual(response.reads, 1)

    def test_cloud_deploy_and_live_proof_use_the_same_promotion_lock(self):
        for name in ("cloud-production-deploy.yml", "mobile-cloud-live-proof.yml"):
            with self.subTest(workflow=name):
                source = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
                self.assertIn("group: flexfactor-cloud-production", source)
                self.assertIn("cancel-in-progress: false", source)
                self.assertIn("fetch-depth: 0", source)
                self.assertIn('test "$EXPECTED_SHA" = "$live_main"', source)

    def test_unreleased_android_changes_block_the_proof(self):
        self.write("android/app/src/main/MainActivity.java", "// unreleased\n")
        self.head = self.commit("unreleased Android change")
        with self.assertRaisesRegex(ValueError, "unreleased runtime"):
            self.verify()

    def test_unreleased_runner_changes_block_the_proof(self):
        self.write(".github/workflows/mobile-run.yml", "name: unreleased\n")
        self.head = self.commit("unreleased runner change")
        with self.assertRaisesRegex(ValueError, "unreleased runtime"):
            self.verify()

    def test_manifest_cannot_name_a_different_source_than_the_tag(self):
        self.manifest["sourceRevision"] = self.head
        with self.assertRaisesRegex(ValueError, "release tag"):
            self.verify()

    def test_checkout_must_be_the_authorized_main_revision(self):
        with self.assertRaisesRegex(ValueError, "authorized"):
            self.verify(expected=self.release)

    def test_released_source_must_be_an_ancestor(self):
        self.git("checkout", "--orphan", "unrelated")
        self.head = self.commit("unrelated history with identical files")
        with self.assertRaisesRegex(ValueError, "ancestor"):
            self.verify()

    def test_cloud_cannot_still_point_to_the_old_engine(self):
        self.write("cloud/lib/config.js", 'export const SERVICE_VERSION = "1.1.7";\nexport const ENGINE_REF = "android-v3.5.7";\n')
        self.head = self.commit("stale cloud engine")
        with self.assertRaisesRegex(ValueError, "engine"):
            self.verify()

    def test_incomplete_rollout_blocks_live_verification(self):
        self.write("cloud/ENGINE_ROLLOUT_PENDING", "android-v3.5.8\n")
        self.head = self.commit("unfinished rollout")
        with self.assertRaisesRegex(ValueError, "pending"):
            self.verify()

    def test_proof_maintenance_does_not_relabel_the_signed_release(self):
        self.write(".github/scripts/mobile_cloud_live_proof.py", "# proof maintenance\n")
        self.head = self.commit("proof maintenance only")
        result = self.verify()
        self.assertEqual(result["release_source_sha"], self.release)
        self.assertEqual(result["verification_sha"], self.head)

    def test_live_cloud_must_match_the_current_version_and_released_engine(self):
        expected = self.verify()
        health = {"ok": True, "oauth_device_configured": True,
                  "version": "1.1.7", "engine_ref": "android-v3.5.8",
                  "source_revision": self.head,
                  "deployment_url": "https://flexfactor-cloud-one-team.vercel.app"}
        self.identity.verify_cloud_health(health, expected)
        for key, bad in (("ok", False), ("oauth_device_configured", False),
                         ("version", "1.1.6"), ("engine_ref", "android-v3.5.7")):
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.identity.verify_cloud_health({**health, key: bad}, expected)



class MobileModeAcceptanceTests(unittest.TestCase):
    def helpers(self):
        import ast
        path = ROOT / '.github/scripts/mobile_cloud_live_proof.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        names = {'build_live_request', 'validate_live_result', 'record_live_result'}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in functions}, names)
        namespace = {}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
        return namespace

    def test_each_live_mode_keeps_its_boundaries(self):
        build = self.helpers()['build_live_request']
        for mode in ('refactor', 'scout', 'audit', 'prodready'):
            request = build('request-1', 'buckeye7066/flexfactor-demo-tinystats', '3.5.9', mode)
            self.assertEqual(request['mode'], mode)
            self.assertEqual(request['max_cost'], 1)
            self.assertFalse(request['scout_apply'])
            self.assertEqual(request['file'], 'stats_utils.py' if mode == 'refactor' else '')
            self.assertEqual(bool(request['goal']), mode == 'refactor')
            self.assertEqual('do not apply' in request['guidance'], mode == 'scout')
        for mode, repo in [('unknown', 'buckeye7066/flexfactor-demo-tinystats'), ('audit', 'buckeye7066/GrantFlow')]:
            with self.assertRaises(ValueError):
                build('request-1', repo, '3.5.9', mode)

    def test_result_cannot_substitute_another_request_or_unpublished_repair(self):
        helper = self.helpers()
        request = helper['build_live_request']('request-1', 'buckeye7066/flexfactor-demo-tinystats', '3.5.9', 'audit')
        good = dict(request_id='request-1', mode='audit', target_repository=request['repository'],
                    target_ref='main', success=True, exit_code=0, publication_required=False,
                    publication_complete=True, source_before='a'*40, source_after='a'*40,
                    run_url='https://github.com/' + request['repository'] + '/actions/runs/99')
        validate = helper['validate_live_result']
        validate(good, request, 99)
        invalid = {'request_id': 'request-2', 'mode': 'scout', 'target_repository': 'owner/other',
                   'target_ref': 'old', 'success': False, 'exit_code': False,
                   'publication_complete': False, 'source_after': 'b'*40, 'run_url': 'https://github.com/other'}
        for field, value in invalid.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(dict(good, **{field: value}), request, 99)
        for field in good:
            incomplete = dict(good)
            del incomplete[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                validate(incomplete, request, 99)
        validate(dict(good, source_after='b'*40, publication_required=True), request, 99)



    def test_refactor_requires_an_actual_published_change(self):
        helper = self.helpers()
        request = helper['build_live_request']('request-1', 'buckeye7066/flexfactor-demo-tinystats', '3.5.9', 'refactor')
        result = dict(request_id='request-1', mode='refactor', target_repository=request['repository'],
                      target_ref='main', success=True, exit_code=0, publication_required=False,
                      publication_complete=True, source_before='a'*40, source_after='a'*40,
                      run_url='https://github.com/' + request['repository'] + '/actions/runs/99')
        with self.assertRaisesRegex(ValueError, 'source change'):
            helper['validate_live_result'](result, request, 99)
        result.update(source_after='b'*40, publication_required=True)
        helper['validate_live_result'](result, request, 99)

    def test_failed_validation_never_records_successful_proof(self):
        helper = self.helpers()
        request = helper['build_live_request']('request-1', 'buckeye7066/flexfactor-demo-tinystats', '3.5.9', 'audit')
        result = dict(request_id='request-1', mode='audit', target_repository=request['repository'],
                      target_ref='main', success=True, exit_code=0, publication_required=False,
                      publication_complete=True, source_before='a'*40, source_after='a'*40,
                      run_url='https://github.com/' + request['repository'] + '/actions/runs/99')
        saves = []
        for changed in ({'run_url': 'wrong'}, {'source_after': 'invalid'}):
            with self.assertRaises(ValueError):
                helper['record_live_result'](dict(result, **changed), request, 99, {'conclusion': 'success'}, lambda **kw: saves.append(kw))
            self.assertEqual(saves[-1]['stage'], 'validation-failed')
            self.assertFalse(saves[-1]['result_validated'])
            self.assertNotEqual(saves[-1].get('phone_result_success'), True)
        helper['record_live_result'](result, request, 99, {'conclusion': 'success'}, lambda **kw: saves.append(kw))
        self.assertEqual(saves[-1]['stage'], 'artifact-validated')
        self.assertTrue(saves[-1]['result_validated'])
        with self.assertRaises(ValueError):
            helper['record_live_result'](result, request, 99, {'conclusion': 'failure'}, lambda **kw: saves.append(kw))
        self.assertFalse(saves[-1]['result_validated'])

    def test_remaining_modes_have_serial_independent_job_budgets(self):
        import re
        source = (ROOT / '.github/workflows/mobile-cloud-live-proof.yml').read_text(encoding='utf-8')
        jobs = {match.group(1): match.group(2) for match in re.finditer(
            r'^  ([a-z-]+):\n(.*?)(?=^  [a-z-]+:|\Z)', source.split('jobs:\n', 1)[1], re.M | re.S)}
        self.assertTrue({'remaining-refactor', 'remaining-audit', 'remaining-prodready'}.issubset(jobs))
        shared = jobs['live-proof']
        self.assertIn('steps: &acceptance_steps', shared)
        self.assertIn('Authorize the exact main revision and triggering owner', shared)
        self.assertIn('Upload redacted live proof', shared)
        self.assertIn('${{ github.job }}', shared)
        for mode in ('refactor', 'audit', 'prodready'):
            job = jobs['remaining-' + mode]
            self.assertIn('timeout-minutes: 360', job)
            self.assertIn('MODE: ' + mode, job)
            self.assertIn('environment: Production', job)
            self.assertIn("if: inputs.mode == 'remaining'", job)
            self.assertIn('steps: *acceptance_steps', job)
        self.assertIn('needs: remaining-refactor', jobs['remaining-audit'])
        self.assertIn('needs: remaining-audit', jobs['remaining-prodready'])
        self.assertIn('group: flexfactor-cloud-production', source)



    def test_timeout_preserves_original_request_as_nonvalidated_evidence(self):
        import json, os, subprocess, sys, tempfile, textwrap
        from pathlib import Path
        source = (ROOT / '.github/workflows/mobile-cloud-live-proof.yml').read_text(encoding='utf-8')
        self.assertIn('timeout-minutes: 300', source)
        start = source.split('      - name: Preserve incomplete acceptance evidence', 1)[1]
        step = start.split('      - name: Upload redacted live proof', 1)[0]
        self.assertIn("steps.exercise.outcome != 'success'", step)
        code = textwrap.dedent(step.split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mobile-cloud-live-proof.json'
            original = {'request_id': 'original-request', 'run_id': 99, 'stage': 'steering-accepted'}
            path.write_text(json.dumps(original), encoding='utf-8')
            result = subprocess.run([sys.executable, '-c', code], cwd=directory,
                                    env=dict(os.environ, STEP_OUTCOME='failure'),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['request_id'], original['request_id'])
            self.assertEqual(saved['run_id'], 99)
            self.assertFalse(saved['result_validated'])
            self.assertEqual(saved['stage'], 'observation-incomplete')
            self.assertEqual(saved['verification_step_outcome'], 'failure')


if __name__ == "__main__":
    unittest.main(verbosity=2)
