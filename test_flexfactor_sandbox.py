"""Tests for flexfactor_sandbox - the cross-platform execution broker.

Run: python test_flexfactor_sandbox.py

A mechanism that is genuinely unavailable on the host SKIPS with a reason
containing "BLOCKED" - it never silently passes. Every resource/abuse test
drives a REAL child process.
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import time
import unittest

import flexfactor_sandbox as sb
from flexfactor_sandbox import Limits, ContainmentUnavailable

PY = sys.executable
REP = sb.capability_report()
IS_WIN = sb.IS_WINDOWS
TMP = tempfile.gettempdir()


def _blocked(what: str) -> str:
    return f"BLOCKED: {what} on this host (strongest={REP['strongest']}, platform={REP['platform']})"


class CapabilityReportTests(unittest.TestCase):
    def test_structure(self):
        for key in ("platform", "mechanisms", "strongest", "network_isolation",
                    "process_tree", "memory", "claim"):
            self.assertIn(key, REP)
        self.assertIsInstance(REP["mechanisms"], list)
        for m in REP["mechanisms"]:
            for k in ("name", "available", "enforces", "detail"):
                self.assertIn(k, m)
            self.assertIsInstance(m["available"], bool)
        self.assertIn(REP["network_isolation"], ("os-enforced", "best-effort-env", "none"))
        self.assertIn(REP["process_tree"], ("os-enforced", "best-effort", "none"))
        self.assertIn(REP["memory"], ("os-enforced", "best-effort", "none"))
        if REP["strongest"] is not None:
            self.assertIn(REP["strongest"], [m["name"] for m in REP["mechanisms"]])

    def test_claim_never_says_contained_unless_os_enforced_net_and_tree(self):
        both = (REP["network_isolation"] == "os-enforced" and REP["process_tree"] == "os-enforced")
        said = "contained" in REP["claim"].lower()
        if not both:
            self.assertFalse(said, REP["claim"])
            self.assertIn("NOT an OS sandbox", REP["claim"])
        else:
            self.assertTrue(said, REP["claim"])

    def test_claim_sentence_is_exercised_for_both_branches(self):
        weak = dict(REP, network_isolation="best-effort-env", process_tree="os-enforced",
                    strongest="x")
        self.assertNotIn("contained", sb._claim_sentence(weak).lower())
        strong = dict(REP, network_isolation="os-enforced", process_tree="os-enforced",
                      strongest="bwrap", memory="os-enforced")
        self.assertIn("contained", sb._claim_sentence(strong).lower())

    def test_windows_network_is_never_claimed_os_enforced(self):
        if not IS_WIN:
            self.skipTest(_blocked("Windows-only assertion"))
        self.assertEqual(REP["network_isolation"], "best-effort-env")
        self.assertEqual(REP["strongest"], "win32-job-object")


class PrepareTests(unittest.TestCase):
    def test_strips_secrets_keeps_path_home_and_poisons_network(self):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/h", "USERPROFILE": "C:/u",
               "TEMP": TMP, "TMP": TMP, "SystemRoot": "C:/Windows", "PATHEXT": ".EXE",
               "COMSPEC": "cmd.exe", "LANG": "C", "PYTHONPATH": "x", "NODE_ENV": "test",
               "npm_config_cache": "/c",
               "ANTHROPIC_API_KEY": "sk-1", "OPENAI_API_KEY": "sk-2", "AWS_SECRET_ACCESS_KEY": "a",
               "AWS_REGION": "us", "GITHUB_TOKEN": "g", "NPM_TOKEN": "n", "MY_PASSWORD": "p",
               "DB_PASSWD": "p", "X_CREDENTIALS": "c", "BASIC_AUTH": "b", "SOME_SECRET": "s",
               "API_KEY": "k", "NODE_AUTH_TOKEN": "t", "HARMLESS": "1"}
        c = sb.prepare([PY, "-c", "pass"], TMP, env, Limits(network=False))
        for gone in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
                     "GITHUB_TOKEN", "NPM_TOKEN", "MY_PASSWORD", "DB_PASSWD", "X_CREDENTIALS",
                     "BASIC_AUTH", "SOME_SECRET", "API_KEY", "NODE_AUTH_TOKEN"):
            self.assertNotIn(gone, c.env, gone)
            self.assertIn(gone, c.level["credentials_stripped"])
        for kept in ("PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "SystemRoot", "PATHEXT",
                     "COMSPEC", "LANG", "PYTHONPATH", "NODE_ENV", "npm_config_cache", "HARMLESS"):
            self.assertIn(kept, c.env, kept)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                  "all_proxy", "npm_config_registry"):
            self.assertEqual(c.env[k], "http://127.0.0.1:9", k)
        self.assertEqual(c.env["NO_PROXY"], "")
        self.assertEqual(c.env["no_proxy"], "")
        self.assertEqual(c.env["npm_config_offline"], "true")
        self.assertEqual(c.env["npm_config_fund"], "false")
        self.assertEqual(c.env["npm_config_audit"], "false")
        self.assertEqual(c.env["PIP_NO_INDEX"], "1")
        self.assertEqual(c.mechanism, REP["strongest"] or "env-only")
        self.assertTrue(callable(c.cleanup))
        c.cleanup()

    def test_network_true_does_not_poison(self):
        c = sb.prepare([PY, "-c", "pass"], TMP, {"PATH": "p"}, Limits(network=True))
        self.assertNotIn("HTTP_PROXY", c.env)
        self.assertEqual(c.level["network_isolation"], "off")
        c.cleanup()


class RunContainedTests(unittest.TestCase):
    def test_hello_rc0_with_containment_attribute(self):
        cp = sb.run_contained([PY, "-c", "print('hi')"], TMP, limits=Limits(timeout_s=60))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "hi")
        self.assertIsInstance(cp.stderr, str)
        self.assertTrue(cp.flexfactor_containment["applied"])
        self.assertEqual(cp.flexfactor_containment["mechanism"], REP["strongest"] or "env-only")
        self.assertIn("level", cp.flexfactor_containment)
        self.assertFalse(getattr(cp, "flexfactor_launch_error", False))

    def test_missing_executable_rc127_launch_error(self):
        cp = sb.run_contained(["definitely-not-a-real-exe-xyz", "--v"], TMP, limits=Limits())
        self.assertEqual(cp.returncode, 127)
        self.assertTrue(cp.flexfactor_launch_error)
        self.assertIn("not found", cp.stderr)
        self.assertEqual(cp.stdout, "")

    def test_timeout_kills_whole_tree_including_grandchild(self):
        child = textwrap.dedent("""
            import subprocess, sys, time
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print("GRANDCHILD", g.pid, flush=True)
            time.sleep(60)
        """)
        t0 = time.time()
        cp = sb.run_contained([PY, "-c", child], TMP, limits=Limits(timeout_s=3))
        self.assertEqual(cp.returncode, 124, cp.stderr)
        self.assertTrue(cp.flexfactor_launch_error)
        self.assertIn("timed out", cp.stderr)
        self.assertLess(time.time() - t0, 40)
        line = [l for l in cp.stdout.splitlines() if l.startswith("GRANDCHILD")]
        self.assertTrue(line, f"grandchild pid never reported: {cp.stdout!r} {cp.stderr!r}")
        gpid = int(line[0].split()[1])
        deadline = time.time() + 10
        while sb.pid_alive(gpid) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(sb.pid_alive(gpid), f"grandchild {gpid} survived the tree kill")

    def test_grandchild_check_can_fail(self):
        # verify-your-verification: pid_alive must report True for a live process
        import subprocess
        p = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(sb.pid_alive(p.pid))
        finally:
            p.kill()
            p.wait()
        time.sleep(0.3)
        self.assertFalse(sb.pid_alive(p.pid))


class ResourceAbuseTests(unittest.TestCase):
    """Test F."""

    def test_memory_limit_stops_a_300mb_allocation(self):
        if REP["memory"] != "os-enforced":
            self.skipTest(_blocked("no OS-enforced memory limit"))
        # Windows: per-process COMMIT limit (python baseline ~10-15MB).
        # Linux: RLIMIT_AS counts virtual address space; python+libs need more headroom.
        limit = 64 * 1024 ** 2 if IS_WIN else 192 * 1024 ** 2
        prog = "b = bytearray(300*1024*1024); b[-1] = 1; print('ALLOCATED', len(b))"
        cp = sb.run_contained([PY, "-c", prog], TMP,
                              limits=Limits(timeout_s=60, memory_bytes=limit))
        self.assertNotEqual(cp.returncode, 0, f"allocation succeeded under limit: {cp.stdout}")
        self.assertNotIn("ALLOCATED", cp.stdout)
        # control: the same allocation succeeds without the limit, so the test can fail
        ctl = sb.run_contained([PY, "-c", prog], TMP,
                               limits=Limits(timeout_s=60, memory_bytes=None))
        self.assertEqual(ctl.returncode, 0, ctl.stderr)
        self.assertIn("ALLOCATED", ctl.stdout)

    def test_process_count_limit_refuses_a_fork_bomb(self):
        if REP["process_count"] != "os-enforced":
            self.skipTest(_blocked("no OS-enforced process-count limit"))
        if not IS_WIN and hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest(_blocked("RLIMIT_NPROC is ignored for root"))
        child = textwrap.dedent("""
            import subprocess, sys
            kids, failed = [], 0
            for i in range(50):
                try:
                    kids.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL))
                except OSError:
                    failed += 1
            print("SPAWNED", len(kids), "FAILED", failed, flush=True)
            for k in kids:
                k.kill()
            for k in kids:
                k.wait()
        """)
        cp = sb.run_contained([PY, "-c", child], TMP,
                              limits=Limits(timeout_s=120, max_processes=5))
        self.assertIn("SPAWNED", cp.stdout, f"rc={cp.returncode} err={cp.stderr}")
        parts = cp.stdout.split()
        spawned, failed = int(parts[1]), int(parts[3])
        self.assertGreater(failed, 0, cp.stdout)
        self.assertLess(spawned, 50, cp.stdout)
        if IS_WIN:
            self.assertLessEqual(spawned, 4, cp.stdout)  # 5 active incl. the child itself

    def test_output_flood_is_capped_at_8mb_per_stream(self):
        prog = ("import sys\n"
                "line = b'x' * 1023 + b'\\n'\n"
                "for _ in range(50*1024): sys.stdout.buffer.write(line)\n"
                "sys.stdout.flush(); print('END', file=sys.stderr)")
        cp = sb.run_contained([PY, "-c", prog], TMP, limits=Limits(timeout_s=180))
        self.assertEqual(cp.returncode, 0, cp.stderr[-500:])
        self.assertLessEqual(len(cp.stdout), sb.OUTPUT_CAP_BYTES + 200)
        self.assertGreater(cp.flexfactor_output_truncated["stdout"], 0)
        self.assertEqual(cp.flexfactor_output_truncated["stderr"], 0)
        self.assertIn("stdout truncated", cp.stdout[-200:])
        self.assertIn("END", cp.stderr)


class RawSocketExfilTests(unittest.TestCase):
    """Test E."""

    def test_raw_socket_cannot_reach_the_internet(self):
        if REP["network_isolation"] != "os-enforced":
            self.skipTest(f"BLOCKED: no OS network isolation on this host ({REP['strongest']}; "
                          f"network_isolation={REP['network_isolation']})")
        prog = ("import socket\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 80), timeout=3); print('CONNECTED')\n"
                "except OSError as e:\n"
                "    print('BLOCKED', e)")
        cp = sb.run_contained([PY, "-c", prog], TMP, limits=Limits(timeout_s=60, network=False))
        self.assertNotIn("CONNECTED", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("BLOCKED", cp.stdout, cp.stdout + cp.stderr)


class SpawnContainedTests(unittest.TestCase):
    def test_spawn_and_kill_tree(self):
        child = textwrap.dedent("""
            import subprocess, sys, time, os
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open(sys.argv[1], "w").write(str(g.pid))
            time.sleep(60)
        """)
        pidfile = os.path.join(TMP, f"ff_sandbox_spawn_{os.getpid()}.txt")
        proc, err, kill_tree = sb.spawn_contained([PY, "-c", child, pidfile], TMP,
                                                  limits=Limits(timeout_s=60))
        self.assertIsNotNone(proc, err)
        self.assertEqual(err, "")
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not (os.path.exists(pidfile)
                                                  and os.path.getsize(pidfile) > 0):
                time.sleep(0.1)
            with open(pidfile, encoding="utf-8") as fh:
                gpid = int(fh.read())
            self.assertTrue(sb.pid_alive(gpid))
        finally:
            kill_tree()
            try:
                os.remove(pidfile)
            except OSError:
                pass
        deadline = time.time() + 10
        while sb.pid_alive(gpid) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(sb.pid_alive(gpid), "grandchild survived kill_tree()")
        self.assertIsNotNone(proc.poll())

    def test_spawn_missing_exe_returns_error_not_raise(self):
        proc, err, kill = sb.spawn_contained(["no-such-exe-qq"], TMP, limits=Limits())
        self.assertIsNone(proc)
        self.assertIn("no-such-exe-qq", err)
        kill()  # must be a harmless no-op


class TrustGateTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import flexfactor_trust
        self.trust = flexfactor_trust

    def test_untrusted_without_os_sandbox_raises(self):
        if sb.os_sandbox_sufficient(REP):
            self.skipTest(_blocked("host HAS a sufficient OS sandbox; refusal path unreachable"))
        d = self.trust.TrustDecision(allowed=False, reason="not in trusted_repos")
        with self.assertRaises(ContainmentUnavailable) as cm:
            sb.require_containment_or_trust(TMP, trust_decision=d)
        msg = str(cm.exception)
        self.assertIn("FLEXFACTOR_TRUSTED_REPOS", msg)
        self.assertIn("trusted_repos", msg)
        self.assertIn("network_isolation", msg)

    def test_trusted_is_allowed_on_trusted_repo_basis(self):
        if sb.os_sandbox_sufficient(REP):
            self.skipTest(_blocked("host HAS a sufficient OS sandbox; trust basis not chosen"))
        d = self.trust.TrustDecision(allowed=True, reason="under rule X")
        out = sb.require_containment_or_trust(TMP, trust_decision=d)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["basis"], "trusted-repo")
        self.assertIn("under rule X", out["claim"])
        self.assertEqual(out["report"]["strongest"], REP["strongest"])

    def test_real_trust_decision_via_env(self):
        if sb.os_sandbox_sufficient(REP):
            self.skipTest(_blocked("host HAS a sufficient OS sandbox; trust basis not chosen"))
        old = os.environ.get("FLEXFACTOR_TRUSTED_REPOS")
        os.environ["FLEXFACTOR_TRUSTED_REPOS"] = TMP
        try:
            d = self.trust.trust_decision(os.path.join(TMP, "proj"))
        finally:
            if old is None:
                os.environ.pop("FLEXFACTOR_TRUSTED_REPOS", None)
            else:
                os.environ["FLEXFACTOR_TRUSTED_REPOS"] = old
        self.assertTrue(d.allowed, d.reason)
        self.assertEqual(sb.require_containment_or_trust(TMP, trust_decision=d)["basis"],
                         "trusted-repo")

    def test_os_sandbox_basis_when_host_is_sufficient(self):
        if not sb.os_sandbox_sufficient(REP):
            self.skipTest(_blocked("no sufficient OS sandbox (process+memory+network)"))
        d = self.trust.TrustDecision(allowed=False, reason="untrusted")
        out = sb.require_containment_or_trust(TMP, trust_decision=d)
        self.assertEqual(out["basis"], "os-sandbox")
        self.assertIn("contained", out["claim"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
