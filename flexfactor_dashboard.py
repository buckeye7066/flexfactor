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


def _main() -> int:
    import tkinter as tk

    root = tk.Tk()
    root.title("FlexFactor - Live Audit")
    root.configure(bg=BG)
    root.geometry("960x620")
    root.minsize(420, 420)

    canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
    canvas.pack(fill="both", expand=True)

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

    def redraw() -> None:
        canvas.delete("all")
        hits.clear()
        all_progs = read_status(STATUS_PATH)
        progs = visible_programs(all_progs)
        hidden = len(all_progs) - len(progs)
        W = canvas.winfo_width() or 960
        H = canvas.winfo_height() or 620

        canvas.create_text(14, 16, anchor="w", text="FlexFactor", fill=TEXT,
                           font=("Segoe UI", 14, "bold"))
        canvas.create_text(W - 14, 16, anchor="e",
                           text=f"watching {os.path.basename(STATUS_PATH)}",
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
            root.after(40, redraw)
            return
        if not progs:
            canvas.create_text(W / 2, H / 2, text="all panels dismissed - click "
                                                  "the line above to show them",
                               fill=DIM, font=("Segoe UI", 12))
            root.after(40, redraw)
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
            # Dismiss control. `p=p` binds THIS program - a bare closure over the
            # loop variable would hand every "x" the last panel drawn.
            canvas.create_text(cx + col_w - 12, top + 12, text="x", fill=DIM,
                               font=("Segoe UI", 11, "bold"))
            hits.append((cx + col_w - 24, top, cx + col_w, top + 24,
                         lambda p=p: dismiss(p)))
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
            # ATTEMPT + LANDED line (2026-08-14). `cycle` counts cycles inside THIS
            # process, so it resets to 1 on every restart - after five restarts the
            # panel still read "cycle 1/12" while four earlier attempts had produced
            # nothing. That hides exactly what the honesty doctrine cares about. These
            # two numbers survive restarts: `attempt` (how many times this program has
            # been (re)launched + resumed) and `landed` (commits actually on the
            # branch - the DURABLE metric; the per-cycle "fixed" counter resets too).
            att = attempt_info(p)
            if att:
                canvas.create_text(cx + col_w / 2, top + 47, text=att[:46], fill=DIM,
                                   font=("Segoe UI", 8))

            t = bar_targets(p)
            # Three bars side by side. Baseline lifted to leave room below for the
            # stat row, the severity breakdown, and the current-file footer.
            bar_area_top = top + 56
            base_y = bottom - 150
            bh = base_y - bar_area_top - 16
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
                frac = ease((i, key), target)
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
            canvas.create_text(cx + col_w / 2, sev_y + 38,
                               text=f"errors: {errors}{cost_txt}",
                               fill=ERRCOL if errors else DIM, font=("Segoe UI", 9))

            # Current file at the very bottom of the panel.
            cur = str(p.get("current_file") or "")
            cur_short = os.path.basename(cur) if cur else "-"
            canvas.create_text(cx + col_w / 2, bottom - 30, text="working on",
                               fill=DIM, font=("Segoe UI", 8))
            canvas.create_text(cx + col_w / 2, bottom - 14, text=cur_short[:40],
                               fill=TEXT, font=("Consolas", 9, "bold"))

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
        sys.exit(0)
    sys.exit(_main())
