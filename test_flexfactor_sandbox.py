"""Tests for flexfactor_sandbox - the cross-platform execution broker.

Run: python test_flexfactor_sandbox.py

A mechanism that is genuinely unavailable on the host SKIPS with a reason
containing "BLOCKED" - it never silently passes. Every resource/abuse test
drives a REAL child process.
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import flexfactor_sandbox as sb
from flexfactor_sandbox import Limits, ContainmentUnavailable

PY = sys.executable
REP = sb.capability_report()
IS_WIN = sb.IS_WINDOWS
TMP = tempfile.gettempdir()

# A GRANDCHILD IS MEASURED BY A HEARTBEAT, NEVER BY ITS PID.
#
# The pid a sandboxed process reports is not a host pid under any
# PID-namespace mechanism. Measured on WSL2 Ubuntu 2026-09-17, strongest=bwrap
# (`--unshare-all`, which is the default because `Limits.network` is False):
# the child reported `GRANDCHILD 3`, and host `/proc/3` does not exist - while
# the child's own namespace pid 2 IS a live host process. So a pid-based
# `assertFalse(pid_alive(gpid))` passed VACUOUSLY: it never observed the
# grandchild at all, and could even have observed an unrelated host process.
# On a mechanism without a namespace (strongest=rlimit, the ubuntu CI runner)
# the pid IS a host pid, but it can be REUSED under load, which made
# `test_spawn_and_kill_tree` fail intermittently on CI while passing 40/40 on
# an idle host.
#
# A file the grandchild keeps rewriting is immune to both: it advances only
# while that process is really running, wherever its pid lives.
_HEARTBEAT_GRANDCHILD = textwrap.dedent("""
    import os, sys, time
    path = sys.argv[1]
    part = path + ".part"
    n = 0
    while n < 4000:            # bounded: never leave an endless orphan behind
        n += 1
        with open(part, "w") as fh:
            fh.write(str(n))
        try:
            os.replace(part, path)   # atomic: a reader never sees a torn value
        except OSError:
            pass
        time.sleep(0.05)
""")

# Spawns the heartbeat grandchild, then outlives it. argv: script, beat file.
_HEARTBEAT_CHILD = textwrap.dedent("""
    import subprocess, sys, time
    subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
    time.sleep(60)
""")


def _heartbeat_paths(tag: str) -> tuple[str, str]:
    """Write the grandchild program to TMP; return (script, beat file).

    TMP is used because every POSIX mechanism binds TMPDIR read-write at the
    same path (see `_prepare_posix`), so the heartbeat is visible from the host
    even inside a bwrap filesystem namespace.
    """
    stamp = f"{os.getpid()}_{time.time_ns()}_{tag}"
    script = os.path.join(TMP, f"ff_beat_{stamp}.py")
    beat = os.path.join(TMP, f"ff_beat_{stamp}.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_HEARTBEAT_GRANDCHILD)
    return script, beat


def _unlink(*paths: str) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _beat(path: str) -> int:
    """The grandchild's current heartbeat count, 0 when it never wrote one."""
    for _ in range(20):
        try:
            with open(path, encoding="utf-8") as fh:
                text = (fh.read() or "").strip()
            return int(text) if text else 0
        except ValueError:
            return 0
        except OSError:
            time.sleep(0.02)   # Windows: the writer may hold it for an instant
    return 0


def _wait_for_beat(path: str, above: int = 0, timeout: float = 25.0) -> int:
    deadline = time.time() + timeout
    latest = _beat(path)
    while latest <= above and time.time() < deadline:
        time.sleep(0.05)
        latest = _beat(path)
    return latest


def _assert_heartbeat_stopped(case: unittest.TestCase, beat: str, what: str) -> None:
    """The heartbeat must not advance any more.

    Read AFTER a settle so a kill still in flight is not mistaken for a
    survivor, then require 30 heartbeat intervals of silence.
    """
    time.sleep(0.5)
    stopped = _beat(beat)
    time.sleep(1.5)
    case.assertEqual(stopped, _beat(beat),
                     f"grandchild heartbeat kept advancing after {what}")


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


