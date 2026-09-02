"""Contracts for the dashboard and the retired local phone launcher.

Phone execution belongs exclusively to the signed managed Android client. The
web dashboard remains an authenticated viewer/steering surface and must never
reintroduce local-only mutation or provider-choice endpoints.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import flexfactor_web as web


class RetiredPhoneLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.project = os.path.join(self.root, "target-app")
        os.makedirs(os.path.join(self.project, ".git"))
        self.env = {
            "TERMUX_VERSION": "0.118.3",
            "PREFIX": "/data/data/com.termux/files/usr",
            "FLEXFACTOR_PROJECT_ROOTS": self.root,
            "OPENAI_API_KEY": "test-secret-that-must-not-leak",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_repository_discovery_still_contains_paths_for_viewer_metadata(self):
        os.makedirs(os.path.join(self.root, "ordinary-folder"))
        programs = web._available_phone_programs(self.env)
        self.assertEqual(1, len(programs))
        self.assertEqual("target-app", programs[0]["name"])
        self.assertTrue(os.path.samefile(self.project, programs[0]["path"]))

    def test_symlink_cannot_escape_a_configured_project_root(self):
        project_root = os.path.join(self.root, "projects")
        outside = os.path.join(self.root, "outside-repo")
        os.makedirs(project_root)
        os.makedirs(os.path.join(outside, ".git"))
        link = os.path.join(project_root, "linked-outside")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.assertFalse(web._path_within_root(
                os.path.realpath(project_root), os.path.realpath(outside)))
            return
        env = dict(self.env, FLEXFACTOR_PROJECT_ROOTS=project_root)
        self.assertEqual([], web._available_phone_programs(env))

    def test_local_phone_launch_and_provider_mutation_are_retired(self):
        spawned = []
        cases = (
            lambda: web.start_phone_run(
                {"program": self.project, "mode": "audit", "provider": "openai"},
                env=self.env, popen=lambda *a, **k: spawned.append(a)),
            lambda: web.save_phone_provider(
                {"provider": "openai", "api_key": "secret-value-long-enough"},
                env=self.env,
                provider_path=os.path.join(self.root, "providers.json")),
            lambda: web.start_phone_provider_install(
                {"provider": "openai"}, env=self.env,
                popen=lambda *a, **k: spawned.append(a)),
        )
        for operation in cases:
            with self.subTest(operation=operation), \
                    self.assertRaisesRegex(ValueError, "retired"):
                operation()
        self.assertEqual(spawned, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "providers.json")))

    def test_phone_launch_state_is_permanently_unavailable(self):
        state = web.phone_launch_state(self.env)
        self.assertFalse(state["available"])
        self.assertEqual(state["programs"], [])
        self.assertEqual(state["providers"], [])
        self.assertIn("signed FlexFactor Mobile", state["policy"])
        self.assertIn("authoritative default branch", state["policy"])

    def test_retired_http_endpoints_require_auth_then_return_gone(self):
        web.Handler.token = "retired-phone-token"
        web.Handler.sampler = object()
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/api/launch", "/api/provider", "/api/provider/install"):
                url = f"http://127.0.0.1:{server.server_port}{path}"

                def post(token):
                    request = urllib.request.Request(
                        url, data=b"{}", method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": "Bearer " + token},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=5) as response:
                            return response.status, response.read().decode()
                    except urllib.error.HTTPError as error:
                        return error.code, error.read().decode()

                self.assertEqual(401, post("wrong")[0])
                status, body = post("retired-phone-token")
                self.assertEqual(410, status)
                self.assertIn("retired", json.loads(body)["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


class DashboardAndManagedMobileTests(unittest.TestCase):
    def test_a_stalled_programs_own_counters_beat_the_shared_file_clock(self):
        stalled_for = web.STALL_S * 10

        class Sampler:
            status_path = "unused"

            @staticmethod
            def rate_per_min(name):
                return 0.0

            @staticmethod
            def counters_moved_ago(name):
                return stalled_for

            @staticmethod
            def durable(program):
                return {"attempts": 1, "resumes": 0, "landed": 0}

            @staticmethod
            def velocity(name):
                return []

        original = web.dash.read_status
        web.dash.read_status = lambda path: ([{
            "name": "stuck", "phase": "fixing", "done": False, "dir": "/repo",
        }], time.time())
        try:
            state = web.build_state(Sampler())
        finally:
            web.dash.read_status = original
        row = next(item for item in state["programs"] if item["name"] == "stuck")
        self.assertEqual("quiet", row["liveness"])
        self.assertFalse(state["launch"]["available"])

    def test_reaper_clears_only_the_exited_child_pid(self):
        with tempfile.TemporaryDirectory() as root:
            pid_path = os.path.join(root, "audit.pid")
            with open(pid_path, "w", encoding="utf-8") as stream:
                stream.write("31337\n")
            waited = threading.Event()

            class Process:
                pid = 31337

                @staticmethod
                def wait():
                    waited.set()
                    return 0

            web._start_audit_reaper(Process(), pid_path)
            self.assertTrue(waited.wait(2))
            deadline = time.time() + 2
            while os.path.exists(pid_path) and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(os.path.exists(pid_path))

    def test_android_launcher_is_native_managed_and_has_all_four_modes(self):
        path = os.path.join(
            os.path.dirname(__file__), "android", "app", "src", "main", "java",
            "com", "firer", "console", "flexfactor", "MainActivity.java",
        )
        with open(path, encoding="utf-8") as stream:
            activity = stream.read()
        for forbidden in ("android.webkit", "WebView", "com.termux", "RUN_COMMAND"):
            self.assertNotIn(forbidden, activity)
        self.assertIn('"Sign in with GitHub"', activity)
        self.assertIn("startGitHubSignIn", activity)
        self.assertIn('addMode("1 · Refactor a file"', activity)
        self.assertIn('addMode("2 · Scout improvements"', activity)
        self.assertIn('addMode("3 · Audit and repair"', activity)
        self.assertIn('addMode("4 · Make production ready"', activity)
        self.assertIn("Choose up to 30 repositories", activity)
        self.assertIn("run one at a time", activity)

    def test_android_main_release_creates_the_exact_version_tag(self):
        path = os.path.join(
            os.path.dirname(__file__), ".github", "workflows", "android-client.yml")
        with open(path, encoding="utf-8") as stream:
            workflow = stream.read()
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("expected_sha:", workflow)
        self.assertIn('test "$EXPECTED_SHA" = "$GITHUB_SHA"', workflow)
        self.assertIn(
            '"$GITHUB_EVENT_NAME" = "workflow_dispatch"', workflow)
        self.assertIn('"$tag_commit" != "$GITHUB_SHA"', workflow)
        self.assertIn(
            "The existing release tag is not the authorized main commit", workflow)
        push_trigger = workflow.split("  push:", 1)[1].split("  pull_request:", 1)[0]
        pull_request_trigger = workflow.split("  pull_request:", 1)[1].split(
            "  workflow_dispatch:", 1)[0]
        self.assertNotIn("paths:", push_trigger)
        self.assertIn("paths:", pull_request_trigger)
        self.assertIn("PREVIOUS_MAIN_SHA: ${{ github.event.before }}", workflow)
        self.assertIn('"${PREVIOUS_MAIN_SHA}:android/app/build.gradle.kts"', workflow)
        self.assertIn("git ls-remote --exit-code --tags origin", workflow)
        self.assertIn('if [ "$tag_lookup_rc" -eq 2 ]', workflow)
        self.assertIn('elif [ "$tag_lookup_rc" -ne 0 ]', workflow)
        self.assertIn('"refs/tags/$release_tag"', workflow)
        self.assertIn("releases/tags/$release_tag", workflow)
        self.assertIn('if [ "$release_status" = 404 ]', workflow)
        self.assertIn('elif [ "$release_status" = 200 ]', workflow)
        self.assertIn('source_sha="$tag_commit"', workflow)
        self.assertIn('source_sha="$GITHUB_SHA"', workflow)
        self.assertIn("source_sha: ${{ steps.plan.outputs.source_sha }}", workflow)
        self.assertIn("ref: ${{ needs.release-plan.outputs.source_sha }}", workflow)
        self.assertNotIn('"${GITHUB_SHA}^:android/app/build.gradle.kts"', workflow)
        self.assertIn('if [ "$tag_commit" != "$SOURCE_SHA" ]', workflow)
        self.assertIn('gh release create "$RELEASE_TAG"', workflow)
        self.assertIn("ANDROID_KEYSTORE_BASE64", workflow)
        self.assertIn(".engine_ref == $engine", workflow)

        command_path = os.path.join(
            os.path.dirname(__file__), ".github", "workflows", "release-command.yml")
        with open(command_path, encoding="utf-8") as stream:
            command = stream.read()
        self.assertIn("github.actor == github.repository_owner", command)
        self.assertIn("author_association == 'OWNER'", command)
        self.assertIn("/release-android ", command)
        self.assertIn("requested_sha", command)
        self.assertIn('test "$requested_sha" = "$main_sha"', command)
        self.assertIn("gh workflow run android-client.yml", command)

    def test_mobile_refactor_does_not_delete_a_tracked_backup_file(self):
        path = os.path.join(
            os.path.dirname(__file__), ".github", "workflows", "mobile-run.yml")
        with open(path, encoding="utf-8") as stream:
            workflow = stream.read()
        self.assertNotIn('backup_path="target/$TARGET_FILE.bak"', workflow)
        self.assertNotIn("refactor-existing.bak", workflow)


if __name__ == "__main__":
    unittest.main()
