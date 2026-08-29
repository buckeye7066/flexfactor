#!/usr/bin/env python3
r"""
FlexFactor live dashboard.

Polls the status file that `flexfactor.py audit` writes
(~/.flexfactor/status.json by default, or the path passed as argv[1]) and draws a
live panel per program: side-by-side vertical bar graphs that glide toward 0 or
100% - Review progress, Fix progress, and Budget used - plus live counts
(defects found, files fixed, errors) and the exact file being worked on at the
bottom. One column per program, so auditing several at once shows them all.

The audit launches this automatically (unless --no-dashboard). You can also run
it by hand:

    python flexfactor_dashboard.py
    python flexfactor_dashboard.py C:\Users\me\.flexfactor\status.json

Pure-stdlib (tkinter); no dependencies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# The error ledger's own reader. Stdlib-only and never imports flexfactor,
# so this stays a pure viewer. Soft import: an older tree without the module
# must still get a working dashboard, just without the error box.
try:
    import flexfactor_errors as _fe
except Exception:  # noqa: BLE001 - a missing viewer feature is not a crash
    _fe = None

# Operator steering (owner order 2026-08-28: "a text box where I can steer the
# direction the program is taking in its editing"). The desktop dashboard is the
# UI the audit auto-launches, so the steering box has to live HERE and not only
# in the web dashboard - a control the owner has to go find in a browser is a
# control that does not exist during the run it was meant to redirect.
# Soft import for the same reason as _fe: an older tree still gets a dashboard.
try:
    import flexfactor_steering as _steer
except Exception:  # noqa: BLE001
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import flexfactor_steering as _steer
    except Exception:  # noqa: BLE001
        _steer = None

STATUS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.expanduser("~"), ".flexfactor", "status.json")

# THE BLACK-SCREEN-FLASH BUG (2026-08-16, owner report: "it flashes a black
# screen constantly and I can't type or anything else"). This dashboard is
# launched with pythonw.exe, so the process has NO console - and on Windows a
# console-less parent that spawns a console child (git.exe) without
# CREATE_NO_WINDOW gets a BRAND-NEW visible console window for every call.
# attempt_info() ran `git log` from redraw(), and redraw reschedules itself
# every 40ms - up to ~25 fresh black console windows per second, each one
# stealing keyboard focus. The machine was unusable while an audit ran.
# Two rules, both load-bearing:
#   1. EVERY subprocess this file starts passes creationflags=_NO_WINDOW.
#   2. NO disk/subprocess I/O runs per frame - slow facts go through the
#      TTL cache below and refresh at most once per _FACTS_TTL_S per program.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_FACTS_TTL_S = 5.0
_FACTS_CACHE: dict[str, tuple[float, str]] = {}  # key -> (expires_at, value)

# Dark palette.
BG = "#0d1117"
PANEL = "#161b22"
EDGE = "#30363d"
TEXT = "#c9d1d9"
DIM = "#8b949e"
REVIEW = "#58a6ff"   # blue
FIX = "#3fb950"      # green
BUDGET = "#d29922"   # amber
BUDGET_OVER = "#f85149"  # red when at/over cap
DONE = "#3fb950"
ERRCOL = "#f85149"
ERRBOX = "#1a1113"    # error box fill - a shade warmer than PANEL
ERRHEAD = "#ff7b72"
FIXCOL = "#7ee787"    # the suggested fix reads as the actionable line
# The small note face. MODULE level, not a local of draw_frame: the resume
# button at the TOP of a panel measures itself with it, and a function-local
# assignment further down would make every earlier reference an UnboundLocalError.
F_NOTE = ("Segoe UI", 7)

# Which kind of error the entry was filed under, in the box's accent color.
KIND_COLOR = {
    "flexfactor-defect": "#f85149",   # ours - the only kind that is a bug here
    "program-defect": "#ff7b72",      # the audited program's
    "provider": "#d29922",            # a route failed; rotation absorbs it
    "budget": "#d29922",
    "environment": "#58a6ff",
    "unknown": "#8b949e",
}

# Severity colors, drawn in this order.
SEV_ORDER = ["critical", "high", "medium", "low", "info"]
SEV_COLOR = {
    "critical": "#f85149",  # red
    "high": "#ff7b72",      # salmon
    "medium": "#d29922",    # amber
    "low": "#58a6ff",       # blue
    "info": "#8b949e",      # gray
}


def read_status(path: str) -> list[dict]:
    """Load the program list from the status file. Returns [] on any problem so
    the dashboard simply shows 'waiting' instead of crashing on a partial write."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        progs = data.get("programs") or []
        return progs if isinstance(progs, list) else []
    except (OSError, ValueError):
        return []


def attempt_info(p: dict) -> str:
    """'attempt 5 - 3 commits landed' - the two numbers that SURVIVE a restart.

    `cycle` in status.json counts cycles inside the CURRENT process, so it resets
    to 1 every relaunch: on 2026-08-14 the panel read 'cycle 1/12' after five
    attempts, hiding that four of them had produced zero commits. Read straight
    off disk (checkpoint dirs + git log) so this needs no change to the running
    audit and no new status.json field. Best-effort: never raises, returns "" when
    it cannot tell.

    Called from redraw() at ~25fps, so the disk walk + git subprocess are behind
    a per-program TTL cache: recomputed at most every _FACTS_TTL_S seconds, and
    the subprocess runs with _NO_WINDOW (see the module comment - without it,
    each call flashed a black console window and stole focus)."""
    key = f"{p.get('name') or ''}|{p.get('dir') or ''}"
    hit = _FACTS_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    value = _attempt_info_uncached(p)
    _FACTS_CACHE[key] = (time.monotonic() + _FACTS_TTL_S, value)
    return value


