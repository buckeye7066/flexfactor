#!/usr/bin/env python3
r"""
FlexFactor web dashboard - the phone-reachable port of flexfactor_dashboard_v2.

Why this exists: the v2 dashboard is Tkinter, so it can only ever be watched
from the desk it runs on. A multi-hour audit is exactly the thing you want to
check from another room, so this serves the SAME seven questions over HTTP to
anything with a browser (in practice: the Android client apps).

It answers the identical questions, in the identical scopes, and inherits the
v2 design rules verbatim - see flexfactor_dashboard_v2's docstring:

  1. Is it alive, or has it wedged?           -> live/stalled, WITH the reason
  2. How far through the WHOLE program?       -> program bar (never the batch's)
  3. What has DURABLY landed?                 -> commits on the branch
  4. How many attempts did this take?         -> survives restarts
  5. What is it doing right now, for how long? -> current file + time on it
  6. Is anything going wrong?                 -> errors/timeouts, called out
  7. What is it costing?                      -> spend vs cap

READ-MOSTLY: status remains observational, while one authenticated endpoint queues
operator steering comments. It reads status.json, the run checkpoints
and `git log`. Steering is append-only and consumed at audit phase boundaries; it
never mutates target code directly. Auth and steering journals live outside the target.

    python flexfactor_web.py                     # 127.0.0.1:8765, local only
    python flexfactor_web.py --host 100.95.159.8 # reachable over the tailnet
    python flexfactor_web.py --print-url         # show the tokenised URL

Pure stdlib. No dependencies.
"""
from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

import flexfactor_steering as steering
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FLEX_DIR = os.path.join(os.path.expanduser("~"), ".flexfactor")
STATUS_PATH = os.path.join(FLEX_DIR, "status.json")
TOKEN_PATH = os.path.join(FLEX_DIR, "web-token.txt")
ACCESS_LOG = os.path.join(FLEX_DIR, "web-access.log")
PHONE_RUN_DIR = os.path.join(os.path.expanduser("~"), ".phone-console")
AUDIT_PID_PATH = os.path.join(PHONE_RUN_DIR, "audit.pid")
AUDIT_LOG_PATH = os.path.join(PHONE_RUN_DIR, "flexfactor-audit.log")
AUDIT_LOCK_PATH = os.path.join(PHONE_RUN_DIR, "audit.lock")
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
LAUNCH_MODES = {"audit", "prodready"}
LAUNCH_PROVIDERS = {"anthropic", "openai", "ollama"}
LAUNCH_LOCK = threading.Lock()

# Inherited from flexfactor_dashboard_v2: quiet longer than this is worth
# SAYING, but it is explicitly NOT death - long fix loops legitimately go
# silent for 20+ minutes while the author model works.
STALL_S = 20 * 60

SAMPLE_EVERY_S = 5.0      # velocity sampling tick (independent of any client)
HIST_MAX = 120            # ~10 min of history at the tick above
DURABLE_TTL_S = 30.0      # durable_facts shells out to git; do not do it per-request

SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _load_dashboard_module():
    """Reuse v2's read_status/durable_facts/human_eta rather than forking them.

    v2 binds STATUS_PATH from sys.argv[1] AT IMPORT TIME, so importing it from a
    process that has its own CLI arguments would silently repoint it at, say,
    "--host". We neuter argv across the import. tkinter is imported inside v2's
    main() only, so importing the module headless is safe.
    """
    saved = sys.argv
    sys.argv = [saved[0] if saved else "flexfactor_web"]
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import flexfactor_dashboard_v2 as dash  # noqa: E402
        return dash
    finally:
        sys.argv = saved


dash = _load_dashboard_module()


# ---------------------------------------------------------------- auth

