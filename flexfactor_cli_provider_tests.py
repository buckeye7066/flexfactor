#!/usr/bin/env python3
"""CLI-backed rotation providers (`claude-code`, `codex-cli`, `copilot-cli`).

THE DEFECT THESE EXIST TO PREVENT
---------------------------------
A proposed change added `codex-cli` / `claude-code` branches whose factory did
`from providers.cli_provider import make_cli_provider` while that module DID
NOT EXIST, and guarded the routes with nothing but `shutil.which()`. On this
machine both binaries ARE on PATH, so the filter would have ADMITTED the routes
and the factory would have raised ModuleNotFoundError the moment one was
selected — precisely the "unbuildable route reaches the Rotator, fails at call
time, burns a cooldown" failure the filter exists to stop.

So the load-bearing property is not "the provider works". It is: an adapter
that cannot be imported must be EXCLUDED WITH A REASON, and must never take
the rest of the catalog down with it.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flexfactor as ff
from providers import cli_provider as cp
from providers import chatgpt_subscription as cs


class Route:
    def __init__(self, api, model="m", is_free=True, base_url="", backend=None,
                 cost_class=None):
        self.api = api
        self.model = model
        self.wire_model = model
        self.is_free = is_free
        self.auth_env = None
        self.base_url = base_url
        self.id = model
        # A real catalog route ALWAYS carries these two and the mode filter
        # reads them. The stub used to omit them, which quietly made every test
        # here depend on a permissive mode that no longer exists (owner order
        # 2026-08-24: the only modes are free and paid). Default them off
        # `is_free` so a stub route is self-consistent instead of 'unset'.
        self.backend = backend or ("groq" if is_free else "openai_api")
        self.cost_class = cost_class or ("free-tier" if is_free else "paid-metered")


def _ext_on():
    os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "1"


class FilterAdmitsOnlyBuildableRoutesTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("FLEXFACTOR_ROTATION_EXTENSIONS")
        _ext_on()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FLEXFACTOR_ROTATION_EXTENSIONS", None)
        else:
            os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = self._prev

    def test_a_missing_adapter_is_a_REASON_not_an_exception(self):
        """The whole point. A PATH hit must not admit an unimportable route."""
        real = cp.cli_binary_for
        try:
            # Simulate the module being absent by making the helper raise the
            # same way an absent import does.
            def boom(_api):
                raise ModuleNotFoundError("No module named 'providers.cli_provider'")
            cp.cli_binary_for = boom
            reason = ff._extended_route_unusable(Route("claude-code"))
        finally:
            cp.cli_binary_for = real
        self.assertIn("adapter unavailable", reason)
        self.assertIn("ModuleNotFoundError", reason)

    def test_one_broken_adapter_never_breaks_the_REST_of_the_catalog(self):
        """A filter that raises takes rotation down entirely."""
        real = cp.cli_binary_for
        try:
            cp.cli_binary_for = lambda _api: (_ for _ in ()).throw(RuntimeError("boom"))
            self.assertNotEqual(ff._route_unusable_reason(Route("claude-code"), "free"), "")
            # An ordinary route is still evaluated normally.
            self.assertEqual(ff._route_unusable_reason(Route("openai"), "free"), "")
        finally:
            cp.cli_binary_for = real

    def test_an_unknown_api_is_still_rejected(self):
        self.assertIn("unsupported api",
                      ff._route_unusable_reason(Route("bogus-api"), "free"))

    def test_extended_apis_are_accepted_by_the_api_allowlist(self):
        # Regression: the allowlist must actually name them, or they are
        # rejected before the buildability check is ever consulted.
        for api in ("cursor", "codex-cli", "claude-code"):
            self.assertNotIn("unsupported api",
                             ff._route_unusable_reason(Route(api), "free"))

    def test_the_claude_code_subscription_lane_is_reachable_from_the_one_policy(self):
        """Legacy mode spellings cannot exclude subscription capacity.

        The `claude` CLI is a paid Anthropic subscription and therefore belongs
        near the front of the sole best-available ladder. ``paid`` and ``free``
        remain accepted only as saved-command aliases for that same policy.
        """
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "claude.bat" if os.name == "nt" else "claude")
            with open(binary, "w", encoding="utf-8") as handle:
                handle.write("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n")
            if os.name != "nt":
                os.chmod(binary, 0o755)
            prior_path = os.environ.get("PATH", "")
            os.environ["PATH"] = tmp + os.pathsep + prior_path
            try:
                self.assertIsNotNone(shutil.which("claude"),
                                     "controlled claude executable was not resolved")
                route = Route("claude-code", is_free=False, backend="claude-code",
                              cost_class="subscription")
                self.assertEqual(ff._route_unusable_reason(route, "paid"), "")
                self.assertEqual(ff._route_unusable_reason(route, "free"), "")
            finally:
                os.environ["PATH"] = prior_path

    def test_extensions_off_disables_the_cli_routes(self):
        os.environ["FLEXFACTOR_ROTATION_EXTENSIONS"] = "0"
        self.assertIn("extended providers off",
                      ff._route_unusable_reason(Route("claude-code"), "free"))


class CliProviderBehaviourTests(unittest.TestCase):
    def setUp(self):
        _ext_on()

    def test_the_prompt_travels_on_STDIN_never_argv(self):
        """WinPS 5.1 mangles quotes in native args; a review prompt is full of
        braces and quotes, so argv would corrupt it."""
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            p = cp.CliProvider("claude-code", "m", "claude")
            p.complete("SECRET_PROMPT_TEXT {\"a\": 1}")
        finally:
            cp._run_process_tree = real
        self.assertIn("SECRET_PROMPT_TEXT", seen["input"])
        self.assertNotIn("SECRET_PROMPT_TEXT", " ".join(seen["argv"]))

    def test_a_nonzero_exit_raises_rather_than_returning_empty(self):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="boom")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            with self.assertRaises(cp.CliUnavailable):
                cp.CliProvider("claude-code", "m", "claude").complete("x")
        finally:
            cp._run_process_tree = real

    def test_an_EMPTY_answer_is_a_failure_not_a_clean_review(self):
        """An empty result recorded as success is the silent-no-op class."""
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout="   ", stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            with self.assertRaises(cp.CliUnavailable):
                cp.CliProvider("claude-code", "m", "claude").complete("x")
        finally:
            cp._run_process_tree = real

    def test_a_timeout_is_bounded_and_reported(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 1))
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            with self.assertRaises(cp.CliUnavailable):
                cp.CliProvider("claude-code", "m", "claude", timeout=1).complete("x")
        finally:
            cp._run_process_tree = real

    def test_every_call_passes_a_timeout(self):
        """An unbounded wait is what froze a live run for 25+ minutes."""
        seen = {}

        def fake_run(argv, **kw):
            seen["timeout"] = kw.get("timeout")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            cp.CliProvider("claude-code", "m", "claude").complete("x")
        finally:
            cp._run_process_tree = real
        self.assertIsNotNone(seen["timeout"])
        self.assertGreater(seen["timeout"], 0)

    def test_it_REFUSES_to_recurse(self):
        """`claude` called from inside a CLI-provider call would fan out one
        nested agent per rotation step."""
        os.environ[cp._RECURSION_MARKER] = "1"
        try:
            with self.assertRaises(cp.CliUnavailable):
                cp.CliProvider("claude-code", "m", "claude").complete("x")
        finally:
            os.environ.pop(cp._RECURSION_MARKER, None)

    def test_structured_extracts_json_from_prose_and_fences(self):
        for payload in ('{"ok": 1}',
                        'Sure!\n```json\n{"ok": 1}\n```\n',
                        'Here you go: {"ok": 1} — done'):
            def fake_run(argv, **kw):
                return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
            real = cp._run_process_tree
            cp._run_process_tree = fake_run
            try:
                out = cp.CliProvider("claude-code", "m", "claude").structured("x", {})
            finally:
                cp._run_process_tree = real
            self.assertEqual(out, {"ok": 1})

    def test_unparseable_json_raises_instead_of_returning_junk(self):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout="no json here", stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            with self.assertRaises(cp.CliUnavailable):
                cp.CliProvider("claude-code", "m", "claude").structured("x", {})
        finally:
            cp._run_process_tree = real

    def test_structured_accepts_the_core_provider_call_shape(self):
        """Fix generation passes system, prompt, schema as three positionals."""
        seen = {}

        def fake_run(argv, **kw):
            seen["input"] = kw.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            out = cp.CliProvider("codex-cli", "m", "codex").structured(
                "SYSTEM RULE", "make the fix", {"type": "object"},
                max_tokens=123, model="ignored", salvage_truncated=True)
        finally:
            cp._run_process_tree = real
        self.assertEqual(out, {"ok": True})
        self.assertIn("SYSTEM RULE", seen["input"])
        self.assertIn("make the fix", seen["input"])

    def test_grade_requests_typed_json_instead_of_free_form_prose(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["input"] = kw.get("input")
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=(
                    '{"grade": 96, "meets_goal": true, '
                    '"rationale": "complete", "issues": []}'
                ),
                stderr="",
            )

        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            result = cp.CliProvider("codex-cli", "gpt-5.6", "codex").grade(
                "grade this candidate"
            )
        finally:
            cp._run_process_tree = real
        self.assertEqual(96, result["grade"])
        self.assertIs(result["meets_goal"], True)
        self.assertIn('"meets_goal": {"type": "boolean"}', seen["input"])
        self.assertIn("grade this candidate", seen["input"])

    def test_the_work_theme_reaches_the_cli(self):
        """Owner requirement: rotated calls stay ON TASK. The theme must be
        carried, or a rotated provider wanders off the run's purpose."""
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
        real = cp._run_process_tree

        cp._run_process_tree = fake_run
        try:
            cp.CliProvider("claude-code", "m", "claude").complete("p", system="THEME_MARKER")
        finally:
            cp._run_process_tree = real
        self.assertIn("THEME_MARKER", " ".join(seen["argv"]) + (seen["input"] or ""))

        cp._run_process_tree = fake_run
        try:
            cp.CliProvider("codex-cli", "m", "codex").complete("p", system="THEME_MARKER")
        finally:
            cp._run_process_tree = real
        # codex exec takes no --append-system-prompt, so it must ride the prompt.
        self.assertIn("THEME_MARKER", seen["input"])

    def test_copilot_is_silent_noninteractive_and_receives_prompt_on_stdin(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            cp.CliProvider(
                "copilot-cli", "claude-sonnet-4.6", "copilot"
            ).complete(
                "PROMPT", system="SYSTEM")
        finally:
            cp._run_process_tree = real
        self.assertIn("-s", seen["argv"])
        self.assertIn("--no-ask-user", seen["argv"])
        self.assertNotIn("--allow-all-tools", seen["argv"])
        self.assertIn("--model", seen["argv"])
        self.assertIn("claude-sonnet-4.6", seen["argv"])
        self.assertIn("SYSTEM", seen["input"])
        self.assertIn("PROMPT", seen["input"])

    def test_copilot_ping_proves_inference_not_just_binary_version(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input")
            return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            self.assertTrue(cp.CliProvider(
                "copilot-cli", "auto", "copilot").ping())
        finally:
            cp._run_process_tree = real
        self.assertNotIn("--version", seen["argv"])
        self.assertIn("Reply with OK", seen["input"])

    def test_codex_ping_proves_inference_and_runs_ephemeral_read_only(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input")
            seen["timeout"] = kw.get("timeout")
            return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
        real = cp._run_process_tree
        cp._run_process_tree = fake_run
        try:
            self.assertTrue(cp.CliProvider(
                "codex-cli", "codex", "codex", timeout=600).ping())
        finally:
            cp._run_process_tree = real
        self.assertNotIn("--version", seen["argv"])
        self.assertIn("--ephemeral", seen["argv"])
        self.assertIn("read-only", seen["argv"])
        self.assertIn("Reply with OK", seen["input"])
        self.assertLessEqual(seen["timeout"], 60)

    def test_billing_label_marks_these_flat_rate(self):
        self.assertIn("subscription", cp.CliProvider("codex-cli", "m", "codex").cost_label)


class _FakeHttpResponse:
    def __init__(self, body=b"", *, content_type="application/json", lines=None):
        self._body = body
        self._lines = list(lines or [])
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


class ChatGPTSubscriptionTransportTests(unittest.TestCase):
    def test_only_exportable_account_bound_oauth_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "real-token", "account_id": "acct-1"},
            }), encoding="utf-8")
            auth = cs.load_exportable_oauth(path)
            self.assertIsNotNone(auth)
            self.assertEqual(auth.account_id, "acct-1")

            path.write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "managed-placeholder",
                           "account_id": None},
            }), encoding="utf-8")
            self.assertIsNone(cs.load_exportable_oauth(path))

    def test_codex_prefers_subscription_http_without_spawning_nested_cli(self):
        class FakeClient:
            model = "gpt-5.6-sol"

            def complete(self, prompt, **kwargs):
                self.call = (prompt, kwargs)
                return "OK"

        fake = FakeClient()
        oauth = cs.CodexOAuth("secret", "acct", "test")
        with mock.patch.object(cs, "load_exportable_oauth", return_value=oauth), \
             mock.patch.object(cs, "ChatGPTSubscriptionClient", return_value=fake), \
             mock.patch.object(cp, "_run_process_tree") as run:
            provider = cp.CliProvider("codex-cli", "codex", "/bin/codex")
            self.assertEqual(provider.complete("PROMPT", system="SYSTEM"), "OK")
        run.assert_not_called()
        self.assertEqual(provider.model, "gpt-5.6-sol")
        self.assertEqual(fake.call[0], "PROMPT")
        self.assertEqual(fake.call[1]["system"], "SYSTEM")

    def test_managed_work_mode_without_exportable_oauth_fails_before_spawn(self):
        with mock.patch.object(cs, "load_exportable_oauth", return_value=None), \
             mock.patch.object(cp, "_inside_managed_codex_session", return_value=True), \
             mock.patch.object(cp, "_run_process_tree") as run:
            provider = cp.CliProvider(
                "codex-cli", "codex",
                # Absolute on BOTH platforms: ntpath.isabs() is False for a
                # driveless "/opt/..." path, so on Windows the guard under
                # test was skipped and this failed for an unrelated reason.
                os.path.abspath(
                    os.path.join(os.sep, "opt", "codex", "bin", "codex")))
            with self.assertRaisesRegex(cp.CliUnavailable, "brokers.*credential"):
                provider.ping()
        run.assert_not_called()

    def test_live_catalog_default_selects_sol_and_streams_text(self):
        requests = []
        model_body = json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "is_default": True},
            {"slug": "gpt-5.6-terra"},
        ]}).encode()
        stream = [
            b'data: {"type":"response.output_text.delta","delta":"O"}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"K"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{}}\n',
            b'\n',
        ]

        def fake_open(request, timeout):
            requests.append((request, timeout))
            if request.get_method() == "GET":
                return _FakeHttpResponse(model_body)
            return _FakeHttpResponse(
                content_type="text/event-stream", lines=stream)

        with mock.patch.object(cs, "_codex_version", return_value="0.149.0"):
            client = cs.ChatGPTSubscriptionClient(
                cs.CodexOAuth("TOP-SECRET", "acct-1", "test"),
                model="codex", binary="codex", timeout=30, urlopen=fake_open)
        self.assertEqual(client.complete("PROMPT", system="SYSTEM", max_tokens=17), "OK")
        self.assertEqual(client.model, "gpt-5.6-sol")
        self.assertEqual(len(requests), 2)
        post = requests[1][0]
        payload = json.loads(post.data.decode())
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["instructions"], "SYSTEM")
        self.assertEqual(payload["max_output_tokens"], 17)
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["tools"], [])
        self.assertNotIn("TOP-SECRET", post.full_url)
        self.assertNotIn("TOP-SECRET", post.data.decode())

    def test_sse_heartbeats_cannot_extend_the_wall_clock_deadline(self):
        now = [0.0]

        def clock():
            now[0] += 0.4
            return now[0]

        heartbeats = [b": keep-alive\n"] * 20
        with self.assertRaisesRegex(cs.SubscriptionUnavailable, "exceeded 1s"):
            list(cs._sse_events(
                heartbeats, deadline=1.0, timeout=1.0, clock=clock))

    def test_expired_oauth_falls_back_to_official_codex_cli_refresh_path(self):
        def rejected(_request, timeout):
            raise urllib.error.HTTPError(
                cs.RESPONSES_URL, 401, "unauthorized", {},
                io.BytesIO(b'{"error":{"message":"access token expired"}}'))

        oauth = cs.CodexOAuth("OLD-SECRET", "acct-1", "test")
        with mock.patch.object(cs, "load_exportable_oauth", return_value=oauth), \
             mock.patch.object(cs, "_codex_version", return_value="0.149.0"), \
             mock.patch.object(cs.urllib.request, "urlopen", side_effect=rejected), \
             mock.patch.object(cp, "_inside_managed_codex_session", return_value=False), \
             mock.patch.object(cp, "_run_cli", return_value="CLI-REFRESHED") as run_cli:
            provider = cp.CliProvider(
                "codex-cli", "gpt-5.6-sol", "/bin/codex", timeout=30)
            self.assertEqual(provider.complete("PROMPT", system="SYSTEM"),
                             "CLI-REFRESHED")
        run_cli.assert_called_once()
        self.assertIsNone(provider._subscription)

    def test_http_failure_never_echoes_bearer_and_becomes_cli_unavailable(self):
        def rejected(_request, timeout):
            raise urllib.error.HTTPError(
                cs.RESPONSES_URL, 429, "limit", {"Retry-After": "12"},
                io.BytesIO(b'{"error":{"message":"quota exhausted"}}'))

        oauth = cs.CodexOAuth("TOP-SECRET", "acct-1", "test")
        with mock.patch.object(cs, "load_exportable_oauth", return_value=oauth), \
             mock.patch.object(cs, "_codex_version", return_value="0.149.0"), \
             mock.patch.object(cs.urllib.request, "urlopen", side_effect=rejected):
            provider = cp.CliProvider(
                "codex-cli", "gpt-5.6-sol", "/bin/codex", timeout=30)
            with self.assertRaises(cp.CliUnavailable) as raised:
                provider.complete("PROMPT")
        message = str(raised.exception)
        self.assertIn("429", message)
        self.assertIn("quota exhausted", message)
        self.assertIn("retry after 12s", message)
        self.assertNotIn("TOP-SECRET", message)