def _attempt_info_uncached(p: dict) -> str:
    try:
        prog = str(p.get("name") or "")
        proj = str(p.get("dir") or "")
        slug = "".join(c.lower() if c.isalnum() else "-" for c in prog).strip("-")
        runs_dir = os.path.join(os.path.expanduser("~"), ".flexfactor", "runs")
        attempts = 0
        resumes = 0
        if slug and os.path.isdir(runs_dir):
            for d in os.listdir(runs_dir):
                if not d.startswith(slug + "-"):
                    continue
                attempts += 1
                try:
                    with open(os.path.join(runs_dir, d, "checkpoint.json"),
                              encoding="utf-8") as fh:
                        resumes += int(json.load(fh).get("resume_count") or 0)
                except (OSError, ValueError, TypeError):
                    pass
        landed = None
        if proj and os.path.isdir(os.path.join(proj, ".git")):
            try:
                out = subprocess.run(
                    ["git", "-C", proj, "log", "--oneline", "--grep=FlexFactor"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_NO_WINDOW)
                if out.returncode == 0:
                    landed = len([ln for ln in out.stdout.splitlines() if ln.strip()])
            except (OSError, subprocess.SubprocessError):
                pass
        if not attempts and landed is None:
            return ""
        bits = []
        if attempts:
            bits.append(f"attempt {attempts}" + (f" (+{resumes} resumes)" if resumes else ""))
        if landed is not None:
            bits.append(f"{landed} commit{'' if landed == 1 else 's'} landed")
        return " - ".join(bits)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# THE ERROR BOX (owner 2026-08-23: "set the error reports for flexfactor as
# communication in a box I can see below each program being run")
#
# The run already writes a full ledger -- what failed, which code is
# responsible, and a suggested fix -- to <run dir>/errors.{json,md}. Until now
# that could only be read by opening the file after the fact, so the live panel
# said "errors: 3" and nothing about WHAT. This reads the same errors.json the
# report is built from and draws the last few entries under the program.
#
# Rules this inherits from the rest of the file:
#   * NO per-frame disk I/O. redraw() runs at ~25 fps; the read is behind a TTL
#     cache, same as attempt_info().
#   * READ-ONLY. This viewer opens errors.json for reading and nothing else.
#   * NO unlabelled scopes. The panel's counter counts FILES that errored
#     during review/fix; the box counts EVERY recorded failure, including the
#     provider retries rotation absorbed. Two different numbers, so the counter
#     reads "file errors: N" and the box header names its own total by kind
#     ("3 errors: 1 flexfactor-defect, 1 provider, 1 budget"). They must never
#     sit next to each other as two bare numbers.
# --------------------------------------------------------------------------- #
_ERR_TTL_S = 2.0
# key -> (expires_at, value, stat signature of errors.json when it was parsed)
_ERR_CACHE: dict[str, tuple[float, dict, tuple]] = {}
ERR_ROWS = 3          # most entries drawn (fewer when they do not fit)
ERR_ROW_H = 46        # px one entry needs: kind line, error, code, fix
ERR_BOX_H = 200       # px reserved at the bottom (fits ERR_ROWS entries)


def _run_dir_for(p: dict) -> str:
    """Where this program's ledger lives. Status first, disk-scan as fallback."""
    rd = str(p.get("run_dir") or "")
    if rd and os.path.isdir(rd):
        return rd
    if _fe is None:
        return ""
    return _fe.find_run_dir(str(p.get("name") or ""))


def _errors_uncached(p: dict) -> dict:
    if _fe is None:
        return {"headline": "", "rows": [], "md_path": "", "available": False}
    run_dir = _run_dir_for(p)
    entries = _fe.load_entries(run_dir)
    return {"headline": _fe.headline(entries),
            "rows": _fe.ui_entries(entries, ERR_ROWS),
            "total": len(entries),
            "md_path": os.path.join(run_dir, "errors.md") if run_dir else "",
            "available": True}


def _ledger_stat(p: dict) -> tuple:
    """(mtime, size) of this program's errors.json - the cheap change signal."""
    try:
        st = os.stat(os.path.join(_run_dir_for(p), "errors.json"))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()


def errors_for(p: dict) -> dict:
    """This program's ledger view, recomputed at most every _ERR_TTL_S - and
    only when the file actually changed.

    A long run's ledger grows without bound (measured: 148 entries = 472 KB,
    5.7 ms to parse; tracebacks are capped per entry, not per file). Re-parsing
    that for every program every couple of seconds for hours is work with no
    output, so past the TTL we stat first and only re-read when mtime or size
    moved. On this machine a stat is single-digit milliseconds and a parse is
    not, and the ledger is quiet most of the time.
    """
    key = program_key(p)
    now = time.monotonic()
    hit = _ERR_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    sig = _ledger_stat(p)
    if hit and hit[2] == sig and sig:
        _ERR_CACHE[key] = (now + _ERR_TTL_S, hit[1], sig)   # unchanged: keep it
        return hit[1]
    try:
        value = _errors_uncached(p)
    except Exception:  # noqa: BLE001 - the box must never take the panel down
        value = {"headline": "", "rows": [], "md_path": "", "available": False}
    _ERR_CACHE[key] = (now + _ERR_TTL_S, value, sig)
    return value


_FONT_CACHE: dict = {}


def _measure(spec, text: str):
    """Width of `text` in font `spec`, or None when Tk cannot tell us.

    Measured, not estimated: the first screenshot of this box (2026-08-23) had
    the fix line running past the panel edge and over the next program, because
    a fixed characters-per-pixel guess is wrong for a proportional font at any
    DPI but the one it was tuned on. Font objects are cached; `measure` is one
    cheap Tcl call."""
    try:
        import tkinter.font as tkfont
    except Exception:  # noqa: BLE001 - no Tk at all
        return None
    for attempt in (0, 1):
        try:
            font = _FONT_CACHE.get(spec)
            if font is None:
                font = _FONT_CACHE[spec] = tkfont.Font(font=spec)
            return font.measure(text)
        except Exception:  # noqa: BLE001
            # A cached Font belongs to the Tk interpreter that made it. If that
            # root is gone (a second dashboard window, a test that builds one
            # root per case) every measure raises and we would SILENTLY drop
            # back to the character-width guess - which is exactly the overflow
            # this function exists to prevent. Rebuild once, then give up.
            _FONT_CACHE.pop(spec, None)
            if attempt:
                return None
    return None


def fit(text: str, px: float, char_px: float = 5.3, spec=None) -> str:
    """Truncate to what actually fits `px` wide, with an ellipsis when cut.

    Canvas text does not clip to a rectangle, so an untruncated error message
    paints straight over the next panel. With `spec` the width is MEASURED in
    that font; without it (or on a Tk-less machine) it falls back to the
    characters-per-pixel estimate, which is why `char_px` still exists."""
    t = " ".join(str(text or "").split())
    if not t:
        return t
    width = _measure(spec, t) if spec is not None else None
    if width is None:
        n = max(4, int(px / max(1.0, char_px)))
        return t if len(t) <= n else t[: n - 3] + "..."
    if width <= px:
        return t
    # Proportional first cut, then walk in until it fits. Bounded: the ratio
    # lands within a character or two, so this is a handful of measures.
    keep = max(1, int(len(t) * px / max(1.0, width)) - 1)
    cut = t[:keep] + "..."
    while keep > 1 and (_measure(spec, cut) or 0) > px:
        keep -= 1
        cut = t[:keep] + "..."
    return cut


def open_ledger(md_path: str) -> bool:
    """Open errors.md in whatever the OS uses for .md. Read-only action: it
    hands the path to the shell and never touches the run. False if it cannot."""
    try:
        if not md_path or not os.path.exists(md_path):
            return False
        if os.name == "nt":
            os.startfile(md_path)  # type: ignore[attr-defined]  # no console window
        else:
            subprocess.Popen(["xdg-open", md_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Copy button (owner request 2026-08-24: "a 'copy' button by each of the error
# boxes that saves that information to my clipboard").
#
# The box only ever PAINTS the newest few entries (ERR_ROWS, fewer on a short
# window). What is worth pasting into an issue or a chat is the whole program's
# ledger, so the payload re-reads it AT CLICK TIME. That does not break the
# standing no-per-frame-disk-I/O rule the redraw loop lives by - a click is not
# a frame, and it happens at human speed, not 25fps.
#
# Truncation is never silent: a capped payload says how many it left out and
# where the rest lives, the same contract the box footer already keeps.
# --------------------------------------------------------------------------- #
COPY_MAX_ROWS = 200
COPY_FEEDBACK_S = 1.6
# program key -> monotonic deadline until which the button reads "copied!"
_COPIED_UNTIL: dict[str, float] = {}


def format_error_clipboard(name: str, md_path: str, headline: str, rows: list,
                           total: int, limit: int = COPY_MAX_ROWS) -> str:
    """The plain text one copy click puts on the clipboard.

    Pure and Tk-free so it can be tested without a display: the same three
    facts the box paints per entry (what failed / which code is responsible /
    what to do), plus the ledger path so a truncated payload is still
    actionable."""
    rows = list(rows or [])
    shown = rows[:max(0, int(limit))]
    out = [f"FlexFactor errors - {name}"]
    if headline:
        out.append(headline)
    if md_path:
        out.append(f"ledger: {md_path}")
    out.append("")
    if not shown:
        out.append("(no errors recorded)")
    for r in shown:
        fix = r.get("fix") or "no known fix"
        # An unverified model guess is never dressed as a known fix - the same
        # rule the painted box follows.
        if r.get("fix_source") == "model":
            fix = "(unverified) " + fix
        out.append(f"#{r.get('n')} {r.get('kind')} / {r.get('phase')}")
        out.append(f"  error: {r.get('error', '')}")
        out.append(f"  code : {r.get('where', '')}")
        out.append(f"  fix  : {fix}")
        out.append("")
    left = int(total or 0) - len(shown)
    if left > 0:
        out.append(f"... and {left} more of {total} total - full ledger: "
                   f"{md_path or '(path unknown)'}")
    return "\n".join(out).rstrip() + "\n"


def error_clipboard_payload(p: dict) -> str:
    """Build one program's copy payload, reading the FULL ledger when it can.

    Falls back to the cached handful the box is already showing if the read
    fails - a degraded copy beats a dead button, and the header still names
    the ledger path."""
    info = errors_for(p)
    name = str(p.get("name") or f"#{p.get('index')}")
    rows = info.get("rows") or []
    total = int(info.get("total") or len(rows))
    if _fe is not None:
        try:
            entries = _fe.load_entries(_run_dir_for(p))
            total = len(entries)
            rows = _fe.ui_entries(entries, min(len(entries), COPY_MAX_ROWS))
        except Exception:  # noqa: BLE001 - never take the panel down
            pass
    return format_error_clipboard(name, info.get("md_path") or "",
                                  info.get("headline") or "", rows, total)


def copy_to_clipboard(widget, text: str) -> bool:
    """Put text on the system clipboard through Tk. Never raises.

    NOTE (Windows): Tk owns the clipboard while the process lives, so the text
    survives for as long as the dashboard is open - which is the whole window
    in which the owner would paste it. Closing the dashboard immediately after
    copying can drop it; that is a Tk/Win32 property, not something this
    function can fix without a native clipboard render."""
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
        widget.update_idletasks()
        return True
    except Exception:  # noqa: BLE001
        return False


def do_copy(widget, p: dict, key: str) -> bool:
    """Click action: copy, and arm the 'copied!' label if it actually worked."""
    ok = copy_to_clipboard(widget, error_clipboard_payload(p))
    if ok:
        _COPIED_UNTIL[key] = time.monotonic() + COPY_FEEDBACK_S
    return ok


def bar_targets(p: dict) -> dict:
    """Compute the 0..1 targets for one program's three bars from raw fields."""
    files_total = max(1, int(p.get("files_total") or 0) or 1)
    reviewed = int(p.get("reviewed") or 0)
    fix_total = int(p.get("fix_total") or 0)
    fix_done = int(p.get("fix_done") or 0)
    cap = p.get("cap")
    cost = float(p.get("cost") or 0.0)
    review = min(1.0, reviewed / files_total) if files_total else 0.0
    fix = min(1.0, fix_done / fix_total) if fix_total else 0.0
    if p.get("done"):
        review = 1.0
        if fix_total:
            fix = min(1.0, fix_done / fix_total)
    budget = min(1.0, cost / cap) if cap else 0.0
    return {"review": review, "fix": fix, "budget": budget, "cap": cap, "cost": cost}


# --------------------------------------------------------------------------- #
# Dismiss ("x") - owner request 2026-08-19
# --------------------------------------------------------------------------- #
# > "give me an 'x' to delete a program out of flexfactor, like in the situation
# >  of Iplay just now, to leave room for the graphics of the other programs."
#
# Five programs ran concurrently; IPlay STOPPED early on a red baseline and its
# dead panel kept holding a fifth of the window while the other four worked.
#
# THIS IS A VIEW ACTION AND NOTHING ELSE, and the architecture is what makes
# that true rather than a promise: this file is a pure READER of status.json. It
# has never opened that file for writing and still does not, it holds no handle
# on any audit process, and the end-of-run summary is produced by flexfactor.py
# from its own in-process totals without ever consulting this module. So
# dismissing cannot kill an audit, cannot mutate a run's state, and cannot make
# a stopped program's outcome unreportable. `_DISMISSED` lives in memory only -
# nothing is persisted, so a fresh dashboard starts with every panel shown.
#
# WHAT HAPPENS IF A DISMISSED PROGRAM COMES BACK TO LIFE: it REAPPEARS. The
# dismissal is recorded against the program's activity signature at the moment
# it was hidden and holds only while that signature does. A finished or stopped
# program never moves again, so it stays gone for the session - which is the
# whole point of the request. A program that resumes reviewing, fixing, spending
# or changing phase changes its signature and comes straight back. That is the
# honest choice: this panel is the owner's only live view of what is running,
# and a display that silently hides working programs would be lying about the
# run. Clicking "x" again re-hides it against the new signature.
_DISMISSED: dict[str, tuple] = {}  # program key -> activity signature when hidden


def program_key(p: dict) -> str:
    """Identity of one panel. Name+dir, falling back to the batch index."""
    name = str(p.get("name") or "")
    proj = str(p.get("dir") or "")
    if name or proj:
        return f"{name}|{proj}"
    return f"#{p.get('index')}"


def activity_signature(p: dict) -> tuple:
    """Everything that changes when a program does real work.

    Deliberately excludes heartbeat-only fields (`updated`, timestamps): a
    stopped program whose status entry is merely re-serialized must NOT count as
    activity, or the dismissal would bounce back on the next poll.
    """
    return tuple(p.get(k) for k in (
        "phase", "done", "reviewed", "files_total", "fixed", "fix_done",
        "fix_total", "defects", "defects_fixed", "errors", "cost",
        "current_file", "cycle", "cycles"))


def dismiss(p: dict) -> None:
    """Hide one program's panel for this dashboard session."""
    _DISMISSED[program_key(p)] = activity_signature(p)


def restore_all() -> None:
    """Show every dismissed panel again."""
    _DISMISSED.clear()


def is_dismissed(p: dict) -> bool:
    """True while this program is hidden AND has not moved since it was hidden."""
    key = program_key(p)
    if key not in _DISMISSED:
        return False
    if _DISMISSED[key] != activity_signature(p):
        del _DISMISSED[key]  # new activity un-hides it, permanently until re-x'd
        return False
    return True


def visible_programs(progs: list[dict]) -> list[dict]:
    """The panels to draw. Never mutates or drops anything from status.json."""
    return [p for p in progs if not is_dismissed(p)]


# --------------------------------------------------------------------------- #
# PER-PROGRAM RESUME BUTTON (added 2026-08-29 by a subagent, UNREQUESTED - see
# the provenance note on _record_invocation in flexfactor.py. The owner has not
# asked for this feature, and no directive for it exists in the transcript,
# .remember or the vault; an earlier draft of this comment quoted one verbatim.)
#
# The failure this exists to prevent is the one the honesty doctrine cares about
# most: a control that LOOKS like a resume and silently starts a full fresh run.
# FlexFactor resumes by RECOVERING a checkpoint, and `flexfactor_runstate.
# is_resumable` is the only authority on whether that recovery can happen - a
# checkpoint whose owning pid is still alive, or that reached a terminal status,
# or that recorded nothing to pick up, is NOT resumable and re-launching it
# would re-pay for the whole program. So the button asks that function, refuses
# to launch when the answer is no, and SAYS WHY on the panel. It never
# reimplements the rule (a second resumability vocabulary is how the
# launcher-drift trap starts).
#
# The button is still only a launcher: it re-issues the run's own recorded argv
# with a single `--program`. Whether the recovery then actually engaged is
# visible on the panel's own attempt line ("attempt N (M resumed)"), which reads
# `resume_count` straight off the checkpoint - so the claim is verifiable by the
# owner rather than asserted here. One thing this cannot predict: FlexFactor
# also drops a checkpoint written under a DIFFERENT purpose-contract policy, and
# computing that needs the whole audit runtime, not a viewer. The attempt line
# is the evidence for that case too.
# --------------------------------------------------------------------------- #
try:
    import flexfactor_runstate as _rs
except Exception:  # noqa: BLE001 - same soft-import contract as _fe/_steer
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import flexfactor_runstate as _rs
    except Exception:  # noqa: BLE001
        _rs = None

INVOCATION_PATH = os.path.join(os.path.expanduser("~"), ".flexfactor",
                               "last-invocation.json")

# program key -> (expires_at, message). Transient, in-memory, per session -
# same contract as _DISMISSED: this file never writes audit state.
_RESUME_NOTE: dict[str, tuple] = {}
_RESUME_NOTE_S = 12.0
# program key -> pid we launched, so the button can report itself honestly.
_RESUMED: dict[str, int] = {}


def _checkpoint_for(p: dict) -> dict:
    """This program's checkpoint, or {} when there is none to read."""
    run_dir = _run_dir_for(p)
    if not run_dir:
        return {}
    try:
        with open(os.path.join(run_dir, "checkpoint.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resume_state(p: dict) -> tuple:
    """(label, enabled, reason) for this program's resume button.

    `enabled` is False whenever a launch would NOT continue the recorded work,
    and the reason names which condition blocked it. No button is drawn at all
    when there is no checkpoint - there is nothing to continue."""
    key = program_key(p)
    pid = _RESUMED.get(key)
    if pid and _rs is not None and _rs.pid_alive(pid):
        return (f"resuming {pid}", False, f"resume running as pid {pid}")
    if _rs is None:
        return ("", False, "flexfactor_runstate.py not importable")
    ckpt = _checkpoint_for(p)
    if not ckpt:
        return ("", False, "no checkpoint on disk")
    if _rs.is_resumable(ckpt):
        return ("resume", True, "")
    owner = int(ckpt.get("pid") or 0)
    if _rs.pid_alive(owner) and owner != os.getpid():
        return ("resume: live", False,
                f"pid {owner} is still working on this run - "
                f"resuming now would fight it for the branch")
    status = str(ckpt.get("status") or "?")
    if not (ckpt.get("reviewed") or ckpt.get("files")
            or (ckpt.get("bootstrap") or {}).get("done")):
        return ("resume: empty", False,
                f"checkpoint ({status}) recorded no reviewed file to pick up - "
                f"a relaunch would start over, so this refuses")
    return ("resume: done", False,
            f"checkpoint status '{status}' is terminal - nothing left to resume")


def _read_invocation() -> dict:
    try:
        with open(INVOCATION_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resume_argv(p: dict, inv: dict, ckpt: dict) -> list:
    """The recorded launch argv, narrowed to THIS program.

    `--program` is `action="append"`, so its occurrences in argv are in the same
    order as the status entries' 1-based `index` - that is how one slot of a
    five-program batch is identified without guessing at name resolution. Both
    spellings (`--program X` and `--program=X`) are handled; the project dir is
    the fallback when the index cannot be matched, because it is what the
    checkpoint is actually keyed on."""
    argv = [str(a) for a in (inv.get("argv") or [])]
    tokens, kept, skip = [], [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--program":
            if i + 1 < len(argv):
                tokens.append(argv[i + 1])
            skip = True
            continue
        if a.startswith("--program="):
            tokens.append(a.split("=", 1)[1])
            continue
        kept.append(a)
    idx = int(p.get("index") or 0) - 1
    token = tokens[idx] if 0 <= idx < len(tokens) else ""
    if not token:
        token = str(ckpt.get("project_dir") or p.get("dir") or p.get("name") or "")
    if not kept or not token:
        return []
    out = list(kept) + ["--program", token]
    # One dashboard per desktop, not one per resumed slot.
    if "--no-dashboard" not in out:
        out.append("--no-dashboard")
    return out


def resume_program(p: dict) -> bool:
    """Relaunch ONE program so its recorded checkpoint is picked up.

    Refuses (and says why on the panel) unless `flexfactor_runstate` says the
    checkpoint is genuinely resumable. Detached + windowless so it survives this
    viewer closing and cannot flash a console (see the module comment); the
    child's own output goes to a log beside the checkpoint it is continuing."""
    key = program_key(p)

    def note(msg: str) -> bool:
        _RESUME_NOTE[key] = (time.monotonic() + _RESUME_NOTE_S, msg)
        return False

    label, enabled, reason = resume_state(p)
    if not enabled:
        return note(reason or "cannot resume")
    inv = _read_invocation()
    if not inv.get("argv"):
        return note("no recorded launch to replay (~/.flexfactor/"
                    "last-invocation.json missing)")
    ckpt = _checkpoint_for(p)
    argv = resume_argv(p, inv, ckpt)
    if not argv:
        return note("recorded launch names no program to resume")
    exe = str(inv.get("python") or sys.executable)
    script = str(inv.get("script") or "")
    if not script or not os.path.isfile(script):
        return note(f"recorded launcher is gone: {script or '(none)'}")
    run_dir = _run_dir_for(p) or os.path.dirname(INVOCATION_PATH)
    log = os.path.join(run_dir, f"resume-{time.strftime('%Y%m%d-%H%M%S')}.log")
    flags = _NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        fh = open(log, "a", encoding="utf-8")
    except OSError as ex:
        return note(f"cannot open resume log: {ex}")
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed launcher, recorded argv
            [exe, script, *argv], cwd=(inv.get("cwd") or None),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=flags, close_fds=True)
    except (OSError, subprocess.SubprocessError) as ex:
        fh.close()
        return note(f"resume launch failed: {ex}")
    finally:
        try:
            fh.close()
        except OSError:
            pass
    _RESUMED[key] = proc.pid
    _RESUME_NOTE[key] = (time.monotonic() + _RESUME_NOTE_S,
                         f"resumed as pid {proc.pid} - watch the attempt line")
    return True


def resume_note(p: dict) -> str:
    """The transient one-line answer to the last click, or ""."""
    hit = _RESUME_NOTE.get(program_key(p))
    if not hit or hit[0] <= time.monotonic():
        return ""
    return str(hit[1])


# --------------------------------------------------------------------------- #
# ONE FRAME. Lifted out of _main's closure (2026-08-23) so the panel - bars,
# stats, and the new error box - can be DRAWN AND READ BACK by a test rather
# than only looked at by a person. `hits` and `shown` are passed in because they
# outlive a frame: `hits` is the click map rebuilt each time, `shown` holds the
# eased bar values that make bars glide instead of jump.
# --------------------------------------------------------------------------- #
def draw_frame(canvas, hits: list, shown: dict, W: float, H: float,
               all_progs: list, status_label: str = "") -> None:
    def ease(key: tuple, target: float) -> float:
        cur = shown.get(key, 0.0)
        cur += (target - cur) * 0.18  # simple exponential glide
        if abs(target - cur) < 0.002:
            cur = target
        shown[key] = cur
        return cur

    def draw_bar(x: float, base_y: float, w: float, h: float, frac: float,
                 color: str, label: str, value_txt: str) -> None:
        # Track.
        canvas.create_rectangle(x, base_y - h, x + w, base_y, outline=EDGE, width=1)
        fh = max(0.0, min(1.0, frac)) * h
        if fh > 0:
            canvas.create_rectangle(x, base_y - fh, x + w, base_y, outline="", fill=color)
        canvas.create_text(x + w / 2, base_y - h - 12, text=value_txt, fill=TEXT,
                           font=("Segoe UI", 9, "bold"))
        canvas.create_text(x + w / 2, base_y + 12, text=label, fill=DIM,
                           font=("Segoe UI", 8))

    progs = visible_programs(all_progs)
    hidden = len(all_progs) - len(progs)

    canvas.create_text(14, 16, anchor="w", text="FlexFactor", fill=TEXT,
                       font=("Segoe UI", 14, "bold"))
    canvas.create_text(W - 14, 16, anchor="e",
                       text=status_label or f"watching {os.path.basename(STATUS_PATH)}",
                       fill=DIM, font=("Segoe UI", 8))
    # A dismissed panel is never a silent disappearance: the count and the
    # way back are always on screen. The run itself still reports it.
    if hidden:
        canvas.create_text(W / 2, 16, text=f"{hidden} dismissed - click to "
                                           f"show (they are still audited)",
                           fill=DIM, font=("Segoe UI", 8))
        hits.append((W / 2 - 140, 8, W / 2 + 140, 24, restore_all))

    if not all_progs:
        canvas.create_text(W / 2, H / 2, text="waiting for an audit to start...",
                           fill=DIM, font=("Segoe UI", 12))
        return
    if not progs:
        canvas.create_text(W / 2, H / 2, text="all panels dismissed - click "
                                              "the line above to show them",
                           fill=DIM, font=("Segoe UI", 12))
        return

    n = len(progs)
    pad = 14
    col_w = (W - pad * (n + 1)) / n
    top = 40
    bottom = H - pad
    panel_h = bottom - top

    for i, p in enumerate(progs):
        cx = pad + i * (col_w + pad)
        # Panel.
        canvas.create_rectangle(cx, top, cx + col_w, bottom, outline=EDGE,
                                fill=PANEL, width=1)
        name = str(p.get("name") or f"program {p.get('index', i + 1)}")
        phase = str(p.get("phase") or "")
        done = bool(p.get("done"))
        cyc = p.get("cycle")
        cycles = p.get("cycles")
        title_col = DONE if done else REVIEW
        canvas.create_text(cx + col_w / 2, top + 16, text=name[:34], fill=title_col,
                           font=("Segoe UI", 11, "bold"))
        sub = ("DONE" if done else phase) + (
            f"  (cycle {cyc}/{cycles})"
            if cyc and cycles and not done and "cycle" not in phase else "")
        canvas.create_text(cx + col_w / 2, top + 34, text=sub[:42], fill=DIM,
                           font=("Segoe UI", 8))
        # Dismiss control, drawn AFTER the title and subtitle so a long
        # centred program name paints under it and can never bury the only
        # way to reclaim the column. `p=p` binds THIS program - a bare
        # closure over the loop variable would hand every "x" the last
        # panel drawn.
        canvas.create_text(cx + col_w - 12, top + 12, text="x", fill=DIM,
                           font=("Segoe UI", 11, "bold"))
        hits.append((cx + col_w - 24, top, cx + col_w, top + 24,
                     lambda p=p: dismiss(p)))
        # RESUME control, top-LEFT, mirroring the dismiss "x" and drawn for the
        # same reason after the title: a long centred program name paints under
        # it. Absent entirely when there is no checkpoint (nothing to continue);
        # drawn DISABLED with the blocking condition in its own label when a
        # relaunch would start over instead of resuming. `p=p` binds THIS panel.
        r_label, r_enabled, _r_why = resume_state(p)
        if r_label:
            rw = (_measure(F_NOTE, r_label) or 40) + 14
            rx0, ry0 = cx + 6, top + 4
            rx1, ry1 = rx0 + rw, top + 20
            rcol = FIXCOL if r_enabled else DIM
            canvas.create_rectangle(rx0, ry0, rx1, ry1, outline=rcol,
                                    fill=BG, width=1)
            canvas.create_text((rx0 + rx1) / 2, (ry0 + ry1) / 2, text=r_label,
                               fill=rcol, font=F_NOTE)
            # A DISABLED button is still clickable on purpose: the click is how
            # the owner is told why it is disabled. It launches nothing.
            hits.append((rx0, ry0, rx1, ry1, lambda p=p: resume_program(p)))
        # ATTEMPT + LANDED line (2026-08-14). `cycle` counts cycles inside THIS
        # process, so it resets to 1 on every restart - after five restarts the
        # panel still read "cycle 1/12" while four earlier attempts had produced
        # nothing. That hides exactly what the honesty doctrine cares about. These
        # two numbers survive restarts: `attempt` (how many times this program has
        # been (re)launched + resumed) and `landed` (commits actually on the
        # branch - the DURABLE metric; the per-cycle "fixed" counter resets too).
        # The resume button's answer takes this line for a few seconds when
        # there is one: it is the only place a refusal ("pid N is still working
        # on this run") can be READ, and a control that refuses silently is the
        # same defect as one that silently starts over. It reverts to the
        # durable attempt/landed facts on its own.
        rnote = resume_note(p)
        att = attempt_info(p)
        if rnote:
            canvas.create_text(cx + col_w / 2, top + 47,
                               text=fit(rnote, col_w - 12, spec=F_NOTE),
                               fill=FIXCOL if _RESUMED.get(program_key(p))
                               else BUDGET, font=F_NOTE)
        elif att:
            canvas.create_text(cx + col_w / 2, top + 47, text=att[:46], fill=DIM,
                               font=("Segoe UI", 8))

        t = bar_targets(p)
        # Three bars side by side. Baseline lifted to leave room below for the
        # stat row, the severity breakdown, and the current-file footer.
        bar_area_top = top + 56
        # The error box owns the bottom ERR_BOX_H px of every panel, so the
        # bars and stats sit above it. max() keeps the bars from inverting
        # when the window is dragged short - a negative height drew the fill
        # upward, over the title.
        err_top = bottom - ERR_BOX_H
        base_y = err_top - 150
        bh = max(24.0, base_y - bar_area_top - 16)
        bw = min(46.0, (col_w - 4 * pad) / 3)
        gap = (col_w - 3 * bw) / 4
        bars = [
            ("review", t["review"], REVIEW, "Review", f"{t['review'] * 100:.0f}%"),
            ("fix", t["fix"], FIX, "Fix", f"{t['fix'] * 100:.0f}%"),
        ]
        cap = t["cap"]
        if cap:
            over = t["budget"] >= 0.999
            bars.append(("budget", t["budget"], BUDGET_OVER if over else BUDGET,
                         "Budget", f"${t['cost']:.2f}"))
        else:
            bars.append(("budget", 0.0, BUDGET, "Budget", f"${t['cost']:.2f}"))

        fixed = int(p.get("fixed") or 0)
        fix_total = int(p.get("fix_total") or 0)
        # The Fix bar's value text shows files completed / files to fix, so it
        # reads as "files", not a bare percentage that looks like defects.
        bars[1] = ("fix", bars[1][1], FIX, "Files fixed",
                   f"{fixed}/{fix_total}" if fix_total else "0/0")
        # SCOPE FIX (2026-08-14, owner caught it): `reviewed`/`files_total`/
        # `defects` are THIS BATCH (20 files); `fix_done`/`fix_total` are the
        # WHOLE PROGRAM (3,140 files). The Review bar therefore hit 100% while
        # 0.6% of the program had been reviewed, and "45 defects" next to
        # "/3140" implied 45 defects across the whole program. Same panel, two
        # scopes, unlabelled - a false impression of progress, which is exactly
        # what the honesty doctrine forbids. Label the batch bar as a batch.
        batch_n = int(p.get("files_total") or 0)
        if batch_n and fix_total and batch_n < fix_total:
            bars[0] = (bars[0][0], bars[0][1], bars[0][2], "Review (batch)",
                       f"{int(p.get('reviewed') or 0)}/{batch_n}")

        for j, (key, target, color, label, vtxt) in enumerate(bars):
            bx = cx + gap + j * (bw + gap)
            # Keyed by program IDENTITY, not by loop index: dismissing a
            # panel shifts every later program one column left, and an
            # index key would hand each of them the DISMISSED panel's eased
            # bar values to glide down from - a visibly wrong percentage on
            # panels that never changed.
            frac = ease((program_key(p), key), target)
            draw_bar(bx, base_y, bw, bh, frac, color, label, vtxt)

        # Stat row. Labels spell out units: "defects" are individual findings;
        # "files fixed" are whole files. The two are different counts.
        stats_y = base_y + 38
        defects = int(p.get("defects") or 0)
        defects_fixed = int(p.get("defects_fixed") or 0)
        errors = int(p.get("errors") or 0)
        # "defects found" counts ONLY the files reviewed in the current batch,
        # so say so - unqualified next to a /3140 denominator it read as a
        # whole-program total (owner, 2026-08-14: "3140 files ... yet only 45
        # defects found. That is quite a gap").
        scoped = (f"defects found: {defects}  (in {int(p.get('reviewed') or 0)} "
                  f"files reviewed so far)"
                  if batch_n and fix_total and batch_n < fix_total
                  else f"defects found: {defects}")
        canvas.create_text(cx + col_w / 2, stats_y, text=scoped,
                           fill=TEXT, font=("Segoe UI", 9, "bold"))

        # Severity breakdown: one colored "label N" chip per present severity,
        # in fixed order, centered. Only severities with a count are shown.
        sev = p.get("severity") or {}
        chips = [(s, int(sev.get(s) or 0)) for s in SEV_ORDER if sev.get(s)]
        sev_y = stats_y + 18
        if chips:
            labels = [f"{s} {n}" for s, n in chips]
            widths = [len(t_) * 6.5 + 14 for t_ in labels]
            total_w = sum(widths)
            sx = cx + (col_w - total_w) / 2
            for (s, n), lbl, wd in zip(chips, labels, widths):
                canvas.create_text(sx + wd / 2, sev_y, text=lbl,
                                   fill=SEV_COLOR.get(s, DIM),
                                   font=("Segoe UI", 8, "bold"))
                sx += wd
        else:
            canvas.create_text(cx + col_w / 2, sev_y, text="(no defects yet)",
                               fill=DIM, font=("Segoe UI", 8))

        # Progress in plain units: whole files fixed, and individual defects
        # addressed across those files (one fixed file resolves many defects).
        canvas.create_text(cx + col_w / 2, sev_y + 20,
                           text=f"files fixed: {fixed}/{fix_total}     "
                                f"defects fixed: {defects_fixed}",
                           fill=FIX, font=("Segoe UI", 9))
        cost_txt = f"     ${t['cost']:.2f} / ${cap:.0f} cap" if cap else f"     ${t['cost']:.2f}"
        # "file errors" = files that errored in review/fix. The box below
        # counts EVERY recorded failure (provider retries included). Naming
        # both is what keeps them from reading as one contradicting number.
        canvas.create_text(cx + col_w / 2, sev_y + 38,
                           text=f"file errors: {errors}{cost_txt}",
                           fill=ERRCOL if errors else DIM, font=("Segoe UI", 9))

        # Current file at the very bottom of the panel.
        cur = str(p.get("current_file") or "")
        cur_short = os.path.basename(cur) if cur else "-"
        canvas.create_text(cx + col_w / 2, err_top - 30, text="working on",
                           fill=DIM, font=("Segoe UI", 8))
        canvas.create_text(cx + col_w / 2, err_top - 14, text=cur_short[:40],
                           fill=TEXT, font=("Consolas", 9, "bold"))

        # ---------------- the error box -------------------------------- #
        # Three lines per entry, in the order the owner asked for them:
        # what failed, which code is responsible, what to do about it.
        info = errors_for(p)
        bx0, bx1 = cx + 8, cx + col_w - 8
        by0, by1 = err_top, bottom - 8
        inner = bx1 - bx0 - 16
        canvas.create_rectangle(bx0, by0, bx1, by1, outline=EDGE,
                                fill=ERRBOX, width=1)
        rows = info.get("rows") or []
        if info.get("available"):
            head = info.get("headline") or "no errors recorded"
        else:
            head = "error ledger unavailable"
        F_HEAD = ("Segoe UI", 8, "bold")
        F_KIND = ("Segoe UI", 8, "bold")
        F_MONO = ("Consolas", 8)
        F_FIX = ("Segoe UI", 8)
        canvas.create_text(bx0 + 8, by0 + 10, anchor="w",
                           text=fit(head, inner, spec=F_HEAD),
                           fill=ERRHEAD if rows else DIM, font=F_HEAD)
        ey = by0 + 26
        # How many entries actually FIT. Drawing a fixed three ran the last one
        # out of the bottom of the box on a short window and painted it over the
        # footer - so the count follows the geometry, and whatever does not fit
        # is COUNTED in the footer rather than silently dropped.
        footer_h = 16
        room = int(max(0.0, (by1 - footer_h) - ey) // ERR_ROW_H)
        drawn = rows[:room]

        # ---- copy button, top-right of this box ------------------------ #
        # Only when there is something to copy: a panel reading "nothing has
        # gone wrong yet" gets no button, so the box keeps its invariant of no
        # clickable region when there is nothing to act on (and the owner never
        # clicks a control that yields an empty paste).
        # Registered BEFORE the box-wide "open errors.md" hit added at the end
        # of this block: on_click fires the FIRST matching region, and the box
        # rectangle completely contains this one, so appending it later would
        # make the button unreachable.
        chx0 = bx1 - 6
        if rows:
            ckey = program_key(p)
            copied = _COPIED_UNTIL.get(ckey, 0.0) > time.monotonic()
            clabel = "copied!" if copied else "copy"
            cw = (_measure(F_NOTE, clabel) or 28) + 14
            chx1, chx0 = bx1 - 6, bx1 - 6 - cw
            chy0, chy1 = by0 + 2, by0 + 18
            canvas.create_rectangle(chx0, chy0, chx1, chy1,
                                    outline=FIXCOL if copied else EDGE,
                                    fill=BG, width=1)
            canvas.create_text((chx0 + chx1) / 2, (chy0 + chy1) / 2,
                               text=clabel, fill=FIXCOL if copied else DIM,
                               font=F_NOTE)
            hits.append((chx0, chy0, chx1, chy1,
                         lambda p=p, k=ckey, c=canvas: do_copy(c, p, k)))

        if drawn:
            # Sits left of the copy button, measured rather than guessed, so it
            # cannot paint over it at another DPI.
            canvas.create_text(chx0 - 8, by0 + 10, anchor="e",
                               text="newest first", fill=DIM, font=F_NOTE)
        for row in drawn:
            kcol = KIND_COLOR.get(row["kind"], DIM)
            canvas.create_text(bx0 + 8, ey, anchor="w",
                               text=fit(f"#{row['n']} {row['kind']} / {row['phase']}",
                                        inner, spec=F_KIND),
                               fill=kcol, font=F_KIND)
            canvas.create_text(bx0 + 8, ey + 12, anchor="w",
                               text=fit(row["error"], inner, spec=F_MONO),
                               fill=TEXT, font=F_MONO)
            canvas.create_text(bx0 + 8, ey + 24, anchor="w",
                               text=fit("code: " + row["where"], inner, spec=F_MONO),
                               fill=DIM, font=F_MONO)
            fix_txt = row["fix"] or "no known fix"
            # An unverified model guess is never dressed as a known fix.
            if row.get("fix_source") == "model":
                fix_txt = "(unverified) " + fix_txt
            canvas.create_text(bx0 + 8, ey + 36, anchor="w",
                               text=fit("fix: " + fix_txt, inner, spec=F_FIX),
                               fill=FIXCOL, font=F_FIX)
            ey += ERR_ROW_H
        if not rows:
            canvas.create_text((bx0 + bx1) / 2, (by0 + by1) / 2,
                               text="nothing has gone wrong yet"
                                    if info.get("available")
                                    else "flexfactor_errors.py not importable",
                               fill=DIM, font=F_FIX)
        # The box shows the newest few; the ledger holds all of them. One click
        # opens it rather than making the owner hunt for the path.
        md = info.get("md_path") or ""
        total = int(info.get("total") or 0)
        if md and total:
            hidden_n = total - len(drawn)
            note = (f"click for all {total} in errors.md" if hidden_n <= 0
                    else f"+{hidden_n} more - click for errors.md")
            canvas.create_text((bx0 + bx1) / 2, by1 - 8,
                               text=fit(note, inner, spec=F_NOTE),
                               fill=DIM, font=F_NOTE)
            hits.append((bx0, by0, bx1, by1, lambda md=md: open_ledger(md)))



def steering_targets(progs: list[dict]) -> list[tuple[str, str]]:
    """(display name, project dir) for every program that can be steered.

    A program with no resolved directory cannot be addressed: the steering
    journal is keyed on (program, canonical dir), so a blank dir would file the
    comment under a key no run will ever claim. Those are dropped here rather
    than accepted and silently lost."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for p in progs or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        pdir = str(p.get("dir") or "").strip()
        if not name or not pdir:
            continue
        key = (name.casefold(), os.path.normcase(os.path.abspath(pdir)))
        if key in seen:
            continue
        seen.add(key)
        out.append((name, pdir))
    return out


def submit_steering(name: str, project_dir: str, comment: str) -> tuple[bool, str]:
    """Record one steering comment. Returns (accepted, message-for-the-operator).

    Never raises: this runs on a Tk callback, and an exception there kills the
    redraw loop. Rejections (empty text, oversize, control characters, no
    steering module) are reported in the panel instead."""
    if _steer is None:
        return False, "steering module not available in this tree"
    text = str(comment or "").strip()
    if not text:
        return False, "type a comment first"
    if not name or not project_dir:
        return False, "no program selected"
    try:
        _steer.submit(name, project_dir, text, source="desktop-dashboard")
    except (ValueError, OSError) as e:
        return False, f"not accepted: {e}"
    return True, "queued - the running audit picks it up at its next checkpoint"


def steering_labels(targets: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(menu label, program, dir) with labels that cannot collide.

    Two audited directories can share a basename - ~/work/api and ~/spike/api -
    and the picker shows only the program name. Looking the directory back up by
    the displayed name then resolves BOTH entries to whichever was stored last,
    so choosing either menu row files the comment against one audit and leaves
    the other silently unsteered. When a name repeats, its directory goes in the
    label; unique names are left alone so the common case reads normally."""
    counts: dict[str, int] = {}
    for name, _pdir in targets:
        counts[name] = counts.get(name, 0) + 1
    out = []
    for name, pdir in targets:
        label = name if counts.get(name, 0) < 2 else f"{name}  [{pdir}]"
        out.append((label, name, pdir))
    return out


def steering_status_line(name: str, project_dir: str) -> str:
    """One-line backlog for the selected program, or '' when unavailable."""
    if _steer is None or not name or not project_dir:
        return ""
    try:
        summary = _steer.summary(name, project_dir)
    except (ValueError, OSError):
        return ""
    counts = summary.get("counts") or {}
    parts = [f"{k}: {v}" for k, v in sorted(counts.items()) if v]
    return f"steering ({summary.get('total', 0)}) " + ", ".join(parts) if parts else ""


def _main() -> int:
    import tkinter as tk

    root = tk.Tk()
    root.title("FlexFactor - Live Audit")
    root.configure(bg=BG)
    root.geometry("960x620")
    root.minsize(420, 420)

    # Steering panel FIRST (side="bottom"), so the canvas below it takes the
    # remaining space instead of squeezing the controls off-screen when the
    # window is small.
    steer_bar = tk.Frame(root, bg=BG)
    steer_bar.pack(side="bottom", fill="x", padx=8, pady=(0, 6))

    canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
    canvas.pack(side="top", fill="both", expand=True)

    steer_target = tk.StringVar(value="")
    steer_note = tk.StringVar(value="steer: waiting for a program")
    steer_targets_cache: list[tuple[str, str]] = []
    # label -> (program, dir). Keyed on the LABEL, which steering_labels
    # guarantees is unique, so two same-named programs stay distinguishable.
    steer_by_label: dict[str, tuple[str, str]] = {}

    tk.Label(steer_bar, text="Steer:", bg=BG, fg=DIM).pack(side="left")
    target_menu = tk.OptionMenu(steer_bar, steer_target, "")
    target_menu.configure(bg=BG, fg=TEXT, highlightthickness=0, activebackground=BG,
                          activeforeground=TEXT, width=16, anchor="w")
    target_menu["menu"].configure(bg=BG, fg=TEXT)
    target_menu.pack(side="left", padx=(6, 8))

    entry = tk.Entry(steer_bar, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                     relief="flat", highlightthickness=1,
                     highlightbackground=DIM, highlightcolor=TEXT)
    entry.pack(side="left", fill="x", expand=True, ipady=4)

    note = tk.Label(steer_bar, textvariable=steer_note, bg=BG, fg=DIM,
                    anchor="w", width=34)

    def do_send(_event=None) -> None:
        name, pdir = steer_by_label.get(steer_target.get(), ("", ""))
        ok, msg = submit_steering(name, pdir, entry.get())
        if ok:
            entry.delete(0, "end")
        steer_note.set(msg)

    send = tk.Button(steer_bar, text="Send", command=do_send, bg=PANEL, fg=TEXT,
                     activebackground=PANEL, activeforeground=TEXT, relief="flat",
                     padx=14)
    send.pack(side="left", padx=(8, 8))
    note.pack(side="left")
    entry.bind("<Return>", do_send)

    def refresh_targets(progs: list[dict]) -> None:
        """Keep the program picker in step with the run, without stomping on a
        selection the operator already made."""
        targets = steering_targets(progs)
        if targets == steer_targets_cache:
            return
        steer_targets_cache[:] = targets
        labelled = steering_labels(targets)
        steer_by_label.clear()
        steer_by_label.update({lab: (n, d) for lab, n, d in labelled})
        menu = target_menu["menu"]
        menu.delete(0, "end")
        for label, _n, _d in labelled:
            menu.add_command(label=label,
                             command=lambda lb=label: steer_target.set(lb))
        current = steer_target.get()
        if labelled and current not in steer_by_label:
            steer_target.set(labelled[0][0])
        elif not labelled:
            steer_target.set("")

    def refresh_note() -> None:
        name, pdir = steer_by_label.get(steer_target.get(), ("", ""))
        if not name:
            steer_note.set("steer: waiting for a program")
        else:
            steer_note.set(steering_status_line(name, pdir) or "steer: no comments yet")
        root.after(3000, refresh_note)

    refresh_note()

    # Per-(program, bar) eased display value so bars glide instead of jumping.
    shown: dict[tuple, float] = {}

    # Clickable regions, rebuilt every frame: (x0, y0, x1, y1, action).
    hits: list[tuple[float, float, float, float, object]] = []

    def on_click(event) -> None:
        for x0, y0, x1, y1, action in hits:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                action()
                return

    canvas.bind("<Button-1>", on_click)

    _targets_tick = [0.0]

    def redraw() -> None:
        canvas.delete("all")
        hits.clear()
        progs = read_status(STATUS_PATH)
        # Rebuilding the OptionMenu is Tk widget work; it must not run at 25 fps
        # (same no-work-per-frame rule as the black-screen-flash fix above).
        now = time.time()
        if now - _targets_tick[0] >= 1.0:
            _targets_tick[0] = now
            refresh_targets(progs)
        draw_frame(canvas, hits, shown,
                   canvas.winfo_width() or 960, canvas.winfo_height() or 620,
                   progs)
        root.after(40, redraw)  # ~25 fps for smooth bar glide

    redraw()
    root.mainloop()
    return 0


if __name__ == "__main__":
    # Headless self-test hook (no display): `python flexfactor_dashboard.py --selftest`
    if "--selftest" in sys.argv:
        sample = {"name": "GrantFlow", "files_total": 40, "reviewed": 40,
                  "fix_total": 39, "fix_done": 20, "cost": 6.5, "cap": 20.0,
                  "defects": 563, "fixed": 18, "errors": 2,
                  "severity": {"critical": 6, "high": 70, "medium": 230, "low": 240, "info": 17},
                  "current_file": "src/pages/Billing.jsx", "cycle": 1, "cycles": 3}
        print("bar_targets:", bar_targets(sample))
        chips = [(s, sample["severity"].get(s)) for s in SEV_ORDER if sample["severity"].get(s)]
        print("severity chips:", chips)
        print("read_status (missing file):", read_status("/no/such/file"))
        # Dismiss logic, headless (no display needed).
        stopped = {"name": "IPlay", "dir": "C:/Users/firer/Iplay", "done": True,
                   "phase": "STOPPED: baseline red", "reviewed": 0}
        both = [sample, stopped]
        assert len(visible_programs(both)) == 2, "nothing dismissed yet"
        dismiss(stopped)
        assert [p["name"] for p in visible_programs(both)] == ["GrantFlow"], \
            "dismissing must hide exactly one panel"
        assert len(both) == 2 and stopped["done"] is True, \
            "dismissing must not mutate or drop the run's own status data"
        print("dismiss (stopped program):", [p["name"] for p in visible_programs(both)])
        revived = dict(stopped, done=False, phase="reviewing", reviewed=3)
        assert not is_dismissed(revived), "new activity must un-hide a panel"
        restore_all()
        assert len(visible_programs(both)) == 2
        print("restore_all:", [p["name"] for p in visible_programs(both)])
        # Operator steering, headless. The box is the owner's live control over
        # a running audit, so its accept/reject contract is gated here rather
        # than only exercised by clicking it.
        import tempfile as _tf
        targets = steering_targets([sample, stopped,
                                    {"name": "NoDir", "dir": ""},
                                    dict(stopped)])
        assert [n for n, _ in targets] == ["IPlay"], targets
        assert submit_steering("IPlay", "C:/x", "   ")[0] is False,             "an empty comment must be refused, not queued"
        assert submit_steering("", "C:/x", "do the thing")[0] is False,             "a comment with no selected program must be refused"
        # Two audits can share a basename; the picker shows only the name, so a
        # label that repeats would steer whichever directory was stored last.
        collide = steering_labels([("api", "C:/work/api"), ("api", "C:/spike/api"),
                                   ("solo", "C:/solo")])
        assert len({lab for lab, _n, _d in collide}) == 3, collide
        assert [lab for lab, _n, _d in collide][2] == "solo",             "a unique name must stay unadorned"
        assert {d for _lab, _n, d in collide} == {
            "C:/work/api", "C:/spike/api", "C:/solo"}, collide
        print("steering labels:", [lab for lab, _n, _d in collide])
        if _steer is not None:
            _root = _tf.mkdtemp()
            _steer.DEFAULT_ROOT = _root
            ok, msg = submit_steering("IPlay", _root, "prioritize the auth bugs")
            assert ok, msg
            assert "pending: 1" in steering_status_line("IPlay", _root),                 "an accepted comment must show up as pending backlog"
            print("steering:", steering_status_line("IPlay", _root))
        sys.exit(0)
    sys.exit(_main())