def load_or_create_token() -> str:
    """A stable token so the phone apps do not need re-pairing on restart."""
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(FLEX_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(tok + "\n")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


# ------------------------------------------------- velocity + liveness

class Sampler:
    """Background poller building the velocity series and the liveness verdict.

    Sampling on a fixed tick (not per HTTP request) is load-bearing: sampling
    per-request would make the sparkline a picture of the CLIENT's poll rate
    instead of the audit's throughput, and two phones watching at once would
    each distort the other's graph.
    """

    def __init__(self, status_path: str) -> None:
        self.status_path = status_path
        self._lock = threading.Lock()
        # program -> [(t, fix_done)]. fix_done, NOT reviewed: fix_done/fix_total
        # is the PROGRAM scope in v2, and the velocity graph and the ETA must be
        # driven by the same counter as the progress bar they sit under.
        self._hist: dict[str, list[tuple[float, int]]] = {}
        self._moved_at: dict[str, float] = {}          # program -> last counter move
        self._durable: dict[str, tuple[float, dict]] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, name="ff-web-sampler", daemon=True)
        t.start()

    def _loop(self) -> None:
        while not self._stop.wait(SAMPLE_EVERY_S):
            try:
                self._sample()
            except Exception:
                # A sampler crash must never take the dashboard down; the next
                # tick retries and the UI degrades to "no velocity yet".
                pass

    def _sample(self) -> None:
        """Mirrors v2's history rule exactly: record on every CHANGE, plus a
        keepalive point every 20s so a wedged run draws a visibly flat line
        rather than simply stopping (a frozen graph and an idle graph must not
        look the same)."""
        progs, _mt = dash.read_status(self.status_path)
        now = time.time()
        with self._lock:
            for p in progs:
                name = str(p.get("name") or "?")
                fix_done = int(p.get("fix_done") or 0)
                h = self._hist.setdefault(name, [])
                if not h or h[-1][1] != fix_done:
                    h.append((now, fix_done))
                    self._moved_at[name] = now
                elif now - h[-1][0] > 20:
                    h.append((now, fix_done))
                if len(h) > HIST_MAX:
                    del h[: len(h) - HIST_MAX]

    def velocity(self, name: str) -> list[float]:
        """Per-interval files/min, the series v2 sparklines."""
        with self._lock:
            h = list(self._hist.get(name, []))
        out = []
        for j in range(1, len(h)):
            dt = max(1e-6, (h[j][0] - h[j - 1][0]) / 60.0)
            out.append(max(0.0, (h[j][1] - h[j - 1][1]) / dt))
        return out

    def counters_moved_ago(self, name: str) -> float | None:
        with self._lock:
            t = self._moved_at.get(name)
        return (time.time() - t) if t else None

    def durable(self, p: dict) -> dict:
        """Cached durable_facts - it runs `git log`, too slow for every request."""
        name = str(p.get("name") or "?")
        now = time.time()
        with self._lock:
            hit = self._durable.get(name)
            if hit and (now - hit[0]) < DURABLE_TTL_S:
                return hit[1]
        facts = dash.durable_facts(p)
        with self._lock:
            self._durable[name] = (now, facts)
        return facts

    def rate_per_min(self, name: str) -> float:
        """Throughput across the whole retained window, as v2 computes it.

        Deliberately NOT a short tail average: fixes land in bursts, and a tail
        window reads 0 during any normal gap between them, which would render a
        healthy run as stalled and an ETA as infinite.
        """
        with self._lock:
            h = list(self._hist.get(name, []))
        if len(h) < 2:
            return 0.0
        dt = (h[-1][0] - h[0][0]) / 60.0
        if dt <= 0.2:                 # too short a baseline to divide by
            return 0.0
        return max(0.0, (h[-1][1] - h[0][1]) / dt)


# ------------------------------------------------------------ payload

# The per-program error ledger, read straight off disk. Kept small on
# purpose: the phone gets the newest few entries plus the counts, and the run
# directory holds the rest - shipping 148 entries over a phone link to render
# three of them would be waste, and re-formatting them here would be a second
# implementation to drift from the first.
_LEDGER_ROWS = 3


def _ledger_view(p: dict) -> dict:
    try:
        import flexfactor_errors as fe
    except Exception:  # noqa: BLE001 - viewer feature, never a 500
        return {"available": False, "total": 0, "headline": "", "rows": []}
    try:
        run_dir = str(p.get("run_dir") or "")
        if not run_dir or not os.path.isdir(run_dir):
            run_dir = fe.find_run_dir(str(p.get("name") or ""))
        entries = fe.load_entries(run_dir)
        return {"available": True, "total": len(entries),
                "headline": fe.headline(entries),
                "rows": fe.ui_entries(entries, _LEDGER_ROWS)}
    except Exception:  # noqa: BLE001
        return {"available": False, "total": 0, "headline": "", "rows": []}


def build_state(sampler: Sampler) -> dict:
    progs, mtime = dash.read_status(sampler.status_path)
    now = time.time()
    quiet_s = (now - mtime) if mtime else None

    out = []
    for p in progs:
        name = str(p.get("name") or "?")
        # SCOPES, and they are NOT interchangeable (v2 design rule #1 - showing
        # one where the other belongs is the misread that made a 0.6%-complete
        # program read as "100% reviewed"):
        #   PROGRAM scope = fix_done / fix_total
        #   BATCH   scope = reviewed / files_total   (files_total IS the batch)
        fix_done = int(p.get("fix_done") or 0)
        fix_total = int(p.get("fix_total") or 0)
        reviewed = int(p.get("reviewed") or 0)
        batch_n = int(p.get("files_total") or 0)
        rate = sampler.rate_per_min(name)
        moved_ago = sampler.counters_moved_ago(name)
        facts = sampler.durable(p)

        sev_raw = p.get("severity") or {}
        severity = {}
        if isinstance(sev_raw, dict):
            for k in SEV_ORDER:
                v = int(sev_raw.get(k) or 0)
                if v:
                    severity[k] = v

        # Liveness is deliberately TWO signals reported separately. v2's design
        # rule: a quiet status file is not death, so never collapse these into a
        # single boolean the reader will mistake for "dead".
        if p.get("done"):
            live = "done"
        elif quiet_s is not None and quiet_s > STALL_S:
            live = "quiet"
        else:
            live = "live"

        out.append({
            "name": name,
            "dir": p.get("dir") or "",
            "phase": p.get("phase") or "",
            "done": bool(p.get("done")),
            "liveness": live,
            # both reasons, so the UI can say WHICH signal is quiet
            "status_quiet_s": round(quiet_s) if quiet_s is not None else None,
            "counters_quiet_s": round(moved_ago) if moved_ago is not None else None,
            "stall_threshold_s": STALL_S,

            # 2. PROGRAM scope - the honest denominator
            "fix_done": fix_done,
            "fix_total": fix_total,
            "program_pct": (round(100.0 * fix_done / fix_total, 1)
                            if fix_total else None),
            "eta": dash.human_eta(fix_done, fix_total, rate),

            # 3. BATCH scope - named, so it can never be misread as the program
            "reviewed": reviewed,
            "batch_n": batch_n,

            # 4. defects, scope-qualified
            "defects": int(p.get("defects") or 0),
            "defects_fixed": int(p.get("defects_fixed") or 0),
            "fixed": int(p.get("fixed") or 0),
            "errors": int(p.get("errors") or 0),
            "severity": severity,

            # 5. durable - the only numbers a restart cannot fake
            "attempts": facts.get("attempts") or 0,
            "resumes": facts.get("resumes") or 0,
            "landed": facts.get("landed"),

            # 6. cost
            "cost": float(p.get("cost") or 0.0),
            "cap": float(p.get("cap") or 0.0),

            # 6b. THE ERROR BOX, phone edition (owner 2026-08-23). Same
            # reader as the desktop dashboard - flexfactor_errors owns both, so
            # the box on the phone can never say something different from the
            # box at the desk or from errors.md itself.
            "ledger": _ledger_view(p),

            # 7. what it is doing right now
            "current_file": p.get("current_file") or "",
            "cycles": p.get("cycles"),

            # Exact final-run proof: repository summary, purpose map, gates,
            # function/workflow execution, blast radius, and trace artifacts.
            "evidence": p.get("evidence") if isinstance(p.get("evidence"), dict) else {},

            "rate_per_min": round(rate, 2),
            "velocity": [round(v, 2) for v in sampler.velocity(name)],
            "steering": steering.summary(name, str(p.get("dir") or "")),
        })

    return {
        "now": now,
        "status_mtime": mtime or None,
        "status_quiet_s": round(quiet_s) if quiet_s is not None else None,
        "programs": out,
        "host": _host_label(),
        "launch": phone_launch_state(),
    }


