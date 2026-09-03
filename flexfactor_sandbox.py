"""flexfactor_sandbox.py - cross-platform execution broker for FlexFactor.

FlexFactor runs a target repository's install / build / test commands, i.e. it
executes third-party code. Until now that went through ``flexfactor._run`` /
``flexfactor._spawn`` with ONLY environment filtering and proxy poisoning
(``_no_network_env``), which is not an OS sandbox. This module is the broker
that wraps every such command with the STRONGEST mechanism the host can
actually enforce, and - equally important - reports truthfully what that is.

Stdlib only. Never imports flexfactor (the wiring agent injects it the other
way round, the same pattern as flexfactor_prodready / flexfactor_competitors).

What is OS-ENFORCED, per platform (probed at runtime, never assumed):

  Windows  Job Object (ctypes: CreateJobObjectW / SetInformationJobObject /
           AssignProcessToJobObject / TerminateJobObject) with
           JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE + PROCESS_MEMORY +
           ACTIVE_PROCESS + JOB_TIME. The child is created SUSPENDED, assigned
           to the job, then resumed, so no grandchild can be spawned before the
           job applies. Gives: whole-tree kill on timeout/cancel, per-process
           commit limit, live process-count limit, CPU-time limit.
           NETWORK IS NOT ISOLATED on Windows. Proxy-poisoned env is applied
           when ``Limits.network`` is False and is reported as
           "best-effort-env": raw sockets bypass it entirely.
           FOLLOW-UP (ISOLATION_SPIKE.md option D): AppContainer launch
           (CreateProcess + STARTUPINFOEX SECURITY_CAPABILITIES without the
           internetClient / privateNetworkClientServer capabilities) is the
           airtight, no-admin mechanism. It needs an AppContainer profile
           (CreateAppContainerProfile), an ACL grant for the container SID on
           the project dir + toolchain dirs, and cleanup of both. Not built.

  Linux    ``bwrap`` (bubblewrap) when on PATH and a probe launch succeeds:
           ``--unshare-all --ro-bind / / --bind <work> <work> --dev /dev
           --proc /proc --die-with-parent``. Gives OS-enforced network
           isolation (net namespace), PID namespace (the tree dies with
           bwrap), read-only filesystem outside the work dir.
           Else ``unshare -rn`` (user + net namespace) for network isolation
           only. RLIMIT_AS / RLIMIT_NPROC / RLIMIT_CPU via ``resource`` in
           ``preexec_fn`` give OS-enforced memory / process-count / CPU
           limits on every POSIX path. Process-tree kill without bwrap is a
           process group (``start_new_session`` + ``killpg``), which a child
           can escape with ``setsid`` - reported as "best-effort".

  Other    Nothing OS-enforced; env scrub + proxy poison only; reported as such.

Every ``CompletedProcess`` this module returns carries
``flexfactor_containment = {mechanism, level, applied: True}`` so the run
manifest can state exactly what protected that command. ``capability_report()
["claim"]`` never uses the word "contained" unless BOTH network isolation and
process-tree containment are OS-enforced.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable

IS_WINDOWS = os.name == "nt"
OUTPUT_CAP_BYTES = 8 * 1024 * 1024   # per stream; the rest is drained and counted
DEAD_PROXY = "http://127.0.0.1:9"    # loopback discard port: nothing answers

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class Limits:
    timeout_s: int = 900
    memory_bytes: int | None = 2 * 1024 ** 3
    max_processes: int | None = 256
    cpu_seconds: int | None = None
    network: bool = False
    writable_dirs: list[str] = field(default_factory=list)


@dataclass
class Contained:
    argv: list[str]
    env: dict
    cwd: str
    mechanism: str
    level: dict
    cleanup: Callable[[], None]
    # internal hooks the runner uses; not part of the wiring contract
    popen_kwargs: dict = field(default_factory=dict)
    attach: Callable[[subprocess.Popen], str] | None = None
    kill_tree: Callable[[subprocess.Popen], None] | None = None


class ContainmentUnavailable(RuntimeError):
    """Raised when neither an OS sandbox nor owner trust authorizes execution."""


# ---------------------------------------------------------------------------
# Environment scrubbing + network poisoning
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
_SECRET_PREFIXES = ("AWS_", "GITHUB_", "ANTHROPIC_", "OPENAI_")
_SECRET_EXACT = {"NPM_TOKEN"}
# Names a build needs even though the loose pattern could catch them
# (e.g. PYTHONHASHSEED has no hit, but be explicit). A keep-listed name is
# still stripped when it carries a STRONG credential marker (NODE_AUTH_TOKEN).
_KEEP_EXACT = {"PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT",
               "PATHEXT", "COMSPEC", "LANG", "LC_ALL", "NPM_CONFIG_CACHE",
               "SYSTEMDRIVE", "WINDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
               "HOMEDRIVE", "HOMEPATH", "USERNAME", "OS", "NUMBER_OF_PROCESSORS",
               "PROCESSOR_ARCHITECTURE", "SHELL", "USER", "LOGNAME", "TERM"}
_KEEP_PREFIXES = ("PYTHON", "NODE_")
_STRONG_SECRET_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)


def is_secret_env_name(name: str) -> bool:
    """True when an env var name looks like a credential and must not reach
    third-party build code."""
    up = name.upper()
    if up in _KEEP_EXACT or up.startswith(_KEEP_PREFIXES):
        return bool(_STRONG_SECRET_RE.search(up))
    if up in _SECRET_EXACT or up.startswith(_SECRET_PREFIXES):
        return True
    return bool(_SECRET_RE.search(up))


def scrub_env(env: dict) -> tuple[dict, list[str]]:
    """Return (clean_env, stripped_names)."""
    out, stripped = {}, []
    for k, v in env.items():
        if is_secret_env_name(k):
            stripped.append(k)
        else:
            out[k] = v
    return out, sorted(stripped)


def poison_network_env(env: dict) -> dict:
    """Best-effort env-level network denial (ISOLATION_SPIKE option A).
    Honored by npm/yarn/pip/curl/node-fetch/undici; raw sockets bypass it."""
    env = dict(env)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env[k] = DEAD_PROXY
    for k in ("NO_PROXY", "no_proxy"):
        env[k] = ""
    env.update({"npm_config_offline": "true", "npm_config_registry": DEAD_PROXY,
                "npm_config_fund": "false", "npm_config_audit": "false",
                "PIP_NO_INDEX": "1"})
    return env


def _resolve_exe(cmd: list[str]) -> list[str]:
    """Windows: bare names must be resolved through PATHEXT (npm is npm.CMD);
    CreateProcess only searches .exe. Mirrors flexfactor._winify."""
    if not IS_WINDOWS or not cmd:
        return list(cmd)
    exe = cmd[0]
    if os.path.splitext(exe)[1] or os.sep in exe or (os.altsep and os.altsep in exe):
        return list(cmd)
    resolved = shutil.which(exe)
    return [resolved, *cmd[1:]] if resolved else list(cmd)


# ---------------------------------------------------------------------------
# Windows Job Object (ctypes)
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_JOB_TIME = 0x4
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _CREATE_SUSPENDED = 0x4
    _TH32CS_SNAPTHREAD = 0x4
    _THREAD_SUSPEND_RESUME = 0x2
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Thread32First.restype = wintypes.BOOL
    _k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.Thread32Next.restype = wintypes.BOOL
    _k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    def _winerr(what: str) -> str:
        code = ctypes.get_last_error()
        return f"{what} failed: WinError {code}: {ctypes.FormatError(code).strip()}"

    def _create_job(limits: Limits) -> tuple[int | None, str]:
        hjob = _k32.CreateJobObjectW(None, None)
        if not hjob:
            return None, _winerr("CreateJobObjectW")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if limits.memory_bytes:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(limits.memory_bytes)
        if limits.max_processes:
            flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = int(limits.max_processes)
        if limits.cpu_seconds:
            flags |= _JOB_OBJECT_LIMIT_JOB_TIME
            info.BasicLimitInformation.PerJobUserTimeLimit = int(limits.cpu_seconds) * 10_000_000
        info.BasicLimitInformation.LimitFlags = flags
        if not _k32.SetInformationJobObject(hjob, _JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            err = _winerr("SetInformationJobObject")
            _k32.CloseHandle(hjob)
            return None, err
        return hjob, ""

    def _resume_process_threads(pid: int) -> str:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snap or snap == _INVALID_HANDLE:
            return _winerr("CreateToolhelp32Snapshot")
        resumed = 0
        try:
            te = _THREADENTRY32()
            te.dwSize = ctypes.sizeof(te)
            ok = _k32.Thread32First(snap, ctypes.byref(te))
            while ok:
                if te.th32OwnerProcessID == pid:
                    ht = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                    if ht:
                        _k32.ResumeThread(ht)
                        _k32.CloseHandle(ht)
                        resumed += 1
                ok = _k32.Thread32Next(snap, ctypes.byref(te))
        finally:
            _k32.CloseHandle(snap)
        return "" if resumed else f"no thread of pid {pid} found to resume"

    def _pid_alive_windows(pid: int) -> bool:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)


def pid_alive(pid: int) -> bool:
    """True when a process with this pid is still running (cross-platform)."""
    if IS_WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # zombie children of THIS process still answer kill(0); reap them
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    return True


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------

_REPORT_CACHE: dict | None = None
_REPORT_LOCK = threading.Lock()


def _probe_cmd(argv: list[str], timeout: float = 15.0) -> tuple[bool, str]:
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]}: probe timed out"
    except OSError as ex:
        return False, f"{argv[0]}: {ex}"
    if cp.returncode == 0:
        return True, "probe launch succeeded"
    err = cp.stderr.decode("utf-8", "replace").strip().splitlines()
    return False, f"rc {cp.returncode}: {err[-1] if err else '(no stderr)'}"


def _probe_windows_job() -> tuple[bool, str]:
    """Really create a job, spawn a suspended child, assign, resume, wait."""
    lim = Limits(timeout_s=20, memory_bytes=256 * 1024 ** 2, max_processes=4)
    hjob, err = _create_job(lim)
    if hjob is None:
        return False, err
    try:
        proc = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=_CREATE_SUSPENDED)
    except OSError as ex:
        _k32.CloseHandle(hjob)
        return False, f"probe spawn failed: {ex}"
    try:
        if not _k32.AssignProcessToJobObject(hjob, int(proc._handle)):
            msg = _winerr("AssignProcessToJobObject")
            proc.kill()
            return False, msg + " (is this process inside a job that forbids nesting?)"
        rerr = _resume_process_threads(proc.pid)
        if rerr:
            proc.kill()
            return False, rerr
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, "probe child never exited"
        return rc == 0, "job-assigned child ran to exit 0" if rc == 0 else f"probe child rc {rc}"
    finally:
        _k32.CloseHandle(hjob)


def _posix_rlimit_available() -> tuple[bool, str]:
    try:
        import resource  # noqa: F401
        for name in ("RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_CPU"):
            if not hasattr(resource, name):
                return False, f"resource.{name} missing"
        return True, "resource.RLIMIT_AS/NPROC/CPU present"
    except ImportError as ex:
        return False, f"resource module unavailable: {ex}"


def capability_report(refresh: bool = False) -> dict:
    """What THIS host can enforce. Probed, cached per process."""
    global _REPORT_CACHE
    with _REPORT_LOCK:
        if _REPORT_CACHE is not None and not refresh:
            return dict(_REPORT_CACHE)
        rep = _build_report()
        _REPORT_CACHE = rep
        return dict(rep)


def _build_report() -> dict:
    mechanisms: list[dict] = []
    strongest = None
    network = "none"
    process_tree = "none"
    memory = "none"
    process_count = "none"
    cpu_time = "none"

    if IS_WINDOWS:
        ok, detail = _probe_windows_job()
        mechanisms.append({"name": "win32-job-object", "available": ok,
                           "enforces": ["process-tree-kill", "memory", "process-count",
                                        "cpu-time"] if ok else [],
                           "detail": detail})
        mechanisms.append({"name": "win32-appcontainer", "available": False, "enforces": [],
                           "detail": "not implemented (ISOLATION_SPIKE option D; needs "
                                     "CreateAppContainerProfile + SID ACL grants)"})
        if ok:
            strongest = "win32-job-object"
            process_tree = memory = process_count = cpu_time = "os-enforced"
        mechanisms.append({"name": "proxy-poison-env", "available": True,
                           "enforces": ["network(best-effort)"],
                           "detail": "HTTP(S)_PROXY/ALL_PROXY -> 127.0.0.1:9, npm offline, "
                                     "PIP_NO_INDEX; raw sockets bypass it"})
        network = "best-effort-env"
    elif os.name == "posix":
        bwrap = shutil.which("bwrap")
        if bwrap:
            ok, detail = _probe_cmd([bwrap, "--unshare-all", "--ro-bind", "/", "/",
                                     "--dev", "/dev", "--proc", "/proc", "--die-with-parent",
                                     "--", "/bin/true"])
        else:
            ok, detail = False, "bwrap not on PATH"
        mechanisms.append({"name": "bwrap", "available": ok,
                           "enforces": ["network", "process-tree-kill", "ro-filesystem"]
                           if ok else [], "detail": detail})
        unshare = shutil.which("unshare")
        if unshare:
            uok, udetail = _probe_cmd([unshare, "-rn", "/bin/true"])
        else:
            uok, udetail = False, "unshare not on PATH"
        mechanisms.append({"name": "unshare-user-net", "available": uok,
                           "enforces": ["network"] if uok else [], "detail": udetail})
        rok, rdetail = _posix_rlimit_available()
        mechanisms.append({"name": "rlimit", "available": rok,
                           "enforces": ["memory", "process-count", "cpu-time"] if rok else [],
                           "detail": rdetail})
        mechanisms.append({"name": "process-group", "available": True,
                           "enforces": ["process-tree-kill(best-effort)"],
                           "detail": "start_new_session + killpg; escapable via setsid"})
        mechanisms.append({"name": "proxy-poison-env", "available": True,
                           "enforces": ["network(best-effort)"],
                           "detail": "applied in addition to any namespace"})
        if ok:
            strongest, network, process_tree = "bwrap", "os-enforced", "os-enforced"
        elif uok:
            strongest, network, process_tree = "unshare-user-net", "os-enforced", "best-effort"
        else:
            strongest, network, process_tree = ("rlimit" if rok else "process-group"), \
                "best-effort-env", "best-effort"
        if rok:
            memory = process_count = cpu_time = "os-enforced"
    else:
        mechanisms.append({"name": "proxy-poison-env", "available": True,
                           "enforces": ["network(best-effort)"], "detail": "env only"})
        network = "best-effort-env"

    rep = {"platform": sys.platform, "mechanisms": mechanisms, "strongest": strongest,
           "network_isolation": network, "process_tree": process_tree, "memory": memory,
           "process_count": process_count, "cpu_time": cpu_time,
           "credential_scrub": "applied"}
    rep["claim"] = _claim_sentence(rep)
    rep["claim_headline"] = _claim_headline(rep)
    return rep


def _claim_headline(rep: dict) -> str:
    """A SHORT containment line for surfaces that cannot render the full claim.

    Truncating ``claim`` to fit a UI row is how the honest half disappears: the
    long sentence opens with the OS-enforced mechanisms and only names what is
    NOT contained near the end, so a 110- or 160-character slice showed a
    reader every guarantee and none of the holes (measured 2026-08-25: the
    dashboard evidence record cut the string mid-word at "raw-socket e", losing
    "gress is NOT prevented and third-party code can still reach the network").
    Callers must render THIS instead of slicing ``claim`` - it is built
    negative-first and is short by construction, so nothing has to be cut.
    """
    unenforced = [name for name, value in (("network", rep["network_isolation"]),
                                           ("process-tree", rep["process_tree"]),
                                           ("memory", rep["memory"]),
                                           ("cpu", rep["cpu_time"]))
                  if value != "os-enforced"]
    mech = rep["strongest"] or "env-only"
    if not unenforced:
        return f"contained by {mech}: network, process-tree, memory, cpu all OS-enforced"
    return (f"NOT contained: {', '.join(unenforced)} NOT OS-enforced "
            f"(strongest mechanism: {mech})")


def _claim_sentence(rep: dict) -> str:
    net, tree, mem = rep["network_isolation"], rep["process_tree"], rep["memory"]
    if net == "os-enforced" and tree == "os-enforced":
        return (f"Third-party build/test code is contained by {rep['strongest']}: network "
                f"isolation and process-tree kill are OS-enforced, memory/process-count "
                f"limits are {mem}; credentials are scrubbed from the environment.")
    parts = []
    parts.append(f"process tree {tree}" + (f" via {rep['strongest']}" if rep['strongest'] else ""))
    parts.append(f"memory {mem}")
    parts.append(f"process count {rep['process_count']}")
    parts.append(f"network isolation {net}")
    return ("NOT an OS sandbox: " + ", ".join(parts) +
            "; raw-socket egress is NOT prevented and third-party code can still reach "
            "the network. Credentials are scrubbed from the environment.")


# ---------------------------------------------------------------------------
# prepare(): wrap argv + env with the strongest mechanism
# ---------------------------------------------------------------------------


def prepare(cmd: list[str], cwd: str, env: dict | None, limits: Limits, *,
            source_root: str | None = None) -> Contained:
    base = dict(env if env is not None else os.environ)
    clean, stripped = scrub_env(base)
    if not limits.network:
        clean = poison_network_env(clean)
    rep = capability_report()
    argv = _resolve_exe(cmd)
    cwd = os.path.abspath(cwd)
    level = {"mechanism": rep["strongest"] or "env-only",
             "network_isolation": ("best-effort-env" if not limits.network and
                                   rep["network_isolation"] == "none" else
                                   rep["network_isolation"]) if not limits.network else "off",
             "process_tree": rep["process_tree"],
             "memory": rep["memory"] if limits.memory_bytes else "off",
             "process_count": rep["process_count"] if limits.max_processes else "off",
             "cpu_time": rep["cpu_time"] if limits.cpu_seconds else "off",
             "credentials_stripped": stripped,
             "limits": {"timeout_s": limits.timeout_s, "memory_bytes": limits.memory_bytes,
                        "max_processes": limits.max_processes,
                        "cpu_seconds": limits.cpu_seconds, "network": limits.network}}

    if IS_WINDOWS and rep["strongest"] == "win32-job-object":
        return _prepare_windows_job(argv, clean, cwd, limits, level)
    if os.name == "posix":
        return _prepare_posix(argv, clean, cwd, limits, level, rep, source_root)
    # no OS mechanism at all
    return Contained(argv=argv, env=clean, cwd=cwd, mechanism="env-only", level=level,
                     cleanup=lambda: None, popen_kwargs={},
                     kill_tree=lambda p: p.kill())


def _prepare_windows_job(argv, env, cwd, limits, level) -> Contained:
    state = {"hjob": None}

    def attach(proc: subprocess.Popen) -> str:
        hjob, err = _create_job(limits)
        if hjob is None:
            return err
        state["hjob"] = hjob
        if not _k32.AssignProcessToJobObject(hjob, int(proc._handle)):
            return _winerr("AssignProcessToJobObject")
        return _resume_process_threads(proc.pid)

    def kill_tree(proc: subprocess.Popen) -> None:
        hjob = state["hjob"]
        if hjob:
            _k32.TerminateJobObject(hjob, 124)
        else:  # job never attached: best-effort tree kill
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        try:
            proc.kill()
        except OSError:
            pass

    def cleanup() -> None:
        hjob = state.pop("hjob", None)
        if hjob:
            _k32.CloseHandle(hjob)   # KILL_ON_JOB_CLOSE finishes any stragglers

    return Contained(argv=argv, env=env, cwd=cwd, mechanism="win32-job-object",
                     level=level, cleanup=cleanup,
                     popen_kwargs={"creationflags": _CREATE_SUSPENDED |
                                   getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)},
                     attach=attach, kill_tree=kill_tree)


def _prepare_posix(argv, env, cwd, limits, level, rep, source_root) -> Contained:
    mech = rep["strongest"] or "process-group"
    wrapped = list(argv)
    if mech == "bwrap":
        bw = [shutil.which("bwrap"), "--unshare-all" if not limits.network else "--unshare-pid",
              "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent"]
        if limits.network:
            bw += ["--share-net"]
        rw = [cwd] + [os.path.abspath(d) for d in limits.writable_dirs]
        for d in (os.environ.get("TMPDIR") or "/tmp",):
            if os.path.isdir(d):
                rw.append(d)
        if source_root and os.path.isdir(source_root):
            rw.append(os.path.abspath(source_root))
        for d in dict.fromkeys(rw):
            bw += ["--bind", d, d]
        bw += ["--chdir", cwd, "--"]
        wrapped = bw + wrapped
    elif mech == "unshare-user-net" and not limits.network:
        wrapped = [shutil.which("unshare"), "-rn", "--"] + wrapped

    def preexec():
        try:
            import resource
            if limits.memory_bytes:
                resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
            if limits.max_processes:
                resource.setrlimit(resource.RLIMIT_NPROC,
                                   (limits.max_processes, limits.max_processes))
            if limits.cpu_seconds:
                resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        except Exception:
            pass  # never block the launch over a refused rlimit; the report says what applied

    def kill_tree(proc: subprocess.Popen) -> None:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.kill()
        except OSError:
            pass

    return Contained(argv=wrapped, env=env, cwd=cwd, mechanism=mech, level=level,
                     cleanup=lambda: None,
                     popen_kwargs={"start_new_session": True, "preexec_fn": preexec},
                     kill_tree=kill_tree)


# ---------------------------------------------------------------------------
# run_contained / spawn_contained
# ---------------------------------------------------------------------------


def _drain(stream, cap: int, sink: dict, key: str) -> None:
    buf = bytearray()
    dropped = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            room = cap - len(buf)
            if room > 0:
                buf += chunk[:room]
                dropped += max(0, len(chunk) - room)
            else:
                dropped += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
    sink[key] = bytes(buf)
    sink[key + "_dropped"] = dropped


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def run_contained(cmd: list[str], cwd: str, *, limits: Limits, env: dict | None = None,
                  source_root: str | None = None) -> subprocess.CompletedProcess:
    """Run to completion under containment. NEVER raises (mirrors flexfactor._run):
    launch failure -> non-zero rc + flexfactor_launch_error=True; timeout -> rc 124
    with the whole tree killed. stdout/stderr are always str."""
    def _tag(cp: subprocess.CompletedProcess, mech: str, level: dict, *,
             launch_error: bool = False, truncated=None) -> subprocess.CompletedProcess:
        if cp.stdout is None:
            cp.stdout = ""
        if cp.stderr is None:
            cp.stderr = ""
        cp.flexfactor_containment = {"mechanism": mech, "level": level, "applied": True}
        cp.flexfactor_output_truncated = truncated or {"stdout": 0, "stderr": 0}
        if launch_error:
            cp.flexfactor_launch_error = True
        return cp

    try:
        c = prepare(cmd, cwd, env, limits, source_root=source_root)
    except Exception as ex:  # a broken probe must not take the audit down
        return _tag(subprocess.CompletedProcess(cmd, 1, "", f"containment prepare failed: "
                                                f"{type(ex).__name__}: {ex}"),
                    "none", {}, launch_error=True)
    try:
        try:
            proc = subprocess.Popen(c.argv, cwd=c.cwd, env=c.env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    **c.popen_kwargs)
        except FileNotFoundError as ex:
            return _tag(subprocess.CompletedProcess(
                cmd, 127, "", f"executable not found: {(cmd or ['?'])[0]} ({ex})"),
                c.mechanism, c.level, launch_error=True)
        except OSError as ex:
            return _tag(subprocess.CompletedProcess(
                cmd, 1, "", f"failed to launch {(cmd or ['?'])[0]}: {ex}"),
                c.mechanism, c.level, launch_error=True)
        except Exception as ex:
            return _tag(subprocess.CompletedProcess(
                cmd, 1, "", f"could not run {(cmd or ['?'])[0]}: {type(ex).__name__}: {ex}"),
                c.mechanism, c.level, launch_error=True)

        if c.attach is not None:
            err = c.attach(proc)
            if err:
                # Child is SUSPENDED and un-jobbed: kill it rather than run unconfined.
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:
                    pass
                return _tag(subprocess.CompletedProcess(
                    cmd, 1, "", f"containment attach failed; refused to run unconfined: {err}"),
                    c.mechanism, c.level, launch_error=True)

        sink: dict = {}
        t_out = threading.Thread(target=_drain, args=(proc.stdout, OUTPUT_CAP_BYTES, sink, "out"),
                                 daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, OUTPUT_CAP_BYTES, sink, "err"),
                                 daemon=True)
        t_out.start()
        t_err.start()
        timed_out = False
        try:
            proc.wait(timeout=limits.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if c.kill_tree:
                c.kill_tree(proc)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        t_out.join(timeout=10)
        t_err.join(timeout=10)
        out = _decode(sink.get("out", b""))
        err = _decode(sink.get("err", b""))
        truncated = {"stdout": sink.get("out_dropped", 0), "stderr": sink.get("err_dropped", 0)}
        if truncated["stdout"]:
            out += f"\n[flexfactor-sandbox] stdout truncated: {truncated['stdout']} bytes dropped\n"
        if truncated["stderr"]:
            err += f"\n[flexfactor-sandbox] stderr truncated: {truncated['stderr']} bytes dropped\n"
        if timed_out:
            err += f"\ntimed out after {limits.timeout_s}s; process tree killed ({c.mechanism})"
            return _tag(subprocess.CompletedProcess(cmd, 124, out, err), c.mechanism, c.level,
                        launch_error=True, truncated=truncated)
        return _tag(subprocess.CompletedProcess(cmd, proc.returncode, out, err),
                    c.mechanism, c.level, truncated=truncated)
    finally:
        try:
            c.cleanup()
        except Exception:
            pass


def spawn_contained(cmd: list[str], cwd: str, *, limits: Limits, env: dict | None = None
                    ) -> tuple[subprocess.Popen | None, str, Callable[[], None]]:
    """Start a long-running process (dev server) under containment.
    Returns (proc, error, kill_tree). Output is discarded (a server must never
    fill a pipe and wedge the audit)."""
    try:
        c = prepare(cmd, cwd, env, limits)
    except Exception as ex:
        return None, f"containment prepare failed: {type(ex).__name__}: {ex}", lambda: None
    try:
        proc = subprocess.Popen(c.argv, cwd=c.cwd, env=c.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **c.popen_kwargs)
    except Exception as ex:
        c.cleanup()
        return None, f"could not start {(cmd or ['?'])[0]}: {type(ex).__name__}: {ex}", lambda: None
    if c.attach is not None:
        err = c.attach(proc)
        if err:
            try:
                proc.kill()
            except OSError:
                pass
            c.cleanup()
            return None, f"containment attach failed; refused to run unconfined: {err}", lambda: None
    proc.flexfactor_containment = {"mechanism": c.mechanism, "level": c.level, "applied": True}

    def kill_tree() -> None:
        if c.kill_tree:
            c.kill_tree(proc)
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        c.cleanup()

    return proc, "", kill_tree


# ---------------------------------------------------------------------------
# Gate: OS sandbox OR owner trust
# ---------------------------------------------------------------------------


def os_sandbox_sufficient(rep: dict | None = None) -> bool:
    rep = rep or capability_report()
    return (rep["process_tree"] == "os-enforced" and rep["memory"] == "os-enforced"
            and rep["network_isolation"] == "os-enforced")


def require_containment_or_trust(project_dir: str, *, trust_decision) -> dict:
    """Allowed when the host gives OS-enforced process+memory containment AND
    OS-enforced network isolation; otherwise only an owner trust decision
    (flexfactor_trust.TrustDecision.allowed) authorizes running third-party
    code. Raises ContainmentUnavailable naming what is missing."""
    rep = capability_report()
    if os_sandbox_sufficient(rep):
        return {"allowed": True, "basis": "os-sandbox", "claim": rep["claim"], "report": rep}
    allowed = bool(getattr(trust_decision, "allowed", False))
    if allowed:
        reason = getattr(trust_decision, "reason", "")
        return {"allowed": True, "basis": "trusted-repo",
                "claim": rep["claim"] + f" Execution authorized by owner trust: {reason}",
                "report": rep}
    missing = [k for k in ("process_tree", "memory", "network_isolation")
               if rep[k] != "os-enforced"]
    raise ContainmentUnavailable(
        f"refusing to run third-party install/build/test for {project_dir}: no OS sandbox "
        f"on this host (missing OS enforcement of: {', '.join(missing)}; strongest mechanism: "
        f"{rep['strongest'] or 'none'}) and the repository is not trusted"
        f" ({getattr(trust_decision, 'reason', 'no trust decision')}). Authorize it by adding "
        f"the path to FLEXFACTOR_TRUSTED_REPOS or to ~/.flexfactor/policy.json "
        f"\"trusted_repos\".")


if __name__ == "__main__":
    import json
    print(json.dumps(capability_report(), indent=2))
