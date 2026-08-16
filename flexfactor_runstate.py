#!/usr/bin/env python3
"""Durable per-run checkpoint state so an interrupted FlexFactor run can be
RESUMED instead of restarted from zero.

WHY THIS EXISTS
---------------
2026-08-11: a `prodready` run on GrantFlow was killed after 7h46m at
$25.47 / $50, 858 of 3,206 files reviewed, 2,469 defects found, 498 files
fixed, cycle 1 of 12. There was no way to continue it. Every restart re-detects
the toolchain, re-installs dependencies, re-reviews all 3,206 files, and re-pays
for the review it already bought.

The tool's only pre-existing continuity mechanism was `brain.json`'s
`clean_files` skip set, and that file is subject to a 40-project LRU cap: a test
run with an unredirected BRAIN_PATH evicted every real project the same day,
permanently destroying those skip sets. So resume state deliberately lives
SOMEWHERE ELSE - one directory per run under a runs root - and is never pruned
by project count.

THE SAFETY PROPERTY THIS MODULE MUST NEVER LOSE
-----------------------------------------------
A resumed run must never treat a file as already-reviewed unless the bytes it
reviewed are still the bytes on disk. Recorded review state is keyed to the
sha256 of the EXACT content that was reviewed, plus the audit POLICY VERSION.
`verify_reviewed()` re-hashes every entry through the caller's contained,
no-follow reader at resume time and DROPS anything that cannot be re-verified:
a changed file, an unreadable file, an entry with no reference hash, or a
checkpoint written under a different policy. Dropped means re-reviewed, which
costs money - the alternative is silently skipping unreviewed work, which is
the false-coverage bug this project treats as unacceptable.

Stdlib only. This module holds NO default path to the owner's home directory:
the runs root is always passed in by the caller, so a test that forgets to
redirect it gets a TypeError rather than quietly writing to ~/.flexfactor.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import time

SCHEMA_VERSION = 2

#: A run is resumable while its status is one of these.
LIVE_STATUSES = ("running", "interrupted")

#: Default cap on how many run directories are retained under a root. Finished
#: runs are pruned first; resumable ones only when nothing else can be dropped.
DEFAULT_KEEP_RUNS = 120

#: Minimum seconds between checkpoint flushes when saving incrementally, and
#: the number of recorded events that force a flush regardless of the clock.
#: A kill can therefore lose at most this much progress, never more.
DEFAULT_FLUSH_INTERVAL_S = 3.0
DEFAULT_FLUSH_EVERY = 10


def _now_iso() -> str:
    # MILLISECONDS, not seconds (fixed 2026-08-16). `list_runs` orders every
    # checkpoint by this string, so two runs started in the same second TIED and
    # a stable sort handed "newest" to whichever directory os.listdir happened to
    # return first. Combined with the run_id collision below, a resumed run could
    # recover the WRONG checkpoint and re-review work it had already paid for -
    # the exact defect flexfactor_runstate exists to prevent. Ordering must never
    # depend on listdir order.
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def pid_alive(pid: int) -> bool:
    """Is `pid` a live process?

    Windows-safe: `os.kill(pid, 0)` must NOT be used - on Windows every signal
    except CTRL_C/CTRL_BREAK maps to TerminateProcess, so the "probe" would kill
    the very run we are checking on. Unknown => alive, because treating a live
    run as resumable is how two processes end up auditing one repo.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _slug(text: str) -> str:
    out = []
    for ch in str(text or "run").lower():
        out.append(ch if ch.isalnum() else "-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:48] or "run"


def _atomic_write_json(path: str, payload: dict) -> bool:
    """Write JSON so a kill mid-write can never corrupt the checkpoint: a fresh
    temp file in the same directory, fsync'd, then os.replace() (atomic on both
    POSIX and Windows). Returns True on success; checkpointing is best-effort
    and must never break a run."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def checkpoint_path(root: str, run_id: str) -> str:
    return os.path.join(root, run_id, "checkpoint.json")


class RunCheckpoint:
    """One run's durable state. Mutated by `record_*` calls during the run and
    flushed atomically; `save(force=True)` at every phase boundary."""

    def __init__(self, root: str, data: dict,
                 flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
                 flush_every: int = DEFAULT_FLUSH_EVERY):
        if not root:
            raise ValueError("RunCheckpoint requires an explicit runs root")
        self.root = root
        self.data = data
        self.flush_interval_s = float(flush_interval_s)
        self.flush_every = int(flush_every)
        self._pending = 0
        self._last_flush = 0.0
        self.enabled = True  # a failed write disables further attempts, never raises

    # -- identity ---------------------------------------------------------- #
    @property
    def run_id(self) -> str:
        return str(self.data.get("run_id") or "")

    @property
    def path(self) -> str:
        return checkpoint_path(self.root, self.run_id)

    # -- writing ----------------------------------------------------------- #
    def save(self, force: bool = False) -> bool:
        """Flush to disk. Throttled unless `force`, so a 3,000-file review does
        not pay an fsync per file while still bounding what a kill can lose."""
        if not self.enabled:
            return False
        now = time.time()
        if not force:
            self._pending += 1
            if (self._pending < self.flush_every
                    and (now - self._last_flush) < self.flush_interval_s):
                return False
        self.data["updated"] = _now_iso()
        self.data["pid"] = os.getpid()
        ok = _atomic_write_json(self.path, self.data)
        if ok:
            self._pending = 0
            self._last_flush = now
        else:
            self.enabled = False
        return ok

    def set(self, **fields) -> None:
        self.data.update(fields)

    def set_phase(self, phase: str, **fields) -> None:
        self.data["phase"] = phase
        self.data.update(fields)
        self.save(force=True)

    def record_reviewed(self, rel: str, sha: str, findings: list | None) -> None:
        """Remember a COMPLETED review of `rel` whose content hashed to `sha`.

        Only call this for a review where every reviewer finished; a review
        aborted by the budget or an exception must never be recorded, or resume
        would skip a file nobody actually reviewed."""
        key = str(rel).replace("\\", "/")
        if not sha:
            return  # no reference hash -> unverifiable later -> do not record
        self.data.setdefault("reviewed", {})[key] = {
            "sha": sha,
            "clean": not findings,
            "findings": list(findings or []),
        }
        self.save()

    def record_file_outcome(self, rel: str, outcome: str) -> None:
        """outcome: fixed | unverified | rolled_back | rejected | skipped."""
        key = str(rel).replace("\\", "/")
        self.data.setdefault("files", {})[key] = str(outcome)
        # A file whose content we just changed can no longer be trusted as
        # "already reviewed" - drop its recorded review so a resume re-reviews
        # the NEW content instead of replaying findings about the old bytes.
        if outcome in ("fixed", "unverified"):
            self.data.get("reviewed", {}).pop(key, None)
        self.save()

    def record_spend(self, usd: float) -> None:
        try:
            self.data["spend_usd"] = round(float(usd), 6)
        except (TypeError, ValueError):
            return
        self.save()

    def record_cycle(self, cycle: int, **fields) -> None:
        self.data["cycle"] = int(cycle)
        self.data.update(fields)
        self.save(force=True)

    def record_bootstrap(self, steps: list, ok: bool) -> None:
        self.data["bootstrap"] = {"done": bool(ok), "steps": list(steps or [])}
        self.save(force=True)

    def finish(self, status: str = "finished", **fields) -> None:
        self.data["status"] = str(status)
        self.data["finished_at"] = _now_iso()
        self.data.update(fields)
        self.save(force=True)

    # -- reading ----------------------------------------------------------- #
    def summary(self) -> dict:
        return run_summary(self.data)


def new_run(root: str, *, program: str, project_dir: str, mode: str,
            policy: str, tool: str = "", max_cost: float = 0.0,
            cycles_cap: int = 0, run_id: str | None = None,
            **extra) -> RunCheckpoint:
    """Start a fresh checkpoint for one program of one run."""
    # MICROSECONDS in the id, for the same reason (fixed 2026-08-16):
    # program + second + pid COLLIDES whenever one process starts two
    # checkpoints for the same program inside one second, and the second
    # new_run then silently OVERWRITES the first one's directory instead of
    # getting its own. Two runs of one program in one second is not exotic -
    # it is what the resume tests do, and what a fast retry does.
    rid = run_id or "{}-{}-{}".format(
        _slug(program),
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"),
        os.getpid())
    data = {
        "schema": SCHEMA_VERSION,
        "run_id": rid,
        "policy": policy,
        "tool": tool,
        "mode": mode,
        "program": str(program),
        "project_dir": project_dir,
        "started": _now_iso(),
        "updated": _now_iso(),
        "pid": os.getpid(),
        "status": "running",
        "phase": "starting",
        "cycle": 0,
        "cycles_cap": int(cycles_cap or 0),
        "files_total": 0,
        "spend_usd": 0.0,
        "max_cost": float(max_cost or 0.0),
        "defects_found": 0,
        "defects_fixed": 0,
        "branch": None,
        "prev_branch": None,
        "bootstrap": {"done": False, "steps": []},
        "reviewed": {},
        "files": {},
        "resume_count": 0,
    }
    data.update(extra)
    cp = RunCheckpoint(root, data)
    cp.save(force=True)
    return cp


def load(root: str, run_id: str) -> RunCheckpoint | None:
    data = _read_json(checkpoint_path(root, run_id))
    if not data:
        return None
    return RunCheckpoint(root, data)


def run_summary(data: dict) -> dict:
    """Compact, display-ready view of one checkpoint (never the whole findings
    blob, which can be megabytes)."""
    reviewed = data.get("reviewed") or {}
    files = data.get("files") or {}
    total = int(data.get("files_total") or 0)
    n_reviewed = len(reviewed)
    fixed = sum(1 for v in files.values() if v in ("fixed", "unverified"))
    pct = int(round(100.0 * n_reviewed / total)) if total else 0
    return {
        "run_id": data.get("run_id"),
        "program": data.get("program"),
        "project_dir": data.get("project_dir"),
        "mode": data.get("mode"),
        "status": data.get("status"),
        "phase": data.get("phase"),
        "started": data.get("started"),
        "updated": data.get("updated"),
        "cycle": data.get("cycle"),
        "cycles_cap": data.get("cycles_cap"),
        "spend_usd": float(data.get("spend_usd") or 0.0),
        "max_cost": float(data.get("max_cost") or 0.0),
        "files_total": total,
        "reviewed": n_reviewed,
        "fixed": fixed,
        "defects_found": int(data.get("defects_found") or 0),
        "percent": pct,
        "branch": data.get("branch"),
        "pid": data.get("pid"),
        "resume_count": int(data.get("resume_count") or 0),
    }


def list_runs(root: str) -> list[dict]:
    """Every readable checkpoint under `root`, newest first. Corrupt or partial
    directories are skipped rather than raising."""
    out: list[dict] = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        data = _read_json(checkpoint_path(root, name))
        if not data or not data.get("run_id"):
            continue
        out.append(data)
    out.sort(key=lambda d: str(d.get("updated") or ""), reverse=True)
    return out


def is_resumable(data: dict) -> bool:
    """A run is resumable when it never reached a terminal status AND no live
    process is still working on it. A run whose PID is alive belongs to someone
    else - offering to resume it would double-spend and fight over the branch."""
    if not data or data.get("schema") != SCHEMA_VERSION:
        return False
    if str(data.get("status")) not in LIVE_STATUSES:
        return False
    if pid_alive(data.get("pid") or 0) and int(data.get("pid") or 0) != os.getpid():
        return False
    # Nothing to pick up: no review recorded, nothing fixed, no bootstrap done.
    d = data
    if (not (d.get("reviewed") or {}) and not (d.get("files") or {})
            and not ((d.get("bootstrap") or {}).get("done"))):
        return False
    return True


def resumable_runs(root: str, program: str | None = None,
                   project_dir: str | None = None) -> list[dict]:
    rows = [d for d in list_runs(root) if is_resumable(d)]
    if program:
        want = _slug(program)
        rows = [d for d in rows if _slug(d.get("program") or "") == want
                or str(d.get("program") or "").lower() == str(program).lower()]
    if project_dir:
        want_dir = os.path.normcase(os.path.abspath(project_dir))
        rows = [d for d in rows
                if os.path.normcase(os.path.abspath(str(d.get("project_dir") or ""))) == want_dir]
    return rows


def latest_resumable(root: str, program: str | None = None,
                     project_dir: str | None = None) -> dict | None:
    rows = resumable_runs(root, program=program, project_dir=project_dir)
    return rows[0] if rows else None


def verify_reviewed(data: dict, hasher, policy: str) -> tuple[dict, dict, list]:
    """Re-verify a checkpoint's recorded review state against the CURRENT files.

    `hasher(rel) -> sha256 hex | None` must be the caller's contained, no-follow
    reader, so containment refusals surface as None (and are dropped).

    Returns (clean, findings, dropped):
      clean    - {rel: sha} files reviewed clean whose content is UNCHANGED.
                 Safe to skip on the resumed run.
      findings - {rel: (sha, [finding, ...])} files reviewed with defects whose
                 content is UNCHANGED. Their findings can be replayed without
                 paying for the review again.
      dropped  - rels that could NOT be re-verified and must be re-reviewed.

    Fail-closed by construction: a policy mismatch drops EVERYTHING, and any
    entry missing a reference hash, unreadable now, or whose hash differs is
    dropped. Nothing is ever trusted on pathname alone.
    """
    clean: dict[str, str] = {}
    findings: dict[str, tuple] = {}
    dropped: list[str] = []
    if not data or data.get("policy") != policy or data.get("schema") != SCHEMA_VERSION:
        return {}, {}, sorted((data or {}).get("reviewed") or {})
    for rel, rec in sorted(((data.get("reviewed") or {})).items()):
        if not isinstance(rec, dict):
            dropped.append(rel)
            continue
        expected = rec.get("sha")
        if not expected:
            dropped.append(rel)          # no verified reference hash -> re-review
            continue
        try:
            cur = hasher(rel)
        except Exception:
            cur = None
        if cur is None or cur != expected:
            dropped.append(rel)          # missing/refused/CHANGED -> re-review
            continue
        if rec.get("clean"):
            clean[rel] = cur
        else:
            findings[rel] = (cur, list(rec.get("findings") or []))
    return clean, findings, dropped


def prune(root: str, keep: int = DEFAULT_KEEP_RUNS) -> int:
    """Bound the runs directory. FINISHED runs are deleted oldest-first; a
    resumable run is only removed once nothing finished is left to drop, so an
    interrupted run is never quietly discarded to make room."""
    rows = list_runs(root)
    if len(rows) <= keep:
        return 0
    rows.sort(key=lambda d: str(d.get("updated") or ""))  # oldest first
    finished = [d for d in rows if str(d.get("status")) not in LIVE_STATUSES]
    live = [d for d in rows if str(d.get("status")) in LIVE_STATUSES]
    order = finished + live
    removed = 0
    for d in order[: max(0, len(rows) - keep)]:
        try:
            shutil.rmtree(os.path.join(root, str(d.get("run_id"))), ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def format_rows(rows: list[dict]) -> str:
    """ASCII table of run summaries for the CLI and the launcher."""
    if not rows:
        return "  (no resumable runs)"
    lines = ["  {:<34} {:<14} {:<9} {:>7} {:>9}  {}".format(
        "RUN ID", "PROGRAM", "MODE", "DONE", "SPENT", "LAST UPDATE")]
    for d in rows:
        s = run_summary(d)
        lines.append("  {:<34} {:<14} {:<9} {:>6}% {:>9}  {}".format(
            str(s["run_id"])[:34], str(s["program"])[:14], str(s["mode"])[:9],
            s["percent"], "${:.2f}".format(s["spend_usd"]), s["updated"]))
    return "\n".join(lines)