def _host_label(env=None):
    """Return an honest, user-facing label for the machine doing the work.

    Android's Termux environment normally has no ``COMPUTERNAME``. Falling
    straight back to ``pc`` made a fully local phone engine look as if it were
    still connected to a laptop. An explicit label wins for unusual hosts;
    otherwise the Termux markers are checked before desktop host names.
    """
    env = os.environ if env is None else env
    explicit = str(env.get("FLEXFACTOR_HOST_LABEL") or "").strip()
    if explicit:
        return explicit
    prefix = str(env.get("PREFIX") or "")
    if env.get("TERMUX_VERSION") or prefix.startswith("/data/data/com.termux/"):
        return "this phone"
    return str(env.get("COMPUTERNAME") or env.get("HOSTNAME") or "pc")


# --------------------------------------------------------- phone launcher

def _is_phone_environment(env=None) -> bool:
    env = os.environ if env is None else env
    prefix = str(env.get("PREFIX") or "")
    return bool(env.get("TERMUX_VERSION")) or prefix.startswith("/data/data/com.termux/")


def _path_within_root(root: str, path: str) -> bool:
    try:
        return (os.path.normcase(os.path.commonpath([root, path])) ==
                os.path.normcase(root))
    except ValueError:
        return False


def _available_phone_programs(env=None) -> list[dict]:
    """Return exact, already-cloned git repositories the phone may launch.

    Only configured roots and their immediate children are considered. The
    endpoint later matches a canonical path against this result, so a caller
    cannot use ``..`` or an arbitrary path to escape the owner's project roots.
    """
    env = os.environ if env is None else env
    configured = str(env.get("FLEXFACTOR_PROJECT_ROOTS") or "").strip()
    roots = configured.split(os.pathsep) if configured else [
        os.path.join(os.path.expanduser("~"), "phone-console"),
        os.path.expanduser("~"),
    ]
    found: dict[str, dict] = {}
    for raw_root in roots:
        raw_root = raw_root.strip()
        if not raw_root:
            continue
        root = os.path.realpath(os.path.expanduser(raw_root))
        if not os.path.isdir(root):
            continue
        candidates = [root]
        try:
            candidates.extend(os.path.join(root, name) for name in os.listdir(root))
        except OSError:
            pass
        for candidate in candidates:
            path = os.path.realpath(candidate)
            if not _path_within_root(root, path):
                continue
            if not os.path.isdir(path) or not os.path.exists(os.path.join(path, ".git")):
                continue
            found[path] = {"name": os.path.basename(path) or path, "path": path}
    return sorted(found.values(), key=lambda item: (item["name"].lower(), item["path"]))


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ollama_reachable(env=None) -> bool:
    env = os.environ if env is None else env
    base = str(env.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    if not (base.startswith("http://127.0.0.1:") or base.startswith("http://localhost:")):
        return False
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=0.25) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - a readiness probe must fail closed
        return False