class CliTimeoutIsActuallyBoundedTests(unittest.TestCase):
    """The advertised per-call timeout must be the real ceiling.

    THE DEFECT THESE EXIST TO PREVENT
    ---------------------------------
    `_run_cli` used `subprocess.run(..., timeout=N)`. On expiry `run` kills
    only the DIRECT child and then, on Windows, calls `communicate()` a SECOND
    time with NO timeout - which joins the daemon reader threads, which block
    until EOF, which never arrives while a descendant still holds the inherited
    write handle. Measured 2026-09-05 on Windows 11 / CPython 3.14.6: a
    `timeout=3` call against a child whose worker lived 20s raised after 24.0s
    and 25.7s - an 8x overrun of a budget the rotation ladder trusts.

    IMPORTANT - why neither test below asserts on elapsed time. Whether the
    worker wins the race to inherit the pipe handle before the child exits is
    NOT deterministic: in a measured 12-run sweep only 2 runs overran (24.0s,
    25.7s); the other 10 returned in ~3.3s. A wall-clock assertion would
    therefore pass against the unpatched code roughly 80% of the time - a
    tripwire that mostly does not trip is worse than none. These tests pin the
    cleanup properties that make the bound hold instead: the tree is really
    killed, the pipes are neither re-drained nor closed, and the child is
    given a group to kill.
    """

    @staticmethod
    def _alive(pid):
        """Liveness without psutil. NB: `os.kill(pid, 0)` is unusable here -
        on Windows it does not probe, it calls TerminateProcess."""
        if os.name == "nt":
            tasklist = shutil.which("tasklist")
            if tasklist is None:                               # pragma: no cover
                raise unittest.SkipTest("tasklist unavailable to probe liveness")
            proc = subprocess.run(
                [tasklist, "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=30, check=False)
            return str(pid) in (proc.stdout or "")
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return False
        # A SIGKILLed worker whose parent has already exited lingers as a
        # zombie until its new reaper collects it, and `kill(pid, 0)` still
        # succeeds for one. Reaped-or-not is not the question this asks.
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[-1].split()
            if fields and fields[0] == "Z":
                return False
        except OSError:
            pass
        return True

    def _reap(self, pid):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (OSError, ProcessLookupError):
            pass

    def test_expiry_kills_the_worker_the_cli_spawned_not_just_the_cli(self):
        """A killed CLI whose worker survives is what holds the pipes open.

        Deterministic: the child is still running at expiry, so the tree walk
        reaches its worker. This is the shape of an agent CLI that farms the
        real job out to a subprocess.
        """
        if not sys.executable:                                 # pragma: no cover
            self.skipTest("no interpreter available to spawn the probe tree")
        with tempfile.TemporaryDirectory() as tmp:
            # Real script files, not `-c`: a `-c` program carrying a Windows
            # path is mangled by list2cmdline's backslash rules and the child
            # then silently does not do what the test believes it does.
            worker_py = os.path.join(tmp, "worker.py")
            child_py = os.path.join(tmp, "child.py")
            pidfile = os.path.join(tmp, "worker.pid")
            with open(worker_py, "w", encoding="utf-8") as handle:
                handle.write("import time\ntime.sleep(180)\n")
            with open(child_py, "w", encoding="utf-8") as handle:
                handle.write(
                    "import os, subprocess, sys, time\n"
                    "here = os.path.dirname(os.path.abspath(__file__))\n"
                    "p = subprocess.Popen(\n"
                    "    [sys.executable, os.path.join(here, 'worker.py')])\n"
                    "with open(os.path.join(here, 'worker.pid'), 'w') as fh:\n"
                    "    fh.write(str(p.pid))\n"
                    "time.sleep(180)\n")
            with self.assertRaises(subprocess.TimeoutExpired):
                cp._run_process_tree(
                    [sys.executable, child_py], input="",
                    capture_output=True, text=True, timeout=4.0, shell=False)
            with open(pidfile, encoding="utf-8") as handle:
                worker = int(handle.read().strip())
        try:
            # taskkill/killpg are asynchronous; poll rather than assert instantly.
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and self._alive(worker):
                time.sleep(0.2)
            self.assertFalse(
                self._alive(worker),
                "the worker the CLI spawned survived the timeout; it keeps the "
                "captured pipes open and the advertised timeout stops binding")
        finally:
            self._reap(worker)

    def test_expiry_never_redrains_or_closes_the_pipes(self):
        """The other half, and the one a `taskkill` grep would miss.

        A cleanup path that kills the tree and THEN calls `communicate()` again
        or closes the streams reintroduces the unbounded wait: the second
        `communicate()` re-joins the same blocked reader threads, and
        `close()` blocks on the BufferedReader lock the still-reading daemon
        thread holds. Both were measured taking the full descendant lifetime.
        """
        closed = []

        class Stream:
            def __init__(self, name):
                self.name = name

            def close(self):
                closed.append(self.name)

        outer = self

        class FakePopen:
            def __init__(self, argv, **kwargs):
                self.pid = 4242
                self.args = argv
                self.kwargs = kwargs
                self.communicate_calls = 0
                self.killed = False
                self.waited = False
                self.stdin = Stream("stdin")
                self.stdout = Stream("stdout")
                self.stderr = Stream("stderr")
                outer.spawned = self

            def communicate(self, input=None, timeout=None):
                self.communicate_calls += 1
                raise subprocess.TimeoutExpired(self.args, timeout or 0)

            def poll(self):
                return None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.waited = True
                return -9

            # Popen is a context manager and subprocess.run uses it as one.
            # Without these, a regression would fail on a TypeError instead of
            # on the property this test exists to pin.
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                for stream in (self.stdout, self.stderr, self.stdin):
                    if stream is not None:
                        stream.close()
                self.wait()

        guards = [mock.patch.object(subprocess, "Popen", FakePopen),
                  # No real taskkill for a fabricated pid...
                  mock.patch.object(cp.shutil, "which", return_value=None)]
        if hasattr(os, "killpg"):
            # ...and emphatically no real killpg either: pid 4242 may well be a
            # live process group on whatever machine runs this suite.
            guards.append(mock.patch.object(cp.os, "killpg"))
        with contextlib.ExitStack() as stack:
            for guard in guards:
                stack.enter_context(guard)
            with self.assertRaises(subprocess.TimeoutExpired):
                cp._run_process_tree(
                    ["cli"], input="body", capture_output=True, text=True,
                    timeout=1.0, shell=False)

        spawned = self.spawned
        self.assertEqual(
            spawned.communicate_calls, 1,
            "communicate() was called again after expiry; that re-joins the "
            "blocked reader threads and restores the unbounded wait")
        self.assertEqual(
            closed, [],
            "the pipes were closed from the calling thread after expiry; "
            "close() blocks on the lock the reading daemon thread holds")
        self.assertTrue(spawned.killed, "the child was not killed on expiry")
        self.assertTrue(spawned.waited, "the killed child was never reaped")

    def test_the_process_group_is_isolated_so_the_tree_can_be_killed(self):
        """Without its own group/session there is no tree to terminate."""
        class FakePopen:
            def __init__(self, argv, **kwargs):
                self.pid = 1
                self.args = argv
                self.kwargs = kwargs
                self.stdin = self.stdout = self.stderr = None

            def communicate(self, input=None, timeout=None):
                return ("out", "err")

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            @property
            def returncode(self):
                return 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.wait()

        captured = {}

        class Recording(FakePopen):
            def __init__(self, argv, **kwargs):
                super().__init__(argv, **kwargs)
                captured.update(kwargs)

        with mock.patch.object(subprocess, "Popen", Recording):
            done = cp._run_process_tree(["cli"], input="b", capture_output=True,
                                        text=True, timeout=1.0)
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (0, "out", "err"))
        if os.name == "nt":
            self.assertTrue(
                captured.get("creationflags", 0)
                & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                "the CLI was not given its own process group")
        else:
            self.assertTrue(captured.get("start_new_session"),
                            "the CLI was not given its own session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
