"""Contracts for the dashboard and the retired local phone launcher.

Phone execution belongs exclusively to the signed managed Android client. The
web dashboard remains an authenticated viewer/steering surface and must never
reintroduce local-only mutation or provider-choice endpoints.
"""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import flexfactor_web as web


class RetiredPhoneLauncherTests(unittest.TestCase):
    def test_retired_local_launcher_implementation_is_not_shipped(self):
        for name in (
            "start_phone_run",
            "save_phone_provider",
            "start_phone_provider_install",
            "_available_phone_programs",
            "_provider_readiness",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(web, name))

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
        self.assertNotIn("launch", state)

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
        self.assertGreaterEqual(
            workflow.count('test "$GITHUB_ACTOR" = "$GITHUB_REPOSITORY_OWNER"'), 2)
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
        self.assertNotIn("git ls-remote --exit-code --tags origin", workflow)
        self.assertNotIn("git fetch --no-tags origin", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/git/ref/tags/$release_tag"', workflow
        )
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/git/ref/heads/main"', workflow
        )
        self.assertIn('if [ "$tag_lookup_rc" -ne 0 ]; then', workflow)
        self.assertIn("Only a proven 404 means the tag is absent", workflow)
        self.assertIn(r"\(HTTP 404\)", workflow)
        self.assertIn("releases/tags/$release_tag", workflow)
        self.assertIn('if [ "$release_status" = 404 ]', workflow)
        self.assertIn('elif [ "$release_status" = 200 ]', workflow)
        self.assertIn('source_sha="$tag_commit"', workflow)
        self.assertIn('source_sha="$GITHUB_SHA"', workflow)
        self.assertIn("source_sha: ${{ steps.plan.outputs.source_sha }}", workflow)
        self.assertIn(
            "authorized_main_sha: ${{ steps.plan.outputs.authorized_main_sha }}",
            workflow,
        )
        self.assertIn("ref: ${{ needs.release-plan.outputs.source_sha }}", workflow)
        self.assertNotIn('"${GITHUB_SHA}^:android/app/build.gradle.kts"', workflow)
        self.assertIn('test "$tag_commit" = "$SOURCE_SHA"', workflow)
        self.assertIn('gh release create "$RELEASE_TAG"', workflow)
        self.assertIn("ANDROID_KEYSTORE_BASE64", workflow)
        self.assertIn(".engine_ref == $engine", workflow)
        self.assertIn("Reauthorize a manual rerun and live main before signing", workflow)
        self.assertIn(
            'test "$GITHUB_TRIGGERING_ACTOR" = "$GITHUB_REPOSITORY_OWNER"',
            workflow,
        )
        self.assertIn("Revalidate live main before publication", workflow)
        self.assertGreaterEqual(
            workflow.count('test "$live_main" = "$AUTHORIZED_MAIN_SHA"'), 2)
        self.assertIn('source_sha="$tag_commit"', workflow)
        self.assertIn('authorized_main_sha="$GITHUB_SHA"', workflow)
        tag_plan = workflow.index('if [ "$GITHUB_REF_TYPE" = "tag" ]; then')
        resolve_live_main = workflow.index(
            "authorized_main_sha=$(gh api", tag_plan
        )
        main_ref = workflow.index(
            '"repos/$GITHUB_REPOSITORY/git/ref/heads/main"', resolve_live_main
        )
        prove_tag_containment = workflow.index(
            'git merge-base --is-ancestor "$GITHUB_SHA" "$authorized_main_sha"',
            main_ref,
        )
        self.assertLess(resolve_live_main, main_ref)
        self.assertLess(main_ref, prove_tag_containment)
        self.assertIn("require_exact_tag()", workflow)
        self.assertGreaterEqual(workflow.count("require_exact_tag"), 8)
        self.assertIn("--draft", workflow)
        self.assertIn("--verify-tag", workflow)
        self.assertIn("--draft=false", workflow)
        complete_guard = workflow.index(
            'if [ "$release_complete_rc" -eq 0 ]; then'
        )
        draft_mutation = workflow.index(
            'gh release edit "$RELEASE_TAG"', complete_guard
        )
        self.assertLess(complete_guard, draft_mutation)
        self.assertIn(
            "is already complete; no publication mutation required", workflow
        )
        self.assertIn(
            "JSON failure aborts here; it is never conflated", workflow
        )
        self.assertIn(
            'elif [ "$release_complete_rc" -ne 1 ]; then', workflow
        )
        self.assertIn(
            'all(.assets[]; type == "object"', workflow
        )
        self.assertIn(
            "group: android-release-${{ needs.release-plan.outputs.release_tag }}",
            workflow,
        )

    def test_mobile_refactor_does_not_delete_a_tracked_backup_file(self):
        path = os.path.join(
            os.path.dirname(__file__), ".github", "workflows", "mobile-run.yml")
        with open(path, encoding="utf-8") as stream:
            workflow = stream.read()
        self.assertNotIn('backup_path="target/$TARGET_FILE.bak"', workflow)
        self.assertNotIn("refactor-existing.bak", workflow)


if __name__ == "__main__":
    unittest.main()