def _provider_readiness(env=None, module_available=None,
                        ollama_reachable=None) -> list[dict]:
    """Report capability booleans and guidance, never credential values."""
    env = os.environ if env is None else env
    module_available = module_available or _module_available
    ollama_reachable = ollama_reachable or (lambda: _ollama_reachable(env))
    specs = [
        ("openai", bool(str(env.get("OPENAI_API_KEY") or "").strip()),
         module_available("openai"), "OPENAI_API_KEY"),
        ("anthropic", bool(str(env.get("ANTHROPIC_API_KEY") or
                               env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()),
         module_available("anthropic"), "ANTHROPIC_API_KEY"),
    ]
    out = []
    for name, has_key, has_sdk, key_name in specs:
        missing = []
        if not has_key:
            missing.append(key_name)
        if not has_sdk:
            missing.append(name + " SDK")
        out.append({"name": name, "ready": not missing,
                    "detail": "ready" if not missing else "missing " + " and ".join(missing)})
    ollama_ok = bool(ollama_reachable())
    out.append({"name": "ollama", "ready": ollama_ok,
                "detail": "ready" if ollama_ok else "local Ollama is not answering"})
    return out


def _running_audit_pid(pid_path=AUDIT_PID_PATH) -> int | None:
    try:
        with open(pid_path, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        if pid <= 0:
            return None
        os.kill(pid, 0)
        return pid
    except (OSError, TypeError, ValueError):
        return None


def _acquire_audit_start_lock(lock_path: str, pid_path: str) -> None:
    """Atomically serialize dashboard and shell starts across processes."""
    for _ in range(2):
        try:
            os.mkdir(lock_path)
            try:
                with open(os.path.join(lock_path, "owner.pid"), "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()) + "\n")
            except OSError:
                try:
                    os.rmdir(lock_path)
                except OSError:
                    pass
                raise
            return
        except FileExistsError:
            running = _running_audit_pid(pid_path)
            if running:
                raise ValueError("an audit is already running (pid {})".format(running))
            owner_path = os.path.join(lock_path, "owner.pid")
            owner = _running_audit_pid(owner_path)
            try:
                old = time.time() - os.path.getmtime(lock_path) > 30
            except OSError:
                old = False
            if owner or not old:
                raise ValueError("another FlexFactor launch is already starting")
            try:
                os.unlink(owner_path)
            except OSError:
                pass
            try:
                os.rmdir(lock_path)
            except OSError:
                raise ValueError("another FlexFactor launch is already starting") from None
    raise ValueError("another FlexFactor launch is already starting")


def _release_audit_start_lock(lock_path: str) -> None:
    try:
        os.unlink(os.path.join(lock_path, "owner.pid"))
    except OSError:
        pass
    try:
        os.rmdir(lock_path)
    except OSError:
        pass


def _start_audit_reaper(process, pid_path: str) -> None:
    """Reap the detached child and clear only its own PID record."""
    def reap() -> None:
        try:
            process.wait()
        finally:
            with LAUNCH_LOCK:
                try:
                    with open(pid_path, "r", encoding="utf-8") as fh:
                        recorded = int(fh.read().strip())
                    if recorded == process.pid:
                        os.unlink(pid_path)
                except (OSError, TypeError, ValueError):
                    pass

    threading.Thread(target=reap, name="flexfactor-audit-reaper", daemon=True).start()


def phone_launch_state(env=None) -> dict:
    env = os.environ if env is None else env
    phone = _is_phone_environment(env)
    return {
        "available": phone,
        "programs": _available_phone_programs(env) if phone else [],
        "providers": _provider_readiness(env) if phone else [],
        "running_pid": _running_audit_pid() if phone else None,
        "default_max_cost": 10,
        "policy": "Changes stay on this phone; launch never pushes or merges.",
    }


def start_phone_run(body: dict, *, env=None, programs=None, readiness=None,
                    pid_path=AUDIT_PID_PATH, log_path=AUDIT_LOG_PATH,
                    lock_path=AUDIT_LOCK_PATH, popen=subprocess.Popen,
                    start_reaper=_start_audit_reaper) -> dict:
    """Validate and spawn one detached phone run without invoking a shell."""
    env = os.environ if env is None else env
    if not _is_phone_environment(env):
        raise ValueError("runs can only be started from the on-phone engine")
    mode = str(body.get("mode") or "")
    provider = str(body.get("provider") or "")
    if mode not in LAUNCH_MODES:
        raise ValueError("mode must be audit or prodready")
    if provider not in LAUNCH_PROVIDERS:
        raise ValueError("provider is not allowed")
    try:
        max_cost = float(body.get("max_cost", 10))
    except (TypeError, ValueError):
        raise ValueError("cost cap must be a number") from None
    if not math.isfinite(max_cost) or max_cost < 1 or max_cost > 150:
        raise ValueError("cost cap must be between 1 and 150 USD")

    programs = _available_phone_programs(env) if programs is None else programs
    requested = os.path.realpath(str(body.get("program") or ""))
    match = next((item for item in programs
                  if os.path.realpath(str(item.get("path") or "")) == requested), None)
    if match is None:
        raise ValueError("program is not an allowed repository on this phone")
    readiness = _provider_readiness(env) if readiness is None else readiness
    provider_state = next((item for item in readiness if item.get("name") == provider), None)
    if not provider_state or not provider_state.get("ready"):
        detail = str((provider_state or {}).get("detail") or "not configured")
        raise ValueError(provider + " is not ready: " + detail)

    with LAUNCH_LOCK:
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        _acquire_audit_start_lock(lock_path, pid_path)
        try:
            running = _running_audit_pid(pid_path)
            if running:
                raise ValueError("an audit is already running (pid {})".format(running))
            model_mode = "free" if provider == "ollama" else "paid"
            command = [
                sys.executable, os.path.join(APP_ROOT, "flexfactor.py"), mode,
                "--program", requested, "--no-dashboard", "--provider", provider,
                "--model-mode", model_mode, "--single",
                "--max-cost", "{:g}".format(max_cost),
                "--no-push", "--no-merge", "--no-auto-clean",
            ]
            child_env = dict(env)
            child_env["FLEXFACTOR_HOST_LABEL"] = "this phone"
            with open(log_path, "a", encoding="utf-8") as log:
                log.write("\n--- {} app launch: {} {} (max ${:g}) ---\n".format(
                    time.strftime("%Y-%m-%dT%H:%M:%S%z"), mode, requested, max_cost))
                log.flush()
                process = popen(
                    command, cwd=APP_ROOT, env=child_env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    close_fds=True,
                )
            temporary = pid_path + ".tmp-{}".format(os.getpid())
            try:
                with open(temporary, "w", encoding="utf-8") as fh:
                    fh.write(str(process.pid) + "\n")
                os.replace(temporary, pid_path)
            except OSError:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:  # noqa: BLE001 - retain the original filesystem failure
                    pass
                raise
            start_reaper(process, pid_path)
        finally:
            _release_audit_start_lock(lock_path)
    return {"ok": True, "pid": process.pid, "program": match["name"],
            "mode": mode, "provider": provider, "max_cost": max_cost}


# --------------------------------------------------------------- HTTP

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>FlexFactor</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#0b0f14;color:#e6edf3;
 font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 padding:env(safe-area-inset-top) 12px calc(24px + env(safe-area-inset-bottom))}
h1{font-size:17px;margin:14px 0 4px;letter-spacing:.3px}
.sub{color:#7d8590;font-size:12px;margin-bottom:14px}
.card{background:#131a22;border:1px solid #243040;border-radius:14px;
 padding:14px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.name{font-size:17px;font-weight:600}
.pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;
 text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.live{background:#12331c;color:#3fb950}.quiet{background:#3a2d0c;color:#d29922}
.done{background:#16304d;color:#58a6ff}
.lbl{color:#7d8590;font-size:11px;text-transform:uppercase;letter-spacing:.6px;
 margin:14px 0 5px}
.bar{height:9px;background:#1c2530;border-radius:999px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:999px;transition:width .4s}
.n{font-variant-numeric:tabular-nums}
.big{font-size:22px;font-weight:650}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:6px}
.dim{color:#7d8590;font-size:12px}
.file{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
 color:#58a6ff;word-break:break-all}
svg{display:block;width:100%;height:38px}
.sev{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.sev span{font-size:11px;padding:2px 8px;border-radius:999px;background:#1c2530}
.err{color:#f85149;font-weight:600}
.errhead{color:#ff7b72;font-weight:600;font-size:12px;margin:2px 0 6px}
.errrow{background:#1a1113;border:1px solid #30363d;border-radius:6px;
        padding:6px 8px;margin-bottom:6px}
.errkind{font-weight:600;font-size:12px;margin-bottom:2px}
.errmsg{font-family:ui-monospace,Consolas,monospace;font-size:12px;
        color:#c9d1d9;word-break:break-word}
.errfix{color:#7ee787;font-size:12px;margin-top:2px}
.k-flexfactor-defect{color:#f85149}
.k-program-defect{color:#ff7b72}
.k-provider,.k-budget{color:#d29922}
.k-environment{color:#58a6ff}
.k-unknown{color:#8b949e}
.ok{color:#3fb950;font-weight:600}
.warnbox{background:#3a1d1d;border:1px solid #f85149;color:#ffb4ae;
 padding:9px 11px;border-radius:10px;font-size:12px;margin-top:10px}
.empty{text-align:center;color:#7d8590;padding:44px 12px}
.steer{width:100%;min-height:86px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:9px;font:14px/1.4 inherit}
.steerbtn{margin-top:7px;background:#238636;color:white;border:0;border-radius:7px;padding:8px 12px;font-weight:650}
.steerbtn:disabled{opacity:.55}.steerrow{border-left:3px solid #30363d;padding:5px 8px;margin-top:6px}
.s-pending{border-color:#d29922}.s-active{border-color:#58a6ff}.s-completed{border-color:#3fb950}.s-needs-attention{border-color:#f85149}
.launcher select,.launcher input{width:100%;background:#0d1117;color:#e6edf3;
 border:1px solid #30363d;border-radius:8px;padding:10px;margin:4px 0 8px;font:14px inherit}
.launchbtn{width:100%;background:#238636;color:white;border:0;border-radius:8px;
 padding:11px 12px;font-weight:700;margin-top:6px}.launchbtn:disabled{opacity:.45}
</style></head><body>
<h1>FlexFactor</h1>
<div class="sub" id="sub">connecting...</div>
<div id="app"></div>
<script>
var TOKEN = new URLSearchParams(location.search).get("t") || "";
var STEER_DRAFTS = {};
var LAUNCH_DRAFT = {program:"",mode:"audit",provider:"openai",max_cost:10};
function launchHtml(l){
  if(!l||!l.available) return "";
  var providers=l.providers||[], programs=l.programs||[], running=l.running_pid;
  if(!LAUNCH_DRAFT.program&&programs.length) LAUNCH_DRAFT.program=programs[0].path;
  var selectedProvider=providers.find(function(p){return p.name===LAUNCH_DRAFT.provider;});
  if(!selectedProvider||!selectedProvider.ready){
    var first=providers.find(function(p){return p.ready;});
    if(first) LAUNCH_DRAFT.provider=first.name;
  }
  selectedProvider=providers.find(function(p){return p.name===LAUNCH_DRAFT.provider;})||{};
  var disabled=running||!programs.length||!selectedProvider.ready;
  var h='<div class="card launcher"><div class="row"><div class="name">Start on this phone</div>'+
    (running?'<div class="pill live">running</div>':'')+'</div>'+
    '<div class="dim">Choose a repository already downloaded to this phone. '+esc(l.policy||'')+'</div>'+
    '<div class="lbl">Repository</div><select onchange="LAUNCH_DRAFT.program=this.value">';
  programs.forEach(function(p){h+='<option value="'+esc(p.path)+'" '+(p.path===LAUNCH_DRAFT.program?'selected':'')+'>'+esc(p.name)+'</option>';});
  h+='</select><div class="lbl">Run</div><select onchange="LAUNCH_DRAFT.mode=this.value">'+
    '<option value="audit" '+(LAUNCH_DRAFT.mode==='audit'?'selected':'')+'>Audit and fix</option>'+
    '<option value="prodready" '+(LAUNCH_DRAFT.mode==='prodready'?'selected':'')+'>Production readiness</option></select>'+
    '<div class="lbl">Provider</div><select onchange="LAUNCH_DRAFT.provider=this.value;tick()">';
  providers.forEach(function(p){h+='<option value="'+esc(p.name)+'" '+(p.name===LAUNCH_DRAFT.provider?'selected':'')+'>'+esc(p.name)+' — '+esc(p.detail)+'</option>';});
  h+='</select><div class="lbl">Maximum provider cost (USD)</div><input type="number" min="1" max="150" step="1" value="'+esc(LAUNCH_DRAFT.max_cost)+'" onchange="LAUNCH_DRAFT.max_cost=this.value">'+
    '<div class="dim">This run may edit the selected repository locally. It cannot push or merge.</div>'+
    (running?'<div class="dim" style="margin-top:8px">Audit process '+running+' is already running.</div>':'')+
    (!programs.length?'<div class="warnbox">No Git repositories were found in the phone project roots.</div>':'')+
    (!selectedProvider.ready?'<div class="warnbox">Configure a provider in Termux first: '+esc(selectedProvider.detail||'none is ready')+'.</div>':'')+
    '<button class="launchbtn" '+(disabled?'disabled':'')+' onclick="submitLaunch(this)">Start FlexFactor</button></div>';
  return h;
}
function submitLaunch(button){
  var name=(LAUNCH_DRAFT.program.split('/').pop()||LAUNCH_DRAFT.program);
  if(!confirm('Start FlexFactor on '+name+'? It may edit that repository locally, but will not push or merge.')) return;
  button.disabled=true;button.textContent='Starting…';
  fetch('/api/launch?t='+encodeURIComponent(TOKEN),{method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+TOKEN},
    body:JSON.stringify(LAUNCH_DRAFT)})
   .then(function(r){return r.json().then(function(d){if(!r.ok) throw new Error(d.error||('HTTP '+r.status));return d;});})
   .then(function(d){alert('FlexFactor '+d.mode+' started on '+d.program+'.');tick();})
   .catch(function(e){alert('Could not start FlexFactor: '+e.message);})
   .finally(function(){button.disabled=false;button.textContent='Start FlexFactor';});
}
function steerDraft(name,value){STEER_DRAFTS[name]=value;}
function submitSteering(name,dir,button){
  var text=STEER_DRAFTS[name]||""; if(!text.trim()) return;
  button.disabled=true; button.textContent="Sending…";
  fetch("/api/steering?t="+encodeURIComponent(TOKEN),{method:"POST",
    headers:{"Content-Type":"application/json","Authorization":"Bearer "+TOKEN},
    body:JSON.stringify({program:name,project_dir:dir,comment:text})})
   .then(function(r){return r.json().then(function(d){if(!r.ok) throw new Error(d.error||("HTTP "+r.status));return d;});})
   .then(function(){STEER_DRAFTS[name]="";tick();})
   .catch(function(e){alert("Could not send comment: "+e.message);})
   .finally(function(){button.disabled=false;button.textContent="Send to FlexFactor";});
}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function dur(s){ if(s==null) return "?";
  if(s<90) return Math.round(s)+"s";
  if(s<5400) return (s/60).toFixed(0)+"m";
  return (s/3600).toFixed(1)+"h"; }
function bar(frac,color){
  var pct=Math.max(0,Math.min(1,frac||0))*100;
  return '<div class="bar"><i style="width:'+pct.toFixed(1)+'%;background:'+color+'"></i></div>';
}
function spark(v){
  if(!v||v.length<2) return '<div class="dim">collecting velocity…</div>';
  var max=Math.max.apply(null,v)||1, w=100, n=v.length, pts=[];
  for(var i=0;i<n;i++){
    pts.push((i/(n-1)*w).toFixed(2)+","+(34-(v[i]/max)*30).toFixed(2));
  }
  return '<svg viewBox="0 0 100 38" preserveAspectRatio="none">'+
    '<polyline fill="none" stroke="#58a6ff" stroke-width="1.4" '+
    'vector-effect="non-scaling-stroke" points="'+pts.join(" ")+'"/></svg>';
}
function card(p){
  var h="";
  var cls = p.liveness==="done"?"done":(p.liveness==="quiet"?"quiet":"live");
  var lab = p.liveness==="done"?"done":(p.liveness==="quiet"?"quiet":"live");
  h+='<div class="card">';
  h+='<div class="row"><div class="name">'+esc(p.name)+'</div>'+
     '<div class="pill '+cls+'">'+lab+'</div></div>';
  h+='<div class="dim">'+esc(p.phase||"—")+'</div>';

  // 1. liveness, WITH which signal is quiet (never a bare "dead")
  if(p.liveness==="quiet"){
    h+='<div class="warnbox">status.json quiet '+dur(p.status_quiet_s)+
       ' (threshold '+dur(p.stall_threshold_s)+'). Counters last moved '+
       dur(p.counters_quiet_s)+' ago. A long fix loop can legitimately be '+
       'silent this long — this is not proof it died.</div>';
  }

  // 2. PROGRAM scope — fix_done/fix_total, never the batch's numbers
  h+='<div class="lbl">Program — whole program</div>';
  h+='<div class="row"><span class="big n">'+
     (p.program_pct==null?"—":p.program_pct+"%")+'</span>'+
     '<span class="dim n">'+p.fix_done+' / '+p.fix_total+' files</span></div>';
  h+=bar(p.fix_total?p.fix_done/p.fix_total:0,"#58a6ff");
  if(p.eta) h+='<div class="dim" style="margin-top:4px">'+esc(p.eta)+'</div>';

  // velocity — driven by the same counter as the bar above it
  h+='<div class="lbl">Velocity — '+p.rate_per_min+' files/min</div>'+spark(p.velocity);

  // 3. BATCH scope, explicitly named so it cannot be read as the program
  if(p.batch_n){
    h+='<div class="lbl">Current batch only — not the program</div>';
    h+='<div class="row"><span class="n">'+p.reviewed+' / '+p.batch_n+
       ' files reviewed</span></div>';
    h+=bar(p.reviewed/p.batch_n,"#3fb950");
  }

  // 4. defects, scope-qualified against what was actually reviewed
  h+='<div class="lbl">Defects — found in '+p.reviewed+' files reviewed</div>';
  h+='<div class="row"><span class="n">'+p.defects+' found · '+
     p.defects_fixed+' fixed</span>'+
     (p.errors?'<span class="err n">'+p.errors+' errors</span>':'')+'</div>';
  if(Object.keys(p.severity||{}).length){
    h+='<div class="sev">';
    for(var k in p.severity){ h+='<span>'+k+' '+p.severity[k]+'</span>'; }
    h+='</div>';
  }

  // 5. durable
  h+='<div class="lbl">Durable (survives restarts)</div>';
  h+='<div class="grid"><div><div class="big n">'+
     (p.landed==null?"—":p.landed)+'</div><div class="dim">commits landed</div></div>'+
     '<div><div class="big n">'+p.attempts+'</div><div class="dim">attempts'+
     (p.resumes?' · '+p.resumes+' resumes':'')+'</div></div></div>';

  // 6. cost
  h+='<div class="lbl">Cost</div>';
  h+='<div class="row"><span class="n">$'+p.cost.toFixed(2)+'</span>'+
     '<span class="dim n">cap $'+p.cap.toFixed(2)+'</span></div>';
  h+=bar(p.cap?p.cost/p.cap:0, p.cap&&p.cost/p.cap>0.8?"#d29922":"#3fb950");

  // Exact evidence views. Compact on mobile, but every primary proof surface
  // remains named and its denominator stays visible.
  var e=p.evidence||{}, g=e.gates||{}, c=e.coverage||{}, r=e.repository||{},
      im=e.impact||{}, pu=e.purpose||{};
  if(Object.keys(e).length){
    h+='<div class="lbl">Executable evidence</div>'+
       '<div class="row"><span class="n">gates '+(g.pass||0)+' pass · '+
       (g.fail||0)+' fail · '+(g.blocked||0)+' blocked</span>'+
       '<span class="'+(g.passed?'ok':'err')+'">'+(g.passed?'VERIFIED':'INCOMPLETE')+'</span></div>'+
       '<div class="dim">Repo '+(r.files||0)+' files · '+(r.functions||0)+' functions · purpose '+
       (pu.confidence||'unknown')+' ('+(pu.nodes||0)+' nodes)</div>'+
       '<div class="dim">Coverage '+(c.functions_executed||0)+'/'+(c.functions||0)+' functions · '+
       (c.routes_executed||0)+'/'+(c.routes||0)+' routes · '+
       (c.controls_executed||0)+'/'+(c.controls||0)+' controls</div>'+
       '<div class="dim">Impact '+(im.affected_files||0)+' files · '+(im.tests||0)+' tests · commit '+
       esc((e.final_commit||'').slice(0,12)||'—')+'</div>';
  }

  // 6b. errors — what failed, whose code, and the suggested fix
  var L=p.ledger||{};
  h+='<div class="lbl">Errors — this run\u0027s ledger</div>';
  if(!L.available){
    h+='<div class="dim">ledger unavailable</div>';
  }else if(!L.total){
    h+='<div class="dim">no errors recorded — nothing has gone wrong yet</div>';
  }else{
    h+='<div class="errhead">'+esc(L.headline)+'</div>';
    (L.rows||[]).forEach(function(r){
      h+='<div class="errrow"><div class="errkind k-'+esc(r.kind)+'">#'+esc(r.n)+
         ' '+esc(r.kind)+' / '+esc(r.phase)+'</div>'+
         '<div class="errmsg">'+esc(r.error)+'</div>'+
         '<div class="dim">code: '+esc(r.where)+'</div>'+
         '<div class="errfix">fix: '+
         (r.fix_source==="model"?"(unverified) ":"")+
         esc(r.fix||"no known fix")+'</div></div>';
    });
    if(L.total>(L.rows||[]).length){
      h+='<div class="dim">+'+(L.total-(L.rows||[]).length)+
         ' more in the run\u0027s errors.md</div>';
    }
  }

  // 7. authenticated operator steering
  var st=p.steering||{}, latest=st.latest||[];
  h+='<div class="lbl">Steer this build</div><div class="dim">Tell FlexFactor what you want changed in this target app. It will interpret the request and route it through the normal build and test gates.</div>'+
     '<textarea class="steer" maxlength="4000" placeholder="Example: Keep the current login, but add a printable family report." oninput="steerDraft('+JSON.stringify(p.name)+',this.value)">'+esc(STEER_DRAFTS[p.name]||"")+'</textarea>'+
     '<button class="steerbtn" onclick="submitSteering('+JSON.stringify(p.name)+','+JSON.stringify(p.dir)+',this)">Send to FlexFactor</button>';
  latest.forEach(function(s){h+='<div class="steerrow s-'+esc(s.status)+'"><div>'+esc(s.comment)+'</div><div class="dim">'+esc(s.status)+(s.detail?' · '+esc(s.detail):'')+'</div></div>';});
  // 8. right now
  if(p.current_file){
    h+='<div class="lbl">Working on</div><div class="file">'+
       esc(p.current_file)+'</div>';
  }
  h+='</div>';
  return h;
}
function tick(){
  fetch("/api/state?t="+encodeURIComponent(TOKEN),{cache:"no-store"})
   .then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
   .then(function(d){
     document.getElementById("sub").textContent =
       d.host+" · status "+(d.status_quiet_s==null?"never seen":
       "updated "+dur(d.status_quiet_s)+" ago");
     var a=document.getElementById("app");
     var launcher=launchHtml(d.launch);
     if(!d.programs.length){
       a.innerHTML=launcher+'<div class="empty">No active programs.<br>'+
         '<span style="font-size:12px">FlexFactor writes status.json when a run starts.</span></div>';
       return;
     }
     a.innerHTML = launcher+d.programs.map(card).join("");
   })
   .catch(function(e){
     document.getElementById("sub").textContent = "offline — "+e.message;
   });
}
tick(); setInterval(tick, 5000);
document.addEventListener("visibilitychange", function(){
  if(!document.hidden) tick();
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FlexFactorWeb/1.0"
    token = ""
    sampler: Sampler = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):
        """Append peer + request to an access log.

        The stdlib default writes to stderr, which is invisible for a
        windowless background process. Having the peer address on disk is what
        makes "the phone reached the server" an observation rather than an
        inference.
        """
        try:
            line = "{} {} {}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"),
                self.client_address[0],
                (fmt % args) if args else fmt,
            )
            with open(ACCESS_LOG, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # logging must never take the dashboard down

    def _authorized(self) -> bool:
        supplied = ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            _, _, qs = self.path.partition("?")
            for part in qs.split("&"):
                k, _, v = part.partition("=")
                if k == "t":
                    from urllib.parse import unquote
                    supplied = unquote(v)
                    break
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # phone navigated away mid-response; not an error worth noise

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            self._send(401, b'{"error":"bad or missing token"}', "application/json")
            return
        if path not in ("/api/steering", "/api/launch"):
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            self._send(413, b'{"error":"invalid request size"}', "application/json")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            if path == "/api/launch":
                result = start_phone_run(body)
                self._send(201, json.dumps(result).encode("utf-8"), "application/json")
                return
            program = str(body.get("program") or "")
            project_dir = str(body.get("project_dir") or "")
            active = build_state(self.sampler).get("programs") or []
            match = next((p for p in active if p.get("name") == program and p.get("dir") == project_dir), None)
            if match is None:
                raise ValueError("target is not an active FlexFactor program")
            item = steering.submit(program, project_dir, body.get("comment") or "", source="web-dashboard")
            self._send(201, json.dumps({"ok": True, "comment": item}).encode("utf-8"), "application/json")
        except (TypeError, ValueError, UnicodeError) as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode("utf-8"), "application/json")
        except OSError:
            self._send(500, b'{"error":"the phone could not start the FlexFactor process"}',
                       "application/json")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            # Deliberately unauthenticated and contentless: lets the phone app
            # distinguish "server down" from "wrong token" without leaking state.
            self._send(200, b'{"ok":true}', "application/json")
            return

        if not self._authorized():
            self._send(401, b'{"error":"bad or missing token"}', "application/json")
            return

        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/state":
            payload = json.dumps(build_state(self.sampler)).encode("utf-8")
            self._send(200, payload, "application/json")
            return

        self._send(404, b'{"error":"not found"}', "application/json")


def main() -> int:
    ap = argparse.ArgumentParser(description="FlexFactor web dashboard with authenticated steering")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use the Tailscale IP to reach phones")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--status", default=STATUS_PATH)
    ap.add_argument("--print-url", action="store_true",
                    help="print the tokenised URL and exit")
    args = ap.parse_args()

    token = load_or_create_token()
    url = "http://{}:{}/?t={}".format(args.host, args.port, token)
    if args.print_url:
        print(url)
        return 0

    sampler = Sampler(args.status)
    sampler.start()

    Handler.token = token
    Handler.sampler = sampler

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("[flexfactor-web] serving {}".format(url))
    print("[flexfactor-web] authenticated steering enabled; token in {}".format(TOKEN_PATH))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[flexfactor-web] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
