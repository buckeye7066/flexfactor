#!/usr/bin/env python3
r"""
FlexFactor live dashboard, v2.

Answers the questions someone ACTUALLY has while watching a multi-hour audit:

  1. Is it alive, or has it wedged?          -> LIVE/STALLED pulse + velocity graph
  2. How far through the WHOLE program?      -> program bar (not the batch's)
  3. What has DURABLY landed?                -> commits on the branch
  4. How many attempts did this take?        -> survives restarts
  5. What is it doing right now, for how long?-> current file + time on it
  6. Is anything going wrong?                -> errors/timeouts, called out
  7. What is it costing?                     -> spend vs cap

Design rules learned the hard way on 2026-08-13/14, all from real misreads:

  * NEVER put two different SCOPES side by side unlabelled. The v1 panel showed
    "Review 100%" (of a 20-file BATCH) next to "10/3140" (of the PROGRAM), so it
    read as "fully reviewed" at 0.6% actual coverage.
  * ALWAYS show at least one number a restart cannot reset. v1 showed
    "cycle 1/12" after five attempts that had produced zero commits.
  * A quiet status file is NOT death - long fix loops legitimately go silent for
    20+ minutes. Judge liveness by the file's mtime AND by whether counters have
    moved, and say which.

Pure stdlib (tkinter). Read-only: it only ever reads status.json, the run
checkpoints and `git log` - it can never disturb a running audit.

    python flexfactor_dashboard_v2.py [path\to\status.json]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

STATUS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.expanduser("~"), ".flexfactor", "status.json")

BG      = "#0b0f14"
PANEL   = "#131a22"
EDGE    = "#243040"
TEXT    = "#e6edf3"
DIM     = "#7d8590"
ACCENT  = "#58a6ff"
GOOD    = "#3fb950"
WARN    = "#d29922"
BAD     = "#f85149"
TRACK   = "#1c2530"

SEV_ORDER = ["critical", "high", "medium", "low", "info"]
SEV_COLOR = {"critical": "#f85149", "high": "#ff7b72", "medium": "#d29922",
             "low": "#58a6ff", "info": "#7d8590"}

STALL_S = 20 * 60          # quiet longer than this -> say so (not "dead")
HIST_MAX = 120             # velocity samples kept (~1 per redraw tick)


def read_status(path: str) -> tuple[list[dict], float]:
    """(programs, mtime). [] on any problem so a partial write never crashes us."""
    try:
        mt = os.path.getmtime(path)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        progs = data.get("programs") or []
        return (progs if isinstance(progs, list) else []), mt
    except (OSError, ValueError):
        return [], 0.0


# Same black-screen-flash defence as flexfactor_dashboard.py (2026-08-16):
# this GUI runs under pythonw (no console), so a spawned console child without
# CREATE_NO_WINDOW flashes a brand-new black console window and steals focus -
# durable_facts() ran `git log` from redraw() at 2Hz. Every subprocess here
# passes _NO_WINDOW, and the disk/git walk sits behind a per-program TTL cache.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_FACTS_TTL_S = 5.0
_FACTS_CACHE: dict[str, tuple[float, dict]] = {}  # key -> (expires_at, facts)


def durable_facts(p: dict) -> dict:
    """The numbers that SURVIVE a restart, read straight off disk. No new
    status.json field needed, so this works against a running audit.

    TTL-cached: called from redraw(), and a render loop must never pay for a
    subprocess + directory walk per frame."""
    key = f"{p.get('name') or ''}|{p.get('dir') or ''}"
    hit = _FACTS_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    facts = _durable_facts_uncached(p)
    _FACTS_CACHE[key] = (time.monotonic() + _FACTS_TTL_S, facts)
    return facts


def _durable_facts_uncached(p: dict) -> dict:
    out = {"attempts": 0, "resumes": 0, "landed": None}
    try:
        prog = str(p.get("name") or "")
        slug = "".join(c.lower() if c.isalnum() else "-" for c in prog).strip("-")
        runs = os.path.join(os.path.expanduser("~"), ".flexfactor", "runs")
        if slug and os.path.isdir(runs):
            for d in os.listdir(runs):
                if d.startswith(slug + "-"):
                    out["attempts"] += 1
                    try:
                        with open(os.path.join(runs, d, "checkpoint.json"),
                                  encoding="utf-8") as fh:
                            out["resumes"] += int(json.load(fh).get("resume_count") or 0)
                    except (OSError, ValueError, TypeError):
                        pass
        proj = str(p.get("dir") or "")
        if proj and os.path.isdir(os.path.join(proj, ".git")):
            r = subprocess.run(["git", "-C", proj, "log", "--oneline", "--grep=FlexFactor"],
                               capture_output=True, text=True, timeout=5,
                               creationflags=_NO_WINDOW)
            if r.returncode == 0:
                out["landed"] = len([x for x in r.stdout.splitlines() if x.strip()])
    except Exception:
        pass
    return out


def human_eta(done: int, total: int, rate_per_min: float) -> str:
    if not total or done >= total or rate_per_min <= 0:
        return ""
    mins = (total - done) / rate_per_min
    if mins < 90:
        return f"~{mins:.0f}m left"
    hrs = mins / 60
    if hrs < 36:
        return f"~{hrs:.1f}h left"
    return f"~{hrs/24:.1f}d left"


def main() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("FlexFactor - Live Audit")
    root.configure(bg=BG)
    root.geometry("1120x760")
    canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    state = {
        "hist": {},        # name -> [(t, fix_done)]
        "file_since": {},  # name -> (filename, t_first_seen)
        "last_move": {},   # name -> (t, signature) last time a counter changed
        "pulse": 0.0,
    }

    def bar(x, y, w, h, frac, color, track=TRACK, radius=True):
        canvas.create_rectangle(x, y, x + w, y + h, outline="", fill=track)
        fw = max(0.0, min(1.0, frac)) * w
        if fw > 1:
            canvas.create_rectangle(x, y, x + fw, y + h, outline="", fill=color)

    def stacked(x, y, w, h, parts):
        """parts = [(count, color)]; draws one proportional segment each."""
        tot = sum(c for c, _ in parts) or 1
        cx = x
        canvas.create_rectangle(x, y, x + w, y + h, outline="", fill=TRACK)
        for c, col in parts:
            seg = w * (c / tot)
            if seg > 0.5:
                canvas.create_rectangle(cx, y, cx + seg, y + h, outline="", fill=col)
            cx += seg

    def sparkline(x, y, w, h, series, color):
        """Velocity over time. Flat line = nothing happening; that is the point."""
        canvas.create_rectangle(x, y, x + w, y + h, outline=EDGE, fill=PANEL)
        if len(series) < 2:
            canvas.create_text(x + w / 2, y + h / 2, text="collecting...",
                               fill=DIM, font=("Segoe UI", 8))
            return
        hi = max(series) or 1
        step = w / max(1, len(series) - 1)
        pts = []
        for i, v in enumerate(series):
            pts += [x + i * step, y + h - (v / hi) * (h - 6) - 3]
        canvas.create_line(*pts, fill=color, width=2, smooth=True)

    def redraw() -> None:
        canvas.delete("all")
        progs, mt = read_status(STATUS_PATH)
        W = canvas.winfo_width() or 1120
        H = canvas.winfo_height() or 760
        now = time.time()
        state["pulse"] = (state["pulse"] + 0.09) % 6.283

        canvas.create_text(18, 20, anchor="w", text="FlexFactor", fill=TEXT,
                           font=("Segoe UI", 16, "bold"))
        canvas.create_text(120, 22, anchor="w", text="live audit", fill=DIM,
                           font=("Segoe UI", 9))

        if not progs:
            canvas.create_text(W / 2, H / 2, text="waiting for an audit to start...",
                               fill=DIM, font=("Segoe UI", 13))
            root.after(500, redraw)
            return

        quiet = now - mt if mt else 1e9
        live = quiet < STALL_S
        # Breathing dot: unmistakable at a glance, and it stops when the file does.
        import math
        r = 5 + (2.2 * abs(math.sin(state["pulse"])) if live else 0)
        dot = GOOD if live else WARN
        canvas.create_oval(W - 210 - r, 22 - r, W - 210 + r, 22 + r, fill=dot, outline="")
        canvas.create_text(W - 196, 22, anchor="w",
                           text=("LIVE" if live else f"QUIET {quiet/60:.0f}m"),
                           fill=dot, font=("Segoe UI", 10, "bold"))
        canvas.create_text(W - 18, 22, anchor="e", text=time.strftime("%H:%M:%S"),
                           fill=DIM, font=("Segoe UI", 9))

        n = len(progs)
        pad = 16
        col_w = (W - pad * (n + 1)) / n
        top, bottom = 46, H - pad

        for i, p in enumerate(progs):
            x0 = pad + i * (col_w + pad)
            canvas.create_rectangle(x0, top, x0 + col_w, bottom, outline=EDGE,
                                    fill=PANEL, width=1)
            L = x0 + 18          # left text margin
            inner_w = col_w - 36
            name = str(p.get("name") or f"program {i+1}")
            fix_done = int(p.get("fix_done") or 0)
            fix_total = int(p.get("fix_total") or 0)
            batch_n = int(p.get("files_total") or 0)
            reviewed = int(p.get("reviewed") or 0)
            defects = int(p.get("defects") or 0)
            defects_fixed = int(p.get("defects_fixed") or 0)
            errors = int(p.get("errors") or 0)
            cost = float(p.get("cost") or 0.0)
            cap = float(p.get("cap") or 0.0)
            cur = str(p.get("current_file") or "")
            evidence = p.get("evidence") if isinstance(p.get("evidence"), dict) else {}

            # ---- velocity history + real liveness (counters, not just mtime)
            h = state["hist"].setdefault(name, [])
            if not h or h[-1][1] != fix_done:
                h.append((now, fix_done))
                state["last_move"][name] = now
            elif len(h) and now - h[-1][0] > 20:
                h.append((now, fix_done))
            del h[:-HIST_MAX]
            rate = 0.0
            if len(h) >= 2:
                dt = (h[-1][0] - h[0][0]) / 60.0
                if dt > 0.2:
                    rate = max(0.0, (h[-1][1] - h[0][1]) / dt)
            series = []
            for j in range(1, len(h)):
                dt = max(1e-6, (h[j][0] - h[j-1][0]) / 60.0)
                series.append(max(0.0, (h[j][1] - h[j-1][1]) / dt))

            # ---- header. Label/colour derive from the PHASE via the SHARED
            # terminal_label (flexfactor_dashboard) - `done` alone painted a
            # crashed program (phase="error", done=True) as a green DONE.
            from flexfactor_dashboard import terminal_label as _tl
            label, kind = _tl(p)
            canvas.create_text(L, top + 24, anchor="w", text=name[:38],
                               fill=(GOOD if kind == "done" else
                                     BAD if kind == "error" else ACCENT),
                               font=("Segoe UI", 15, "bold"))
            d = durable_facts(p)
            att = f"attempt {d['attempts']}" + (f" (+{d['resumes']} resumes)" if d["resumes"] else "")
            canvas.create_text(x0 + col_w - 18, top + 24, anchor="e", text=att,
                               fill=DIM, font=("Segoe UI", 9))
            canvas.create_text(L, top + 46, anchor="w",
                               text=label[:46],
                               fill=(GOOD if kind == "done" else
                                     BAD if kind == "error" else TEXT),
                               font=("Segoe UI", 10))

            y = top + 76

            # ---- 1. PROGRAM progress (the honest denominator)
            canvas.create_text(L, y, anchor="w", text="PROGRAM", fill=DIM,
                               font=("Segoe UI", 8, "bold"))
            pct = (fix_done / fix_total * 100) if fix_total else 0.0
            canvas.create_text(x0 + col_w - 18, y, anchor="e",
                               text=f"{fix_done:,} / {fix_total:,} files  ({pct:.1f}%)",
                               fill=TEXT, font=("Segoe UI", 10, "bold"))
            bar(L, y + 12, inner_w, 16, (fix_done / fix_total) if fix_total else 0, ACCENT)
            eta = human_eta(fix_done, fix_total, rate)
            if eta:
                canvas.create_text(x0 + col_w - 18, y + 40, anchor="e",
                                   text=eta, fill=DIM, font=("Segoe UI", 9))
            canvas.create_text(L, y + 40, anchor="w",
                               text=f"{rate:.1f} files/min",
                               fill=(GOOD if rate > 0 else WARN),
                               font=("Segoe UI", 9, "bold"))
            y += 60

            # ---- 2. VELOCITY (flat = wedged, and you can SEE it)
            canvas.create_text(L, y, anchor="w", text="VELOCITY", fill=DIM,
                               font=("Segoe UI", 8, "bold"))
            sparkline(L, y + 10, inner_w, 46, series, GOOD if rate > 0 else WARN)
            y += 68

            # ---- 3. THIS BATCH (scope named, so it can't be misread as the program)
            canvas.create_text(L, y, anchor="w", text="CURRENT BATCH", fill=DIM,
                               font=("Segoe UI", 8, "bold"))
            canvas.create_text(x0 + col_w - 18, y, anchor="e",
                               text=f"{reviewed} / {batch_n} files reviewed",
                               fill=TEXT, font=("Segoe UI", 10))
            bar(L, y + 12, inner_w, 10, (reviewed / batch_n) if batch_n else 0, ACCENT)
            y += 34

            # ---- 4. DEFECTS, scope-qualified
            canvas.create_text(L, y, anchor="w",
                               text=f"DEFECTS  {defects} found in {reviewed} files reviewed",
                               fill=DIM, font=("Segoe UI", 8, "bold"))
            sev = p.get("severity") or {}
            parts = [(int(sev.get(s) or 0), SEV_COLOR[s]) for s in SEV_ORDER if sev.get(s)]
            if parts:
                stacked(L, y + 12, inner_w, 14, parts)
                chips = "   ".join(f"{s} {int(sev.get(s))}" for s in SEV_ORDER if sev.get(s))
                canvas.create_text(L, y + 36, anchor="w", text=chips, fill=DIM,
                                   font=("Segoe UI", 8))
            else:
                bar(L, y + 12, inner_w, 14, 0, GOOD)
                canvas.create_text(L, y + 36, anchor="w", text="no defects yet",
                                   fill=DIM, font=("Segoe UI", 8))
            y += 56

            # ---- 5. LANDED - the only number a restart cannot fake
            landed = d["landed"]
            canvas.create_text(L, y, anchor="w", text="LANDED ON BRANCH", fill=DIM,
                               font=("Segoe UI", 8, "bold"))
            canvas.create_text(L, y + 22, anchor="w",
                               text=(f"{landed} commit{'' if landed == 1 else 's'}"
                                     if landed is not None else "-"),
                               fill=GOOD, font=("Segoe UI", 17, "bold"))
            canvas.create_text(L + 150, y + 24, anchor="w",
                               text=f"{defects_fixed} defects fixed",
                               fill=DIM, font=("Segoe UI", 9))
            # errors sit right next to it: never let a failure hide behind a win
            canvas.create_text(x0 + col_w - 18, y + 22, anchor="e",
                               text=(f"{errors} not fixed" if errors else "0 failures"),
                               fill=(BAD if errors else DIM),
                               font=("Segoe UI", 10, "bold"))
            y += 50

            # ---- 6. COST
            frac_cost = (cost / cap) if cap else 0.0
            canvas.create_text(L, y, anchor="w", text="SPEND", fill=DIM,
                               font=("Segoe UI", 8, "bold"))
            canvas.create_text(x0 + col_w - 18, y, anchor="e",
                               text=(f"${cost:,.2f} / ${cap:,.0f}" if cap else f"${cost:,.2f}"),
                               fill=TEXT, font=("Segoe UI", 10))
            bar(L, y + 12, inner_w, 8,
                frac_cost, BAD if frac_cost >= 0.9 else (WARN if frac_cost >= 0.5 else GOOD))
            y += 34

            # ---- 7. EXACT EXECUTABLE EVIDENCE
            if evidence:
                gates = evidence.get("gates") or {}
                cov = evidence.get("coverage") or {}
                impact = evidence.get("impact") or {}
                gate_ok = gates.get("passed") is True
                canvas.create_text(L, y, anchor="w", text="EVIDENCE",
                                   fill=DIM, font=("Segoe UI", 8, "bold"))
                canvas.create_text(x0 + col_w - 18, y, anchor="e",
                                   text=("VERIFIED" if gate_ok else "INCOMPLETE"),
                                   fill=(GOOD if gate_ok else BAD),
                                   font=("Segoe UI", 9, "bold"))
                canvas.create_text(L, y + 18, anchor="w",
                                   text=(f"gates {gates.get('pass', 0)} pass / "
                                         f"{gates.get('fail', 0)} fail / "
                                         f"{gates.get('blocked', 0)} blocked"),
                                   fill=TEXT, font=("Segoe UI", 9))
                canvas.create_text(L, y + 34, anchor="w",
                                   text=(f"functions DIRECT {cov.get('functions_direct', cov.get('functions_executed', 0))}/"
                                         f"{cov.get('functions', 0)}  routes "
                                         f"{cov.get('routes_executed', 0)}/{cov.get('routes', 0)}  "
                                         f"controls {cov.get('controls_executed', 0)}/"
                                         f"{cov.get('controls', 0)}  impact "
                                         f"{impact.get('affected_files', 0)} files"),
                                   fill=DIM, font=("Segoe UI", 8))
                y += 16
                canvas.create_text(L, y + 34, anchor="w",
                                   text=(f"purpose: {evidence.get('purpose_confidence') or '?'}"
                                         + ("" if evidence.get('purpose_mutation_authorized') is None else
                                            ("  gap-fixes AUTHORIZED" if evidence.get('purpose_mutation_authorized')
                                             else "  gap-fixes NOT authorized"))),
                                   fill=TEXT, font=("Segoe UI", 9))
                y += 16
                # Render the sandbox's own SHORT headline, never a slice of the
                # long claim: the long form names the OS-enforced mechanisms
                # first, so cutting it to fit this row deleted the "network is
                # NOT contained" half. Older evidence records (written before
                # `containment_headline` existed) fall back to the full claim.
                _contain = str(evidence.get("containment_headline")
                               or evidence.get("containment") or "unknown")
                canvas.create_text(L, y + 34, anchor="w",
                                   text="containment: " + _contain,
                                   fill=WARN if "NOT" in _contain else TEXT,
                                   font=("Segoe UI", 9))
                wip = evidence.get("wip") or {}
                if wip.get("snapshot_ref"):
                    y += 16
                    canvas.create_text(L, y + 34, anchor="w",
                                       text=(f"owner WIP: {wip.get('snapshot_ref')}  "
                                             f"{wip.get('restore') or 'attached'}")[:110],
                                       fill=WARN if "RETAINED" in str(wip.get("restore") or "") else TEXT,
                                       font=("Segoe UI", 9))
                if evidence.get("blocked_reason"):
                    y += 16
                    canvas.create_text(L, y + 34, anchor="w",
                                       text=("BLOCKED: " + str(evidence.get("blocked_reason")))[:110],
                                       fill=BAD, font=("Segoe UI", 9, "bold"))
                y += 46

            # ---- 8. CURRENT FILE + how long it has been stuck on it
            if cur:
                prev = state["file_since"].get(name)
                if not prev or prev[0] != cur:
                    state["file_since"][name] = (cur, now)
                held = now - state["file_since"][name][1]
                warn = held > 15 * 60      # past FIX_FILE_MAX_SECONDS -> notable
                canvas.create_text(L, y + 6, anchor="w", text="WORKING ON", fill=DIM,
                                   font=("Segoe UI", 8, "bold"))
                canvas.create_text(L, y + 26, anchor="w",
                                   text=os.path.basename(cur)[:44], fill=TEXT,
                                   font=("Consolas", 11, "bold"))
                canvas.create_text(x0 + col_w - 18, y + 26, anchor="e",
                                   text=f"{held/60:.0f}m on this file",
                                   fill=(WARN if warn else DIM),
                                   font=("Segoe UI", 9, "bold" if warn else "normal"))

        root.after(500, redraw)

    redraw()
    root.mainloop()


def _selftest() -> int:
    p = {"name": "Demo", "dir": "", "phase": "fixing (cycle 1/12)", "reviewed": 20,
         "files_total": 20, "defects": 45, "fix_done": 11, "fix_total": 3140,
         "cost": 0.81, "cap": 150.0, "errors": 2, "defects_fixed": 5,
         "severity": {"critical": 9, "high": 9, "medium": 15, "low": 11, "info": 1},
         "current_file": "src/pages/Billing.jsx"}
    print("durable_facts:", durable_facts(p))
    print("eta @2/min   :", human_eta(11, 3140, 2.0))
    print("eta @0/min   :", repr(human_eta(11, 3140, 0.0)))
    print("read_status  :", read_status("nope.json"))
    assert human_eta(11, 3140, 0.0) == ""
    assert human_eta(3140, 3140, 5) == ""
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    main()
