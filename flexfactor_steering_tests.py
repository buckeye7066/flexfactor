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
                              "app.py": "value = 1\n"}) as project:
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
                        return {"findings": [], "summary": "clean"}

                stub = ContextRecordingProvider()
                state = os.path.join(self.root, "state")
                with ft._patched(ff, "BRAIN_PATH", os.path.join(state, "brain.json")), \
                     ft._patched(ff, "RUNS_PATH", os.path.join(state, "runs")), \
                     ft._patched(ff, "STATUS_PATH", os.path.join(state, "status.json")), \
                     ft._patched(ff, "build_audit_providers",
                                 lambda a, m=None: [("stub", stub)]), \
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
