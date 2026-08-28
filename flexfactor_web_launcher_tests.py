"""Executable contract for the authenticated on-phone run launcher."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import flexfactor_web as web


class PhoneLauncherTests(unittest.TestCase):
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

    def test_discovers_only_git_repositories_inside_configured_roots(self):
        os.makedirs(os.path.join(self.root, "ordinary-folder"))
        outside = tempfile.mkdtemp(dir=self.tmp.name)
        programs = web._available_phone_programs(self.env)
        self.assertEqual([{"name": "target-app", "path": self.project}], programs)
        self.assertNotIn(outside, [item["path"] for item in programs])

    def test_provider_readiness_never_exposes_secret_values(self):
        readiness = web._provider_readiness(
            self.env,
            module_available=lambda name: name == "openai",
            ollama_reachable=lambda: False,
        )
        self.assertTrue(next(item for item in readiness if item["name"] == "openai")["ready"])
        self.assertNotIn(self.env["OPENAI_API_KEY"], json.dumps(readiness))

    def test_launch_uses_exact_argv_and_never_pushes_or_merges(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return SimpleNamespace(pid=424242)

        pid_path = os.path.join(self.root, "run", "audit.pid")
        log_path = os.path.join(self.root, "run", "audit.log")
        result = web.start_phone_run(
            {"program": self.project, "mode": "prodready", "provider": "openai",
             "max_cost": 7},
            env=self.env,
            programs=[{"name": "target-app", "path": self.project}],
            readiness=[{"name": "openai", "ready": True, "detail": "ready"}],
            pid_path=pid_path,
            log_path=log_path,
            popen=fake_popen,
        )
        command = captured["command"]
        self.assertEqual(424242, result["pid"])
        self.assertIsInstance(command, list)
        self.assertEqual(self.project, command[command.index("--program") + 1])
        self.assertIn("--no-push", command)
        self.assertIn("--no-merge", command)
        self.assertIn("--single", command)
        self.assertNotIn("shell", captured["kwargs"])
        with open(pid_path, encoding="utf-8") as fh:
            self.assertEqual("424242", fh.read().strip())

    def test_rejects_path_escape_unready_provider_and_invalid_cost(self):
        allowed = [{"name": "target-app", "path": self.project}]
        ready = [{"name": "openai", "ready": True, "detail": "ready"}]
        cases = [
            ({"program": os.path.join(self.project, ".."), "mode": "audit",
              "provider": "openai", "max_cost": 10}, ready, "allowed repository"),
            ({"program": self.project, "mode": "audit", "provider": "openai",
              "max_cost": 10}, [{"name": "openai", "ready": False,
                                  "detail": "missing SDK"}], "not ready"),
            ({"program": self.project, "mode": "audit", "provider": "openai",
              "max_cost": 999}, ready, "between 1 and 150"),
        ]
        for body, readiness, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                web.start_phone_run(
                    body, env=self.env, programs=allowed, readiness=readiness,
                    pid_path=os.path.join(self.root, "run", "audit.pid"),
                    log_path=os.path.join(self.root, "run", "audit.log"),
                )

    def test_rejects_second_run_while_pid_is_alive(self):
        pid_path = os.path.join(self.root, "audit.pid")
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        with self.assertRaisesRegex(ValueError, "already running"):
            web.start_phone_run(
                {"program": self.project, "mode": "audit", "provider": "openai",
                 "max_cost": 10},
                env=self.env,
                programs=[{"name": "target-app", "path": self.project}],
                readiness=[{"name": "openai", "ready": True, "detail": "ready"}],
                pid_path=pid_path,
                log_path=os.path.join(self.root, "audit.log"),
                popen=lambda *args, **kwargs: self.fail("must not spawn"),
            )

    def test_http_launch_requires_exact_token(self):
        original = web.start_phone_run
        web.start_phone_run = lambda body: {
            "ok": True, "pid": 12, "program": "target-app", "mode": "audit",
            "provider": "openai", "max_cost": 10,
        }
        web.Handler.token = "phone-launch-token"
        web.Handler.sampler = object()
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:{}/api/launch".format(server.server_port)

            def post(token):
                request = urllib.request.Request(
                    url, data=b'{"mode":"audit"}', method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + token},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            self.assertEqual(401, post("wrong"))
            self.assertEqual(201, post("phone-launch-token"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            web.start_phone_run = original

    def test_page_exposes_launcher_and_local_only_policy(self):
        self.assertIn("Start on this phone", web.PAGE)
        self.assertIn("cannot push or merge", web.PAGE)
        self.assertIn("/api/launch", web.PAGE)


if __name__ == "__main__":
    unittest.main()
