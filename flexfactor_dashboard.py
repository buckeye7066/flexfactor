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
import sys

STATUS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.expanduser("~"), ".flexfactor", "status.json")

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
        progs = read_status(STATUS_PATH)
        W = canvas.winfo_width() or 960
        H = canvas.winfo_height() or 620

        canvas.create_text(14, 16, anchor="w", text="FlexFactor", fill=TEXT,
                           font=("Segoe UI", 14, "bold"))
        canvas.create_text(W - 14, 16, anchor="e",
                           text=f"watching {os.path.basename(STATUS_PATH)}",
                           fill=DIM, font=("Segoe UI", 8))

        if not progs:
            canvas.create_text(W / 2, H / 2, text="waiting for an audit to start...",
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
            name = str(p.get("name") or f"program {p.get('index', i + 1)}")
            phase = str(p.get("phase") or "")
            done = bool(p.get("done"))
            cyc = p.get("cycle")
            cycles = p.get("cycles")
            title_col = DONE if done else REVIEW
            canvas.create_text(cx + col_w / 2, top + 16, text=name[:34], fill=title_col,
                               font=("Segoe UI", 11, "bold"))
            sub = ("DONE" if done else phase) + (
                f"  (cycle {cyc}/{cycles})" if cyc and cycles and not done else "")
            canvas.create_text(cx + col_w / 2, top + 34, text=sub[:42], fill=DIM,
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
            canvas.create_text(cx + col_w / 2, stats_y,
                               text=f"defects found: {defects}",
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
        sys.exit(0)
    sys.exit(_main())