class MemoryCgroupPreparationTests(unittest.TestCase):
    def _prepare(self, mechanism="bwrap", limits=None):
        level = {}
        contained = sb._prepare_posix([PY, "-c", "pass"],
            {"FLEXFACTOR_MEMORY_CGROUP_ROOT": "/sys/fs/cgroup/delegated"},
            TMP, limits or Limits(), level, {"strongest": mechanism}, None)
        self.addCleanup(contained.cleanup)
        return contained

    def test_unavailable_delegation_preserves_address_space_limit_and_reason(self):
        with mock.patch.object(sb.sys, "platform", "linux"), \
             mock.patch.object(sb, "_MemoryCgroup", side_effect=PermissionError("no delegation")):
            contained = self._prepare()
        self.assertEqual(contained.level["memory_mechanism"], "rlimit-as")
        self.assertIn("no delegation", contained.level["memory_fallback_reason"])
        self.assertNotIn("FLEXFACTOR_MEMORY_CGROUP_ROOT", contained.env)
        resource = mock.Mock(RLIMIT_AS=9, RLIMIT_NPROC=6)
        with mock.patch.dict(sys.modules, {"resource": resource}):
            contained.popen_kwargs["preexec_fn"]()
        resource.setrlimit.assert_any_call(9, (2 * 1024 ** 3, 2 * 1024 ** 3))
        resource.setrlimit.assert_any_call(6, (256, 256))
        self.assertEqual(contained.level["process_count_mechanism"], "rlimit-nproc")
        self.assertIn("no delegation", contained.level["process_count_fallback_reason"])

    def test_weaker_backend_cannot_expose_writable_controls(self):
        with mock.patch.object(sb.sys, "platform", "linux"), \
             mock.patch.object(sb, "_MemoryCgroup") as factory:
            contained = self._prepare("process-group")
        factory.assert_not_called()
        self.assertEqual(contained.level["memory_mechanism"], "rlimit-as")
        self.assertIn("read-only", contained.level["memory_fallback_reason"])

    def test_join_precedes_exec_and_failure_is_not_swallowed(self):
        guard = mock.Mock(path="/sys/fs/cgroup/delegated/flexfactor-test", swap="disabled", max_processes=256)
        guard.cleanup.return_value = None
        with mock.patch.object(sb.sys, "platform", "linux"), \
             mock.patch.object(sb, "_MemoryCgroup", return_value=guard):
            contained = self._prepare()
        resource = mock.Mock(RLIMIT_AS=9, RLIMIT_NPROC=6)
        with mock.patch.dict(sys.modules, {"resource": resource}):
            contained.popen_kwargs["preexec_fn"]()
        guard.join.assert_called_once()
        self.assertNotIn(mock.call(9, (2 * 1024 ** 3, 2 * 1024 ** 3)), resource.setrlimit.call_args_list)
        self.assertEqual(contained.level["memory_mechanism"], "cgroup-v2")
        self.assertEqual(contained.level["process_count_mechanism"], "cgroup-v2")
        self.assertEqual(contained.level["process_cgroup_limit"], 256)
        self.assertNotIn(mock.call(6, (256, 256)), resource.setrlimit.call_args_list)
        ro = ["--ro-bind", "/sys/fs/cgroup", "/sys/fs/cgroup", "--cap-drop", "ALL"]
        start = contained.argv.index("--cap-drop") - 3
        self.assertEqual(contained.argv[start:start + 5], ro)
        guard.join.side_effect = PermissionError("join refused")
        with mock.patch.dict(sys.modules, {"resource": resource}), self.assertRaises(PermissionError):
            contained.popen_kwargs["preexec_fn"]()
        contained.cleanup()
        self.assertEqual(contained.level["memory_cgroup_cleanup"], "removed")

    def test_non_cgroup_filesystem_is_rejected_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                sb._MemoryCgroup(directory, 64 * 1024 ** 2)
            self.assertEqual(os.listdir(directory), [])

    def test_process_limit_off_keeps_cpu_limit_and_memory_guard(self):
        guard = mock.Mock(path="/sys/fs/cgroup/delegated/flexfactor-test", swap="disabled", max_processes=None)
        guard.cleanup.return_value = None
        with mock.patch.object(sb.sys, "platform", "linux"), \
             mock.patch.object(sb, "_MemoryCgroup", return_value=guard) as factory:
            contained = self._prepare(limits=Limits(max_processes=None, cpu_seconds=3))
        factory.assert_called_once_with("/sys/fs/cgroup/delegated", 2 * 1024 ** 3, None)
        resource = mock.Mock(RLIMIT_AS=9, RLIMIT_NPROC=6, RLIMIT_CPU=0)
        with mock.patch.dict(sys.modules, {"resource": resource}):
            contained.popen_kwargs["preexec_fn"]()
        resource.setrlimit.assert_called_once_with(0, (3, 3))
        self.assertEqual(contained.level["process_count_mechanism"], "off")
        self.assertEqual(contained.level["memory_mechanism"], "cgroup-v2")

    def test_process_limit_readback_mismatch_cleans_group_and_refuses_it(self):
        mount = "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=mount)), \
             mock.patch.object(sb.os.path, "realpath", side_effect=lambda p: p), \
             mock.patch.object(sb.os.path, "commonpath", return_value="/sys/fs/cgroup"), \
             mock.patch.object(sb.os, "O_DIRECTORY", 0, create=True), \
             mock.patch.object(sb.os, "O_NOFOLLOW", 0, create=True), \
             mock.patch.object(sb.os, "stat", return_value=mock.Mock(st_dev=1)), \
             mock.patch.object(sb.os, "open", return_value=61), \
             mock.patch.object(sb.os, "mkdir"), \
             mock.patch.object(sb._MemoryCgroup, "_write") as write, \
             mock.patch.object(sb._MemoryCgroup, "_read", side_effect=["1024", "max"]), \
             mock.patch.object(sb._MemoryCgroup, "cleanup", return_value=None) as cleanup:
            with self.assertRaisesRegex(OSError, "pids.max readback"):
                sb._MemoryCgroup("/sys/fs/cgroup/delegated", 1024, 5)
        write.assert_any_call("pids.max", "5")
        cleanup.assert_called_once()

    def test_failed_cleanup_is_idempotent_and_never_removes_relative_to_cwd(self):
        guard = sb._MemoryCgroup.__new__(sb._MemoryCgroup)
        guard.parent_fd, guard.fd = 61, 62
        guard.name, guard.created, guard.cleanup_error = "flexfactor-fixture", True, None
        guard.kill = mock.Mock()
        inode = mock.Mock(st_ino=123)
        with mock.patch.object(sb.os, "stat", return_value=inode), \
             mock.patch.object(sb.os, "fstat", return_value=inode), \
             mock.patch.object(sb.os, "rmdir", side_effect=OSError("busy")) as remove, \
             mock.patch.object(sb.os, "close"), mock.patch.object(sb.time, "sleep"):
            first = guard.cleanup()
            attempts = remove.call_count
            self.assertIn("busy", first)
            self.assertEqual(guard.cleanup(), first)
            self.assertEqual(remove.call_count, attempts)
            self.assertTrue(all(call.kwargs["dir_fd"] == 61 for call in remove.call_args_list))
        self.assertIsNone(guard.parent_fd)


