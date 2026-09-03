"""Executable contract for authenticated operator steering."""
import os
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import flexfactor as ff
import flexfactor_tests as ft
import flexfactor_steering as fs
import flexfactor_web as web

class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.project = os.path.join(self.root, "Target App")
        os.makedirs(self.project)
    def tearDown(self):
        self.tmp.cleanup()
    def test_submit_claim_context_and_complete(self):
        item = fs.submit("Target", self.project, "Add a printable report and test the download.", root=self.root)
        self.assertEqual("pending", item["status"])
        context, active, new = fs.refresh_context("PURPOSE", "Target", self.project, "run-1", root=self.root)
        self.assertEqual([item["id"]], active)
        self.assertEqual(active, new)
        self.assertIn("printable report", context)
        context2, active2, new2 = fs.refresh_context(context, "Target", self.project, "run-1", root=self.root)
        self.assertEqual(active, active2)
        self.assertEqual([], new2)
        self.assertEqual(1, context2.count(item["id"]))
        fs.finish("Target", self.project, "run-1", active, completed=True, detail="verified", root=self.root)
        self.assertEqual("completed", fs.list_comments("Target", self.project, root=self.root)[0]["status"])
        context3, active3, _ = fs.refresh_context(context2, "Target", self.project, "run-2", root=self.root)
        self.assertEqual([], active3)
        self.assertNotIn(fs._BEGIN, context3)
    def test_interrupted_run_comment_is_reclaimed(self):
        item = fs.submit("Target", self.project, "Keep the existing login.", root=self.root)
        _, ids1, _ = fs.refresh_context("", "Target", self.project, "dead-run", root=self.root)
        _, ids2, new2 = fs.refresh_context("", "Target", self.project, "replacement-run", root=self.root)
        self.assertEqual(ids1, ids2)
        self.assertEqual([item["id"]], new2)
    def test_project_scope_is_exact(self):
        fs.submit("Target", self.project, "Only app one", root=self.root)
        other = os.path.join(self.root, "Other")
        os.makedirs(other)
        self.assertEqual([], fs.list_comments("Target", other, root=self.root))
        self.assertEqual([], fs.list_comments("Other", self.project, root=self.root))
    def test_validation(self):
        for value in ("", "x" * (fs.MAX_COMMENT_CHARS + 1), "bad\x00text"):
            with self.assertRaises(ValueError):
                fs.submit("Target", self.project, value, root=self.root)

    def test_multi_program_prompt_routes_named_and_shared_requirements(self):
        grant = os.path.join(self.root, "GrantFlow")
        sermon = os.path.join(self.root, "sermonsmith")
        os.makedirs(grant)
        os.makedirs(sermon)
        routed = fs.route_session_prompt(
            "GrantFlow: repair billing. Also test tier enforcement. "
            "SermonSmith: fix the Bible reader. All programs must push verified work.",
            [("GrantFlow", grant), ("SermonSmith", sermon)],
        )
        by_name = {row["program"]: row["instruction"] for row in routed["routes"]}
        self.assertIn("repair billing", by_name["GrantFlow"])
        self.assertIn("tier enforcement", by_name["GrantFlow"])
        self.assertNotIn("Bible reader", by_name["GrantFlow"])
        self.assertIn("Bible reader", by_name["SermonSmith"])
        self.assertIn("push verified work", by_name["GrantFlow"])
        self.assertIn("push verified work", by_name["SermonSmith"])

    def test_session_prompt_is_durable_and_claimed_by_each_target_when_worked(self):
        other = os.path.join(self.root, "Other")
        os.makedirs(other)
        receipt = fs.submit_session_prompt(
            "Target: keep login. Other: add exports.",
            [("Target", self.project), ("Other", other)],
            root=self.root,
        )
        self.assertEqual(2, len(receipt["submission_ids"]))
        target_context, target_ids, _ = fs.refresh_context(
            "PURPOSE", "Target", self.project, "run-target", root=self.root)
        other_context, other_ids, _ = fs.refresh_context(
            "PURPOSE", "Other", other, "run-other", root=self.root)
        self.assertIn("keep login", target_context)
        self.assertNotIn("add exports", target_context)
        self.assertIn("add exports", other_context)
        self.assertEqual(1, len(target_ids))
        self.assertEqual(1, len(other_ids))
        target_row = fs.list_comments("Target", self.project, root=self.root)[0]
        self.assertEqual(receipt["session_id"], target_row["session_id"])
        self.assertEqual("multi-program-session", target_row["scope"])

    def test_guidance_is_program_scoped_durable_and_injected_each_run(self):
        saved = fs.set_guidance(
            "Target", self.project,
            "Keep the existing login and make every user journey genuinely usable.",
            root=self.root,
        )
        self.assertTrue(os.path.isfile(fs.guidance_path("Target", self.project, self.root)))
        self.assertEqual(saved["prompt"], fs.get_guidance(
            "Target", self.project, root=self.root)["prompt"])
        first, active, _ = fs.refresh_context(
            "PURPOSE", "Target", self.project, "run-1", root=self.root)
        self.assertEqual([], active)
        self.assertIn(fs._GUIDANCE_BEGIN, first)
        self.assertIn("genuinely usable", first)
        second, _, _ = fs.refresh_context(first, "Target", self.project, "run-2",
                                          root=self.root)
        self.assertEqual(1, second.count(fs._GUIDANCE_BEGIN))
        self.assertIn("genuinely usable", second)
        other = os.path.join(self.root, "Other")
        os.makedirs(other)
        self.assertIsNone(fs.get_guidance("Target", other, root=self.root))
        self.assertTrue(fs.clear_guidance("Target", self.project, root=self.root))
        cleared, _, _ = fs.refresh_context(second, "Target", self.project, "run-3",
                                           root=self.root)
        self.assertNotIn(fs._GUIDANCE_BEGIN, cleared)

    def test_dashboard_guidance_helpers_persist_and_clear(self):
        original = fs.DEFAULT_ROOT
        fs.DEFAULT_ROOT = self.root
        try:
            ok, message = __import__("flexfactor_dashboard").save_guidance(
                "Target", self.project, "Prioritize a complete end-to-end workflow.")
            self.assertTrue(ok, message)
            self.assertIn("end-to-end", __import__("flexfactor_dashboard").guidance_value(
                "Target", self.project))
            ok, message = __import__("flexfactor_dashboard").clear_guidance(
                "Target", self.project)
            self.assertTrue(ok, message)
            self.assertEqual("", __import__("flexfactor_dashboard").guidance_value(
                "Target", self.project))
        finally:
            fs.DEFAULT_ROOT = original

    def test_dashboard_can_auto_route_one_live_session_prompt(self):
        other = os.path.join(self.root, "Other")
        os.makedirs(other)
        original = fs.DEFAULT_ROOT
        fs.DEFAULT_ROOT = self.root
        try:
            ok, message = __import__("flexfactor_dashboard").submit_session_steering(
                [("Target", self.project), ("Other", other)],
                "Target: test login. Other: repair exports.",
            )
            self.assertTrue(ok, message)
            self.assertIn("2 program", message)
            self.assertIn("test login", fs.list_comments(
                "Target", self.project, root=self.root)[0]["comment"])
            self.assertIn("repair exports", fs.list_comments(
                "Other", other, root=self.root)[0]["comment"])
        finally:
            fs.DEFAULT_ROOT = original

    def test_web_dashboard_identifies_the_on_phone_engine(self):
        self.assertEqual(
            "this phone",
            web._host_label({"TERMUX_VERSION": "0.119.0", "HOSTNAME": "localhost"}),
        )
        self.assertEqual(
            "this phone",
            web._host_label({"PREFIX": "/data/data/com.termux/files/usr"}),
        )

    def test_web_dashboard_host_label_has_an_explicit_override(self):
        self.assertEqual(
            "my phone",
            web._host_label({
                "FLEXFACTOR_HOST_LABEL": "my phone",
                "TERMUX_VERSION": "0.119.0",
            }),
        )
        self.assertEqual("build-host", web._host_label({"HOSTNAME": "build-host"}))
    def test_reassigning_DEFAULT_ROOT_actually_isolates(self):
        """Patching the module default must redirect writes, not decorate them.

        `root: str = DEFAULT_ROOT` bound the owner's real journal directory at
        import time, so every caller that reassigned DEFAULT_ROOT - the desktop
        dashboard's self-test among them - still wrote into
        ~/.flexfactor/steering. Measured 2026-08-28: two live comments
        ("prioritize the auth bugs") sat in the real journal pointing at
        tempdirs, and a matching program's next audit would have claimed them as
        owner instructions."""
        real = fs.DEFAULT_ROOT
        redirected = os.path.join(self.root, "redirected")
        fs.DEFAULT_ROOT = redirected
        try:
            fs.submit("prog", self.root, "only in the redirected journal")
            self.assertTrue(
                os.path.isfile(fs.journal_path("prog", self.root, redirected)),
                "the write must land under the reassigned root")
            self.assertFalse(
                os.path.isdir(os.path.join(real, "prog")),
                "and nothing may appear under the import-time default")
            self.assertEqual(
                1, len(fs.list_comments("prog", self.root)),
                "reads must follow the same reassigned root as writes")
        finally:
            fs.DEFAULT_ROOT = real

    def test_context_replaces_prior_snapshot(self):
        combined = fs.merge_context("BASE", fs.steering_block([{"id": "one", "comment": "first"}]))
        refreshed = fs.merge_context(combined, fs.steering_block([{"id": "two", "comment": "second"}]))
        self.assertIn("BASE", refreshed)
        self.assertNotIn("[one]", refreshed)
        self.assertEqual(1, refreshed.count(fs._BEGIN))
        self.assertIn("[two]", refreshed)

    def test_http_auth_exact_target_audit_pickup_and_terminal_receipt(self):
        """One real path: HTTP boundary -> journal -> audit -> final receipt."""
        helper = ft.AuditPipelineIntegrationTests()
        with ft._RepoFixture({"pyproject.toml": "[project]\nname='target'\nversion='1'\n",
                              "app.py": "value = 1\n"}, production=True) as project:
            args = helper._args(["prodready", "--program", project,
                                 "--no-bootstrap", "--no-preflight",
                                 "--no-dashboard", "--no-tests", "--no-e2e",
                                 "--no-full-suite"])
            original = (fs.submit, fs.refresh_context, fs.summary, fs.finish,
                        web.build_state, web.steering.submit)

            def submit(program, project_dir, comment, *, source="dashboard"):
                return original[0](program, project_dir, comment,
                                   source=source, root=self.root)

            def refresh(context, program, project_dir, run_id):
                return original[1](context, program, project_dir, run_id,
                                   root=self.root)

            def summary(program, project_dir):
                return original[2](program, project_dir, root=self.root)

            def finish(program, project_dir, run_id, ids, *, completed, detail=""):
                return original[3](program, project_dir, run_id, ids,
                                   completed=completed, detail=detail, root=self.root)

            fs.submit, fs.refresh_context, fs.summary, fs.finish = \
                submit, refresh, summary, finish
            web.steering.submit = submit
            web.build_state = lambda sampler: {
                "programs": [{"name": os.path.basename(project), "dir": project}]}
            web.Handler.token = "operator-e2e-token"
            web.Handler.sampler = object()
            server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/steering"

                def post(token, target=project):
                    body = json.dumps({
                        "program": os.path.basename(project),
                        "project_dir": target,
                        "comment": "Keep login and add a printable report.",
                    }).encode("utf-8")
                    request = urllib.request.Request(
                        url, data=body, method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {token}"})
                    try:
                        with urllib.request.urlopen(request, timeout=5) as response:
                            return response.status, json.loads(response.read())
                    except urllib.error.HTTPError as exc:
                        return exc.code, json.loads(exc.read())

                self.assertEqual(401, post("wrong-token")[0])
                wrong_target = os.path.join(self.root, "not-active")
                self.assertEqual(400, post("operator-e2e-token", wrong_target)[0])
                status, payload = post("operator-e2e-token")
                self.assertEqual(201, status)
                steering_id = payload["comment"]["id"]

                class ContextRecordingProvider(ft._StubProvider):
                    def structured(self, system, prompt, schema, max_tokens=8000,
                                   model=None, **kw):
                        self.calls.append(prompt)
                        if schema is ff.PROGRAM_UNDERSTANDING_SCHEMA:
                            # This end-to-end fixture predates the mandatory
                            # purpose-understanding gate.  Cite one exact ref
                            # from the rendered evidence instead of bypassing
                            # that gate with a schema-incomplete fake response.
                            evidence_ref = next(
                                line.split("] ", 1)[1].split(": ", 1)[0]
                                for line in prompt.splitlines()
                                if line.startswith("- [") and "] " in line
                            )
                            return {
                                "purpose": "Provide the target application's documented outcome",
                                "primary_users": ["Target application operators"],
                                "core_journeys": ["Run the application and obtain its output"],
                                "acceptance_criteria": [
                                    "The documented application journey completes successfully"
                                ],
                                "evidence_refs": [evidence_ref],
                            }
                        return {"findings": [], "summary": "clean"}

                stub = ContextRecordingProvider()
                state = os.path.join(self.root, "state")

                def offline_competitor_gate(**_kwargs):
                    # This test owns the HTTP steering boundary, not public
                    # competitor discovery.  Return a completed, empty research
                    # receipt so a developer's configured Scout/Repo Rewards
                    # endpoints can never leak into the unit test process.
                    return {
                        "research": {
                            "researched_at": "test-fixture",
                            "target": 3,
                            "competitors": [],
                            "verified": 0,
                            "sources_used": [],
                            "sources_skipped": {
                                "test": "public research replaced by offline fixture"
                            },
                            "coverage_note": "offline steering fixture; no competitors",
                        },
                        "findings": [],
                        "purpose_files": [],
                        "applied": [],
                        "unverified": [],
                        "notes": [],
                        "dirty_abort": False,
                        "committed": False,
                        "attempted": True,
                    }

                with ft._patched(ff, "BRAIN_PATH", os.path.join(state, "brain.json")), \
                     ft._patched(ff, "RUNS_PATH", os.path.join(state, "runs")), \
                     ft._patched(ff, "STATUS_PATH", os.path.join(state, "status.json")), \
                     ft._patched(ff, "build_audit_providers",
                                 lambda a, m=None: [("stub", stub)]), \
                     ft._patched(ff, "_run_top_competitor_gate",
                                 offline_competitor_gate), \
                     ft._patched(ff, "_full_gate",
                                 lambda d, s: (None, "offline E2E build stub")):
                    result = ff.audit_one_program(project, args, 0, 1, None)

                self.assertIn(steering_id, result["steering_comment_ids"])
                self.assertTrue(any("printable report" in call for call in stub.calls))
                receipt = original[2](os.path.basename(project), project,
                                      root=self.root)["latest"][0]
                self.assertEqual("needs-attention", receipt["status"])
                self.assertIn("run ended partial", receipt["detail"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                (fs.submit, fs.refresh_context, fs.summary, fs.finish,
                 web.build_state, web.steering.submit) = original

if __name__ == "__main__":
    unittest.main()
