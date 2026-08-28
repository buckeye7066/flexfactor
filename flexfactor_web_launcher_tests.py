"""Executable contract for the authenticated on-phone run launcher."""
import json
import os
import tempfile
import threading
import time
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
        self.provider_path = os.path.join(self.root, "private", "providers.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovers_only_git_repositories_inside_configured_roots(self):
        os.makedirs(os.path.join(self.root, "ordinary-folder"))
        outside = tempfile.mkdtemp(dir=self.tmp.name)
        programs = web._available_phone_programs(self.env)
        self.assertEqual(1, len(programs))
        self.assertEqual("target-app", programs[0]["name"])
        self.assertTrue(os.path.samefile(self.project, programs[0]["path"]))
        self.assertFalse(any(os.path.samefile(outside, item["path"])
                             for item in programs))

    def test_symlink_cannot_escape_configured_project_root(self):
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

    def test_provider_readiness_never_exposes_secret_values(self):
        readiness = web._provider_readiness(
            self.env,
            module_available=lambda name: name == "openai",
            ollama_reachable=lambda: False,
        )
        self.assertTrue(next(item for item in readiness if item["name"] == "openai")["ready"])
        self.assertNotIn(self.env["OPENAI_API_KEY"], json.dumps(readiness))

    def test_provider_key_is_saved_privately_without_response_leak(self):
        secret = "test-provider-secret-that-must-not-leak"
        result = web.save_phone_provider(
            {"provider": "openai", "api_key": secret},
            env=self.env,
            provider_path=self.provider_path,
        )
        self.assertTrue(result["configured"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(secret, web._load_phone_provider_env(
            self.provider_path)["OPENAI_API_KEY"])
        blank_env = dict(self.env, OPENAI_API_KEY="")
        self.assertEqual(secret, web._effective_provider_env(
            blank_env, self.provider_path)["OPENAI_API_KEY"])
        if os.name != "nt":
            self.assertEqual(0o600, os.stat(self.provider_path).st_mode & 0o777)
            self.assertEqual(0o700, os.stat(os.path.dirname(
                self.provider_path)).st_mode & 0o777)

    def test_provider_setup_rejects_invalid_inputs_without_writing(self):
        cases = [
            ({"provider": "ollama", "api_key": "long-enough-secret-value"},
             "openai or anthropic"),
            ({"provider": "openai", "api_key": "short"}, "invalid format"),
            ({"provider": "openai", "api_key": "bad-secret-value\n"},
             "invalid format"),
        ]
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, message):
                web.save_phone_provider(
                    body, env=self.env, provider_path=self.provider_path)
        self.assertFalse(os.path.exists(self.provider_path))

    def test_concurrent_provider_saves_retain_both_keys(self):
        barrier = threading.Barrier(3)
        errors = []

        def save(provider, secret):
            try:
                barrier.wait()
                web.save_phone_provider(
                    {"provider": provider, "api_key": secret},
                    env=self.env, provider_path=self.provider_path)
            except Exception as exc:  # noqa: BLE001 - surfaced after both threads join
                errors.append(exc)

        first = threading.Thread(target=save, args=(
            "openai", "concurrent-openai-secret-value"))
        second = threading.Thread(target=save, args=(
            "anthropic", "concurrent-anthropic-secret-value"))
        first.start()
        second.start()
        barrier.wait()
        first.join(5)
        second.join(5)
        self.assertEqual([], errors)
        configured = web._load_phone_provider_env(self.provider_path)
        self.assertIn("OPENAI_API_KEY", configured)
        self.assertIn("ANTHROPIC_API_KEY", configured)

    def test_saved_provider_key_reaches_child_only_through_environment(self):
        secret = "saved-provider-secret-that-must-not-leak"
        env = dict(self.env)
        env.pop("OPENAI_API_KEY")
        web.save_phone_provider(
            {"provider": "openai", "api_key": secret},
            env=env,
            provider_path=self.provider_path,
        )
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return SimpleNamespace(pid=737373)

        run_dir = os.path.join(self.root, "configured-run")
        result = web.start_phone_run(
            {"program": self.project, "mode": "audit", "provider": "openai",
             "max_cost": 3},
            env=env,
            programs=[{"name": "target-app", "path": self.project}],
            readiness=[{"name": "openai", "ready": True, "detail": "ready"}],
            provider_path=self.provider_path,
            pid_path=os.path.join(run_dir, "audit.pid"),
            log_path=os.path.join(run_dir, "audit.log"),
            lock_path=os.path.join(run_dir, "audit.lock"),
            popen=fake_popen,
            start_reaper=lambda process, path: None,
        )
        self.assertEqual(secret, captured["env"]["OPENAI_API_KEY"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, json.dumps(captured["command"]))
        with open(os.path.join(run_dir, "audit.log"), encoding="utf-8") as fh:
            self.assertNotIn(secret, fh.read())

    def test_provider_support_installer_uses_fixed_allowlisted_argv(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return SimpleNamespace(pid=838383)

        run_dir = os.path.join(self.root, "provider-install")
        result = web.start_phone_provider_install(
            {"provider": "openai"},
            env=self.env,
            pid_path=os.path.join(run_dir, "provider.pid"),
            log_path=os.path.join(run_dir, "provider.log"),
            popen=fake_popen,
            start_reaper=lambda process, path: captured.update(
                reaped=(process.pid, path)),
        )
        self.assertEqual(838383, result["pid"])
        self.assertEqual("openai", captured["command"][-1])
        self.assertEqual("bash", captured["command"][0])
        self.assertTrue(os.path.samefile(
            captured["command"][1],
            os.path.join(os.path.dirname(__file__), "scripts", "phone",
                         "install-provider.sh"),
        ))
        self.assertNotIn("shell", captured["kwargs"])
        self.assertNotIn("OPENAI_API_KEY", captured["kwargs"]["env"])
        self.assertEqual((838383, os.path.join(run_dir, "provider.pid")),
                         captured["reaped"])

    def test_provider_install_and_audit_are_mutually_exclusive(self):
        audit_pid = os.path.join(self.root, "active-audit.pid")
        with open(audit_pid, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        with self.assertRaisesRegex(ValueError, "while audit pid"):
            web.start_phone_provider_install(
                {"provider": "openai"}, env=self.env,
                audit_pid_path=audit_pid,
                pid_path=os.path.join(self.root, "provider.pid"),
                log_path=os.path.join(self.root, "provider.log"),
                popen=lambda *args, **kwargs: self.fail("must not spawn"),
            )

        install_pid = os.path.join(self.root, "active-install.pid")
        with open(install_pid, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        with self.assertRaisesRegex(ValueError, "provider installation pid"):
            web.start_phone_run(
                {"program": self.project, "mode": "audit", "provider": "openai",
                 "max_cost": 3}, env=self.env,
                programs=[{"name": "target-app", "path": self.project}],
                readiness=[{"name": "openai", "ready": True, "detail": "ready"}],
                provider_install_pid_path=install_pid,
                pid_path=os.path.join(self.root, "mutual-audit.pid"),
                log_path=os.path.join(self.root, "mutual-audit.log"),
                lock_path=os.path.join(self.root, "mutual-audit.lock"),
                popen=lambda *args, **kwargs: self.fail("must not spawn"),
            )

    def test_provider_installer_is_terminated_when_pid_cannot_be_recorded(self):
        terminated = []

        class Process:
            pid = 848484

            @staticmethod
            def terminate():
                terminated.append("terminate")

            @staticmethod
            def wait(timeout=None):
                terminated.append(("wait", timeout))
                return 1

        blocked_pid_path = os.path.join(self.root, "pid-is-a-directory")
        os.mkdir(blocked_pid_path)
        with self.assertRaises(OSError):
            web.start_phone_provider_install(
                {"provider": "openai"}, env=self.env,
                audit_pid_path=os.path.join(self.root, "no-audit.pid"),
                pid_path=blocked_pid_path,
                log_path=os.path.join(self.root, "failed-provider.log"),
                popen=lambda *args, **kwargs: Process(),
            )
        self.assertEqual(["terminate", ("wait", 5)], terminated)

    def test_provider_installer_failure_is_sanitized_for_the_dashboard(self):
        status_path = os.path.join(self.root, "install-status.json")
        pid_path = os.path.join(self.root, "install-status.pid")
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write("858585\n")
        finished = threading.Event()

        class Process:
            pid = 858585

            @staticmethod
            def wait():
                finished.set()
                return 1

        web._start_provider_install_reaper(
            Process(), pid_path, status_path, "openai")
        self.assertTrue(finished.wait(2))
        for _ in range(50):
            if os.path.exists(status_path) and not os.path.exists(pid_path):
                break
            time.sleep(0.01)
        status = web._load_provider_install_status(status_path)
        self.assertEqual("failed", status["state"])
        self.assertIn("provider-log", status["detail"])

    def test_provider_support_installer_rejects_non_cloud_provider(self):
        with self.assertRaisesRegex(ValueError, "openai or anthropic"):
            web.start_phone_provider_install(
                {"provider": "ollama"}, env=self.env,
                pid_path=os.path.join(self.root, "provider.pid"),
                log_path=os.path.join(self.root, "provider.log"),
                popen=lambda *args, **kwargs: self.fail("must not spawn"),
            )

    def test_launch_uses_exact_argv_and_never_pushes_or_merges(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return SimpleNamespace(pid=424242)

        pid_path = os.path.join(self.root, "run", "audit.pid")
        log_path = os.path.join(self.root, "run", "audit.log")
        lock_path = os.path.join(self.root, "run", "audit.lock")
        result = web.start_phone_run(
            {"program": self.project, "mode": "prodready", "provider": "openai",
             "max_cost": 7},
            env=self.env,
            programs=[{"name": "target-app", "path": self.project}],
            readiness=[{"name": "openai", "ready": True, "detail": "ready"}],
            pid_path=pid_path,
            log_path=log_path,
            lock_path=lock_path,
            popen=fake_popen,
            start_reaper=lambda process, path: captured.update(reaped=(process.pid, path)),
        )
        command = captured["command"]
        self.assertEqual(424242, result["pid"])
        self.assertIsInstance(command, list)
        self.assertTrue(os.path.samefile(
            self.project, command[command.index("--program") + 1]))
        self.assertIn("--no-push", command)
        self.assertIn("--no-merge", command)
        self.assertIn("--no-auto-clean", command)
        self.assertIn("--single", command)
        self.assertEqual("paid", command[command.index("--model-mode") + 1])
        self.assertNotIn("shell", captured["kwargs"])
        self.assertEqual((424242, pid_path), captured["reaped"])
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
                    lock_path=os.path.join(self.root, "run", "audit.lock"),
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
                lock_path=os.path.join(self.root, "audit.lock"),
                popen=lambda *args, **kwargs: self.fail("must not spawn"),
            )

    def test_process_start_lock_is_shared_across_processes(self):
        lock_path = os.path.join(self.root, "audit.lock")
        os.mkdir(lock_path)
        with open(os.path.join(lock_path, "owner.pid"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        with self.assertRaisesRegex(ValueError, "already starting"):
            web.start_phone_run(
                {"program": self.project, "mode": "audit", "provider": "openai",
                 "max_cost": 10},
                env=self.env,
                programs=[{"name": "target-app", "path": self.project}],
                readiness=[{"name": "openai", "ready": True, "detail": "ready"}],
                pid_path=os.path.join(self.root, "audit.pid"),
                log_path=os.path.join(self.root, "audit.log"),
                lock_path=lock_path,
                popen=lambda *args, **kwargs: self.fail("must not spawn"),
            )

    def test_ollama_launch_uses_free_model_mode(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return SimpleNamespace(pid=989898)

        run_dir = os.path.join(self.root, "ollama-run")
        web.start_phone_run(
            {"program": self.project, "mode": "audit", "provider": "ollama",
             "max_cost": 2},
            env=self.env,
            programs=[{"name": "target-app", "path": self.project}],
            readiness=[{"name": "ollama", "ready": True, "detail": "ready"}],
            pid_path=os.path.join(run_dir, "audit.pid"),
            log_path=os.path.join(run_dir, "audit.log"),
            lock_path=os.path.join(run_dir, "audit.lock"),
            popen=fake_popen,
            start_reaper=lambda process, path: None,
        )
        command = captured["command"]
        self.assertEqual("free", command[command.index("--model-mode") + 1])

    def test_reaper_clears_only_the_exited_child_pid(self):
        pid_path = os.path.join(self.root, "audit.pid")
        with open(pid_path, "w", encoding="utf-8") as fh:
            fh.write("31337\n")
        waited = threading.Event()

        class Process:
            pid = 31337

            @staticmethod
            def wait():
                waited.set()
                return 0

        web._start_audit_reaper(Process(), pid_path)
        self.assertTrue(waited.wait(2))
        for _ in range(20):
            if not os.path.exists(pid_path):
                break
            time.sleep(0.01)
        self.assertFalse(os.path.exists(pid_path))

    def test_shell_launcher_uses_the_same_atomic_lock(self):
        path = os.path.join(os.path.dirname(__file__), "scripts", "phone", "engine.sh")
        with open(path, encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('AUDIT_LOCK="$RUN_DIR/audit.lock"', script)
        self.assertIn('mkdir "$AUDIT_LOCK"', script)
        self.assertIn("acquire_audit_lock || exit 1", script)

    def test_http_launch_requires_exact_token(self):
        original = web.start_phone_run
        web.start_phone_run = lambda body, provider_path=None: {
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

    def test_http_provider_setup_requires_token_and_never_echoes_key(self):
        secret = "http-provider-secret-that-must-not-leak"
        original = web.save_phone_provider
        web.save_phone_provider = lambda body, provider_path=None: {
            "ok": True, "provider": body["provider"], "configured": True,
            "ready": True, "detail": "ready",
        }
        web.Handler.token = "provider-setup-token"
        web.Handler.sampler = object()
        web.Handler.provider_path = self.provider_path
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:{}/api/provider".format(server.server_port)

            def post(token):
                request = urllib.request.Request(
                    url,
                    data=json.dumps({"provider": "openai", "api_key": secret}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + token},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, response.read().decode()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read().decode()

            self.assertEqual(401, post("wrong")[0])
            status, response = post("provider-setup-token")
            self.assertEqual(201, status)
            self.assertNotIn(secret, response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            web.save_phone_provider = original

    def test_page_exposes_launcher_and_local_only_policy(self):
        self.assertIn("Start on this phone", web.PAGE)
        self.assertIn("cannot push or merge", web.PAGE)
        self.assertIn("/api/launch", web.PAGE)
        self.assertIn("Save key on this phone", web.PAGE)
        self.assertIn('type="password"', web.PAGE)
        self.assertIn("/api/provider", web.PAGE)
        self.assertIn("Install provider support", web.PAGE)
        self.assertIn("/api/provider/install", web.PAGE)
        self.assertNotIn("if(providerKey&&providerKey.value) return", web.PAGE)

    def test_android_launcher_is_native_and_standalone(self):
        path = os.path.join(
            os.path.dirname(__file__), "android", "app", "src", "main", "java",
            "com", "firer", "console", "flexfactor", "MainActivity.java",
        )
        with open(path, encoding="utf-8") as fh:
            activity = fh.read()
        self.assertNotIn("android.webkit", activity)
        self.assertNotIn("WebView", activity)
        self.assertNotIn("com.termux", activity)
        self.assertNotIn("RUN_COMMAND", activity)
        self.assertIn('button("Credentials")', activity)
        self.assertIn('addMode("1 · Refactor a file"', activity)
        self.assertIn('addMode("2 · Scout improvements"', activity)
        self.assertIn('addMode("3 · Audit and repair"', activity)
        self.assertIn('addMode("4 · Make production ready"', activity)

    def test_android_main_release_creates_the_exact_version_tag(self):
        path = os.path.join(
            os.path.dirname(__file__), ".github", "workflows",
            "android-client.yml",
        )
        with open(path, encoding="utf-8") as fh:
            workflow = fh.read()
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn('previous_version=$(git show', workflow)
        self.assertIn('if [ "$version_name" != "$previous_version" ]', workflow)
        self.assertIn("needs.release-plan.outputs.should_publish == 'true'", workflow)
        self.assertIn('create_ref_args=(--target "$GITHUB_SHA")', workflow)
        self.assertIn('create_ref_args=(--verify-tag)', workflow)
        self.assertIn('tag_commit=$(resolve_tag_commit)', workflow)
        self.assertIn('if [ "$tag_commit" != "$GITHUB_SHA" ]', workflow)
        self.assertIn('gh release create "$RELEASE_TAG"', workflow)


if __name__ == "__main__":
    unittest.main()