@unittest.skipUnless(sys.platform.startswith("linux"), "BLOCKED: Linux cgroup-v2 tests")
class LinuxMemoryCgroupTests(unittest.TestCase):
    def setUp(self):
        self.root = os.environ.get("FLEXFACTOR_MEMORY_CGROUP_ROOT")
        if not self.root or REP["strongest"] != "bwrap":
            self.skipTest("BLOCKED: opt-in delegated cgroup root and bwrap required")

    def run_guarded(self, command, memory=64 * 1024 ** 2, extra_env=None, timeout=30):
        env = dict(os.environ)
        env.update(extra_env or {})
        cp = sb.run_contained(command, TMP, env=env,
                              limits=Limits(timeout_s=timeout, memory_bytes=memory))
        level = cp.flexfactor_containment["level"]
        self.assertEqual(level.get("memory_mechanism"), "cgroup-v2", level)
        self.assertEqual(level.get("memory_cgroup_cleanup"), "removed", level)
        self.assertFalse(os.path.exists(level["memory_cgroup"]))
        return cp

    def test_physical_budget_kills_oversized_allocation(self):
        cp = self.run_guarded([PY, "-c", "x=bytearray(128*1024**2); print('UNBOUNDED')"])
        self.assertIn(cp.returncode, (-9, 137), cp.stderr)
        self.assertNotIn("UNBOUNDED", cp.stdout)

    def test_virtual_reservation_fits_without_raising_physical_budget(self):
        script = ("import mmap,os; x=mmap.mmap(-1,4*1024**3); x[0]=1; "
                  "assert 'FLEXFACTOR_MEMORY_CGROUP_ROOT' not in os.environ; "
                  "assert os.environ['FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY']=='1'; print('RESERVED')")
        cp = self.run_guarded([PY, "-c", script], extra_env={"FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY": "1"})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("RESERVED", cp.stdout)
        self.assertEqual(os.environ["FLEXFACTOR_MEMORY_CGROUP_ROOT"], self.root)

    def test_timeout_cleans_the_task_group(self):
        cp = self.run_guarded([PY, "-c", "import time; time.sleep(20)"], timeout=1)
        self.assertEqual(cp.returncode, 124, cp.stderr)

    def test_target_cannot_raise_limit_or_access_host_proc_with_writable_mounts(self):
        script = ("import os,sys; "
                  "\nassert not os.path.exists(sys.argv[2]), 'host supervisor root exposed'"
                  "\nstatus=open('/proc/self/status').read()"
                  "\nassert any(line.split()==['NoNewPrivs:', '1'] for line in status.splitlines()), 'privilege elevation permitted'"
                  "\nfor line in status.splitlines():"
                  "\n if line.startswith(('CapEff:', 'CapPrm:', 'CapAmb:')): assert int(line.split()[1], 16)==0, 'target retained capabilities'"
                  "\nfor fd in os.listdir('/proc/self/fd'):"
                  "\n try: target=os.readlink('/proc/self/fd/'+fd)"
                  "\n except FileNotFoundError: continue"
                  "\n assert not target.startswith('/sys/fs/cgroup'), 'supervisor cgroup descriptor inherited'"
                  "\nfor name in ('memory.max', 'pids.max'):"
                  "\n try:\n  open(sys.argv[1]+'/'+name,'w').write('max')"
                  "\n except OSError:\n  print('PROTECTED', name)"
                  "\n else:\n  sys.exit(3)")
        c = sb.prepare([PY, "-c", script], TMP, dict(os.environ),
                       Limits(memory_bytes=64 * 1024 ** 2, network=True,
                              writable_dirs=["/", "/proc", "/sys"]))
        try:
            self.assertEqual(c.level.get("memory_mechanism"), "cgroup-v2", c.level)
            cp = subprocess.run(c.argv + [c.level["memory_cgroup"], f"/proc/{os.getpid()}/root"], cwd=c.cwd, env=c.env,
                                capture_output=True, text=True, timeout=20, **c.popen_kwargs)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn("PROTECTED memory.max", cp.stdout)
            self.assertIn("PROTECTED pids.max", cp.stdout)
        finally:
            c.cleanup()
        self.assertEqual(c.level["memory_cgroup_cleanup"], "removed")

    def test_powershell_starts_with_unchanged_two_gib_physical_budget(self):
        pwsh = os.environ.get("FLEXFACTOR_TEST_PWSH")
        if not pwsh:
            self.skipTest("BLOCKED: FLEXFACTOR_TEST_PWSH portable test executable not configured")
        with tempfile.TemporaryDirectory() as directory:
            env = {"POWERSHELL_TELEMETRY_OPTOUT": "1", "POWERSHELL_UPDATECHECK": "Off"}
            for kind in ("CACHE", "CONFIG", "DATA"):
                env["XDG_" + kind + "_HOME"] = directory
            cp = self.run_guarded([pwsh, "-NoProfile", "-NonInteractive", "-Command", "Write-Output CGROUP_OK"],
                                  memory=2 * 1024 ** 3, extra_env=env, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("CGROUP_OK", cp.stdout)


class RunContainedTests(unittest.TestCase):
    def test_target_child_cannot_resume_parent_mobile_queue(self):
        env = dict(os.environ, FLEXFACTOR_QUEUE_ID="parent-mobile-request",
                   FLEXFACTOR_QUEUE_STATE="parent-queue.json",
                   FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY="1")
        script = (
            "import os,json; print(json.dumps({k:os.environ.get(k) for k in "
            "['FLEXFACTOR_QUEUE_ID','FLEXFACTOR_QUEUE_STATE',"
            "'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY']}))"
        )
        cp = sb.run_contained([PY, "-c", script], TMP, env=env,
                              limits=Limits(timeout_s=60))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(json.loads(cp.stdout), {
            "FLEXFACTOR_QUEUE_ID": None, "FLEXFACTOR_QUEUE_STATE": None,
            "FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY": "1"})
        self.assertEqual(env["FLEXFACTOR_QUEUE_ID"], "parent-mobile-request")
        self.assertEqual(env["FLEXFACTOR_QUEUE_STATE"], "parent-queue.json")

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
        """A timeout must kill the GRANDCHILD too - measured, not assumed.

        This used to read the grandchild's own reported pid and assert it was
        not alive on the host. Under any PID-namespace mechanism that pid is
        not a host pid, so the assertion could not fail: see the measurement
        recorded beside `_HEARTBEAT_GRANDCHILD`.
        """
        script, beat = _heartbeat_paths("timeout")
        self.addCleanup(_unlink, script, beat, beat + ".part")
        t0 = time.time()
        cp = sb.run_contained([PY, "-c", _HEARTBEAT_CHILD, script, beat], TMP,
                              limits=Limits(timeout_s=3))
        self.assertEqual(cp.returncode, 124, cp.stderr)
        self.assertTrue(cp.flexfactor_launch_error)
        self.assertIn("timed out", cp.stderr)
        self.assertLess(time.time() - t0, 40)
        # VERIFY THE VERIFICATION: without a heartbeat the check below is vacuous.
        self.assertGreater(_beat(beat), 0,
                           "the grandchild never wrote a heartbeat, so 'the tree "
                           "was killed' would prove nothing")
        _assert_heartbeat_stopped(self, beat, "the timeout tree kill")

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
        # A returned Popen is NOT proof of a running child on Windows. At
        # ActiveProcessLimit a Job Object lets CreateProcess succeed and then
        # TERMINATES the new process because the job association failed (it
        # exits 101), so counting calls that did not raise OSError measures
        # nothing but timing: this assertion read 7, 16, 27, 41 and 44 live
        # "escapes" on one host while containment was working perfectly, and
        # only passed on CI because a faster runner failed CreateProcess
        # outright more often. Count the children actually ALIVE under the cap
        # - the one quantity both mechanisms bound (POSIX RLIMIT_NPROC makes
        # fork itself fail, which is why `failed` stays meaningful there).
        child = textwrap.dedent("""
            import subprocess, sys, time
            kids, failed = [], 0
            for i in range(50):
                try:
                    kids.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL))
                except OSError:
                    failed += 1
            time.sleep(3)
            alive = sum(1 for k in kids if k.poll() is None)
            print("SPAWNED", len(kids), "FAILED", failed, "ALIVE", alive, flush=True)
            for k in kids:
                k.kill()
            for k in kids:
                k.wait()
        """)
        cp = sb.run_contained([PY, "-c", child], TMP,
                              limits=Limits(timeout_s=120, max_processes=5))
        level = cp.flexfactor_containment["level"]
        if not IS_WIN and os.environ.get("FLEXFACTOR_MEMORY_CGROUP_ROOT") and REP["strongest"] == "bwrap":
            self.assertEqual(level["process_count_mechanism"], "cgroup-v2", level)
            self.assertEqual(level["process_cgroup_limit"], 5)
            self.assertEqual(level["memory_cgroup_cleanup"], "removed")
        self.assertIn("SPAWNED", cp.stdout, f"rc={cp.returncode} err={cp.stderr}")
        parts = cp.stdout.split()
        spawned, failed, alive = int(parts[1]), int(parts[3]), int(parts[5])
        self.assertLess(alive, 50, cp.stdout)
        # 5 active processes including the child itself -> at most 4 live kids.
        # Measured unconfined (max_processes=None) this reads 50, so the bound
        # still fails if the limit ever stops being applied.
        self.assertLessEqual(alive, 4, cp.stdout)
        if not IS_WIN:
            self.assertGreater(failed, 0, cp.stdout)
            self.assertLess(spawned, 50, cp.stdout)

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
        """`kill_tree()` must stop the GRANDCHILD, not only the direct child.

        Heartbeat-measured for the reasons recorded beside
        `_HEARTBEAT_GRANDCHILD`: the previous pid-identity form failed on the
        ubuntu CI runner (host pid, reusable under load) and failed locally
        under bwrap (namespace pid, never a host pid at all).
        """
        script, beat = _heartbeat_paths("spawn")
        self.addCleanup(_unlink, script, beat, beat + ".part")
        proc, err, kill_tree = sb.spawn_contained(
            [PY, "-c", _HEARTBEAT_CHILD, script, beat], TMP,
            limits=Limits(timeout_s=60))
        self.assertIsNotNone(proc, err)
        self.assertEqual(err, "")
        try:
            first = _wait_for_beat(beat)
            self.assertGreater(first, 0,
                               "the grandchild never started, so 'kill_tree "
                               "stopped it' would prove nothing")
            # VERIFY THE VERIFICATION: the heartbeat really does ADVANCE while
            # the tree is alive, so a frozen one afterwards is meaningful.
            self.assertGreater(_wait_for_beat(beat, above=first, timeout=10.0), first,
                               "the heartbeat never advanced while the grandchild "
                               "was running; 'stopped' would be indistinguishable "
                               "from 'never ran'")
        finally:
            kill_tree()
        _assert_heartbeat_stopped(self, beat, "kill_tree()")
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
