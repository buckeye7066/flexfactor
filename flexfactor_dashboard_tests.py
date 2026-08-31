#!/usr/bin/env python3
"""Tests for the live dashboard, and specifically for the PER-PROGRAM ERROR BOX.

Owner, 2026-08-23: "set the error reports for flexfactor as communication in a
box I can see below each program being run."

A box that says the wrong thing is worse than no box, so these tests do not stop
at the readers: they build a real Tk canvas, draw one frame with `draw_frame`,
and read the drawn text items back. Every assertion about "the owner can see X"
is therefore about text that was actually painted, not about a helper's return
value.

The rendering tests skip (never silently pass) when there is no display, e.g. a
headless CI container without Tk. The reader tests run everywhere.

    python flexfactor_dashboard_tests.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flexfactor_dashboard as dash  # noqa: E402
import flexfactor_errors as fe  # noqa: E402


def _entry(n, kind, phase, error, where=None, program_file="", route="",
           suggestion="do the thing", source="signature"):
    return {"n": n, "at": "2026-08-23T00:00:00+00:00", "phase": phase,
            "error": error, "detail": "", "responsible": where,
            "program_file": program_file, "route": route, "kind": kind,
            "suggestion": suggestion, "suggestion_source": source}


class TerminalLabelTests(unittest.TestCase):
    """The header label derives from the PHASE, never from `done` alone.

    done=True used to replace the phase text with a green "DONE"
    unconditionally, so a crashed program (audit_one_program's fatal handler
    publishes phase="error", done=True, error=...) rendered exactly like a
    success, and neither the word "error" nor the recorded error string was
    ever painted. Shared by BOTH dashboards so they cannot drift."""

    def test_a_crashed_program_is_not_a_green_done(self):
        label, kind = dash.terminal_label(
            {"phase": "error", "done": True,
             "error": "'str' object has no attribute 'get'"})
        self.assertEqual(kind, "error")
        self.assertIn("'str' object has no attribute 'get'", label)
        self.assertNotEqual(label, "DONE")

    def test_partial_and_stopped_keep_their_phase_text(self):
        label, kind = dash.terminal_label(
            {"phase": "done - partial (suite red)", "done": True})
        self.assertEqual(kind, "partial")
        self.assertIn("partial", label)
        label, kind = dash.terminal_label(
            {"phase": "STOPPED (incomplete) - repairs/verification pending",
             "done": False, "stopped": True})
        self.assertEqual(kind, "stopped")
        self.assertTrue(label.startswith("STOPPED"))

    def test_a_genuine_success_is_still_the_green_done(self):
        label, kind = dash.terminal_label({"phase": "done - verified", "done": True})
        self.assertEqual((label, kind), ("DONE", "done"))

    def test_a_running_program_keeps_its_phase(self):
        label, kind = dash.terminal_label({"phase": "fixing", "done": False})
        self.assertEqual((label, kind), ("fixing", "running"))


class LedgerReaderTests(unittest.TestCase):
    """flexfactor_errors' reader half - the single source both viewers use."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ff-dash-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _write(self, run_dir, entries):
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "errors.json"), "w", encoding="utf-8") as fh:
            json.dump({"program": "Demo", "entries": entries}, fh)

    def test_missing_or_broken_ledger_reads_as_empty_not_an_exception(self):
        self.assertEqual(fe.load_entries(""), [])
        self.assertEqual(fe.load_entries(os.path.join(self.root, "nope")), [])
        run = os.path.join(self.root, "torn")
        os.makedirs(run)
        with open(os.path.join(run, "errors.json"), "w", encoding="utf-8") as fh:
            fh.write('{"entries": [')          # a half-written file
        self.assertEqual(fe.load_entries(run), [])

    def test_headline_splits_by_kind_so_ours_is_distinguishable_from_the_providers(self):
        entries = [_entry(1, "provider", "rotation", "429"),
                   _entry(2, "provider", "rotation", "402"),
                   _entry(3, "flexfactor-defect", "review", "TypeError: boom")]
        head = fe.headline(entries)
        self.assertIn("3 errors", head)
        self.assertIn("1 flexfactor-defect", head)
        self.assertIn("2 provider", head)
        self.assertEqual(fe.headline([]), "no errors recorded")

    def test_ui_entries_are_newest_first_and_carry_the_three_asked_for_facts(self):
        entries = [_entry(i, "provider", "rotation", f"err {i}") for i in range(1, 6)]
        entries.append(_entry(6, "flexfactor-defect", "fix", "TypeError: boom",
                              where={"file": "flexfactor.py", "line": 42,
                                     "function": "apply_fix", "source": "x = 1"},
                              suggestion="pass the argv runner"))
        rows = fe.ui_entries(entries, 3)
        self.assertEqual([r["n"] for r in rows], ["6", "5", "4"])
        self.assertEqual(rows[0]["error"], "TypeError: boom")            # what failed
        self.assertEqual(rows[0]["where"], "flexfactor.py:42 apply_fix()")  # whose code
        self.assertEqual(rows[0]["fix"], "pass the argv runner")          # what to do

    def test_where_falls_back_through_program_file_then_route(self):
        self.assertEqual(fe.where_of(_entry(1, "program-defect", "fix", "e",
                                            program_file="src/app.js")), "src/app.js")
        self.assertEqual(fe.where_of(_entry(1, "provider", "rotation", "e",
                                            route="openrouter/free")),
                         "route openrouter/free")
        self.assertEqual(fe.where_of(_entry(1, "unknown", "x", "e")), "not attributable")

    def test_find_run_dir_picks_the_newest_run_for_that_program(self):
        old = os.path.join(self.root, "iplay-20260101-000000")
        new = os.path.join(self.root, "iplay-20260823-000000")
        other = os.path.join(self.root, "grantflow-20260823-000000")
        for d in (old, new, other):
            self._write(d, [_entry(1, "provider", "rotation", "e")])
        os.utime(os.path.join(old, "errors.json"), (1, 1))
        os.utime(os.path.join(new, "errors.json"), (10_000_000, 10_000_000))
        self.assertEqual(fe.find_run_dir("IPlay", runs_root=self.root), new)
        self.assertEqual(fe.find_run_dir("Nothing", runs_root=self.root), "")


class BoxDataTests(unittest.TestCase):
    """dashboard-side plumbing: which ledger a panel reads, and how it is cached."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ff-dash-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        dash._ERR_CACHE.clear()
        self.addCleanup(dash._ERR_CACHE.clear)

    def _run_dir(self, entries):
        run = os.path.join(self.root, "run-1")
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, "errors.json"), "w", encoding="utf-8") as fh:
            json.dump({"program": "Demo", "entries": entries}, fh)
        with open(os.path.join(run, "errors.md"), "w", encoding="utf-8") as fh:
            fh.write("# Errors")
        return run

    def test_status_run_dir_is_preferred_over_scanning_for_one(self):
        run = self._run_dir([_entry(1, "provider", "rotation", "429 rate limited")])
        info = dash.errors_for({"name": "Demo", "run_dir": run})
        self.assertEqual(info["total"], 1)
        self.assertTrue(info["md_path"].endswith("errors.md"))

    def test_a_program_with_no_ledger_reports_empty_rather_than_failing(self):
        info = dash.errors_for({"name": "NoSuchProgramAnywhere12345"})
        self.assertEqual(info["rows"], [])
        self.assertIn(info.get("headline", ""), ("", "no errors recorded"))

    def test_result_is_cached_so_a_25fps_redraw_does_not_read_the_disk_per_frame(self):
        run = self._run_dir([_entry(1, "provider", "rotation", "first")])
        p = {"name": "Demo", "run_dir": run}
        self.assertEqual(dash.errors_for(p)["total"], 1)
        self._run_dir([_entry(1, "provider", "rotation", "first"),
                       _entry(2, "provider", "rotation", "second")])
        self.assertEqual(dash.errors_for(p)["total"], 1, "should still be the cached read")
        dash._ERR_CACHE.clear()
        self.assertEqual(dash.errors_for(p)["total"], 2, "and refresh once the TTL is up")

    def test_an_unchanged_ledger_is_not_reparsed_after_the_ttl(self):
        # Past the TTL the cache stats the file and only re-reads when it moved.
        # Without that, a 6-hour run re-parses a growing JSON for every program
        # every couple of seconds to produce the identical three rows.
        run = self._run_dir([_entry(1, "provider", "rotation", "first")])
        p = {"name": "Demo", "run_dir": run}
        self.assertEqual(dash.errors_for(p)["total"], 1)
        calls = {"n": 0}
        real = dash._errors_uncached
        dash._errors_uncached = lambda pr: (calls.__setitem__("n", calls["n"] + 1)
                                            or real(pr))
        self.addCleanup(setattr, dash, "_errors_uncached", real)
        key = dash.program_key(p)
        exp, val, sig = dash._ERR_CACHE[key]
        dash._ERR_CACHE[key] = (0.0, val, sig)          # force the TTL open
        self.assertEqual(dash.errors_for(p)["total"], 1)
        self.assertEqual(calls["n"], 0, "an unchanged ledger must not be re-parsed")
        # ...but a ledger that GREW is re-read, or the box would freeze.
        self._run_dir([_entry(1, "provider", "rotation", "first"),
                       _entry(2, "provider", "rotation", "second")])
        exp, val, sig = dash._ERR_CACHE[key]
        dash._ERR_CACHE[key] = (0.0, val, sig)
        self.assertEqual(dash.errors_for(p)["total"], 2)
        self.assertEqual(calls["n"], 1)

    def test_fit_truncates_to_the_column_and_marks_that_it_did(self):
        long = "x" * 400
        out = dash.fit(long, 100)
        self.assertLess(len(out), 40)
        self.assertTrue(out.endswith("..."))
        self.assertEqual(dash.fit("short", 400), "short")


def _tk_or_skip():
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - headless machine, not a failure
        raise unittest.SkipTest(f"no Tk display available: {exc}")
    root.withdraw()
    return tk, root


class RenderedBoxTests(unittest.TestCase):
    """Draw a real frame and read back what is on it."""

    def setUp(self):
        self.tk, self.root = _tk_or_skip()
        self.addCleanup(self.root.destroy)
        self.canvas = self.tk.Canvas(self.root, width=960, height=620)
        self.tmp = tempfile.mkdtemp(prefix="ff-dash-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        dash._ERR_CACHE.clear()
        self.addCleanup(dash._ERR_CACHE.clear)

    def _ledger(self, entries):
        run = os.path.join(self.tmp, "run-x")
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, "errors.json"), "w", encoding="utf-8") as fh:
            json.dump({"program": "Demo", "entries": entries}, fh)
        with open(os.path.join(run, "errors.md"), "w", encoding="utf-8") as fh:
            fh.write("# Errors")
        return run

    def _draw(self, progs, w=960, h=620):
        hits: list = []
        self.canvas.delete("all")
        dash.draw_frame(self.canvas, hits, {}, w, h, progs, status_label="test")
        texts = [self.canvas.itemcget(i, "text") for i in self.canvas.find_all()
                 if self.canvas.type(i) == "text"]
        return texts, hits

    def test_the_box_shows_what_failed_which_code_and_the_fix(self):
        run = self._ledger([
            _entry(1, "provider", "rotation", "RateLimitError: 429 free-models-per-day",
                   route="openrouter/qwen", suggestion="the rotator cools the pool"),
            _entry(2, "flexfactor-defect", "fix",
                   "TypeError: unsupported parameter max_tokens",
                   where={"file": "flexfactor.py", "line": 2489,
                          "function": "structured", "source": "kwargs = {}"},
                   suggestion="send max_completion_tokens for api.openai.com"),
        ])
        texts, hits = self._draw([{"name": "Demo", "dir": self.tmp, "run_dir": run,
                                   "files_total": 10, "reviewed": 10, "cost": 0.0}])
        blob = " | ".join(texts)
        self.assertIn("2 errors", blob)                       # headline, with the split
        self.assertIn("1 flexfactor-defect", blob)
        self.assertIn("TypeError: unsupported parameter", blob)   # what failed
        self.assertIn("code: flexfactor.py:2489", blob)           # whose code
        self.assertIn("fix: send max_completion_tokens", blob)    # what to do
        self.assertTrue(any("errors.md" in t for t in texts))
        self.assertTrue(hits, "the box must be clickable to open errors.md")

    def test_newest_error_is_the_one_that_survives_a_narrow_column(self):
        entries = [_entry(i, "provider", "rotation", f"failure number {i}")
                   for i in range(1, 9)]
        run = self._ledger(entries)
        texts, _ = self._draw([{"name": "Demo", "run_dir": run}])
        blob = " | ".join(texts)
        self.assertIn("failure number 8", blob)
        self.assertNotIn("failure number 1 ", blob + " ")

    def test_a_clean_run_says_so_instead_of_looking_broken(self):
        run = self._ledger([])
        texts, hits = self._draw([{"name": "Clean", "run_dir": run}])
        blob = " | ".join(texts)
        self.assertIn("nothing has gone wrong yet", blob)
        self.assertIn("no errors recorded", blob)
        self.assertEqual([h for h in hits if h[4].__name__ == "<lambda>"
                          and h[1] > 400], [],
                         "no ledger link when there is nothing to open")

    def test_each_program_gets_its_own_box_with_its_own_errors(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        for d, msg in ((a, "AAA distinctive failure"), (b, "BBB distinctive failure")):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "errors.json"), "w", encoding="utf-8") as fh:
                json.dump({"program": os.path.basename(d),
                           "entries": [_entry(1, "provider", "rotation", msg)]}, fh)
        texts, _ = self._draw([{"name": "A", "run_dir": a}, {"name": "B", "run_dir": b}])
        blob = " | ".join(texts)
        self.assertIn("AAA distinctive", blob)
        self.assertIn("BBB distinctive", blob)

    def test_the_two_error_numbers_are_labelled_so_they_cannot_contradict(self):
        # `errors` in status.json counts FILES that errored; the ledger counts
        # every recorded failure. Unlabelled, 2 vs 40 reads as a bug in one of
        # them - the exact "two scopes side by side" defect this panel has a
        # standing rule against.
        run = self._ledger([_entry(i, "provider", "rotation", f"e{i}")
                            for i in range(1, 41)])
        texts, _ = self._draw([{"name": "Demo", "run_dir": run, "errors": 2}])
        blob = " | ".join(texts)
        self.assertIn("file errors: 2", blob)
        self.assertIn("40 errors", blob.replace("file errors: 2", ""))

    def test_the_panel_survives_a_window_too_short_for_everything(self):
        run = self._ledger([_entry(1, "provider", "rotation", "e")])
        texts, _ = self._draw([{"name": "Demo", "run_dir": run}], w=420, h=420)
        self.assertTrue(any("Demo" in t for t in texts))

    def _boxes(self):
        """(x0, y0, x1, y1) of every error box drawn in the last frame."""
        out = []
        for i in self.canvas.find_all():
            if self.canvas.type(i) != "rectangle":
                continue
            if self.canvas.itemcget(i, "fill") == dash.ERRBOX:
                out.append(tuple(self.canvas.coords(i)))
        return out

    def test_nothing_in_the_box_is_painted_outside_the_box(self):
        # The first screenshot of this feature (2026-08-23) had the fix line
        # running past the panel edge and over the next program, because the
        # truncation guessed character widths instead of measuring them.
        run = self._ledger([
            _entry(1, "flexfactor-defect", "fix",
                   "BadRequestError: Unsupported parameter 'max_tokens' is not "
                   "supported with this model, use max_completion_tokens instead",
                   where={"file": "flexfactor.py", "line": 2489,
                          "function": "structured", "source": "k = {}"},
                   suggestion="Newer OpenAI models reject max_tokens; send "
                              "max_completion_tokens for api.openai.com routes "
                              "(OpenAIProvider builds the kwargs in _chat)."),
            _entry(2, "provider", "rotation", "RateLimitError: 429 " + "x" * 300,
                   route="openrouter/" + "y" * 80, suggestion="z" * 400),
            _entry(3, "budget", "rotation", "APIStatusError: 402 " + "q" * 200,
                   route="openrouter/deepseek", suggestion="w" * 300),
        ])
        self._draw([{"name": "IPlay", "run_dir": run},
                    {"name": "GrantFlow", "run_dir": run}], w=1100, h=760)
        boxes = self._boxes()
        self.assertEqual(len(boxes), 2, "one box per program")
        for item in self.canvas.find_all():
            if self.canvas.type(item) != "text":
                continue
            x0, y0, x1, y1 = self.canvas.bbox(item)
            # Attribute by the text's CENTRE, not by y alone: every box shares
            # the same y band, so a y-only match blames panel A for panel B's
            # header and the assertion stops meaning anything.
            cx_, cy_ = (x0 + x1) / 2, (y0 + y1) / 2
            inside = [b for b in boxes
                      if b[1] - 2 <= cy_ <= b[3] + 2 and b[0] - 40 <= cx_ <= b[2] + 40]
            if not inside:
                continue                      # a panel label, not box content
            b = inside[0]
            txt = self.canvas.itemcget(item, "text")
            self.assertGreaterEqual(x0, b[0] - 1, f"paints left of its box: {txt!r}")
            self.assertLessEqual(x1, b[2] + 1, f"paints past its box: {txt!r}")

    def test_entries_that_do_not_fit_are_counted_not_dropped_silently(self):
        run = self._ledger([_entry(i, "provider", "rotation", f"e{i}")
                            for i in range(1, 21)])
        texts, _ = self._draw([{"name": "Demo", "run_dir": run}])
        blob = " | ".join(texts)
        self.assertIn("20 errors", blob)
        self.assertTrue(any("more" in t and "errors.md" in t for t in texts)
                        or any("all 20" in t for t in texts),
                        f"the box must account for what it could not show: {blob}")

    def test_a_long_message_is_cut_to_the_column_and_never_painted_full_width(self):
        run = self._ledger([_entry(1, "provider", "rotation", "Z" * 800)])
        texts, _ = self._draw([{"name": "A", "run_dir": run},
                               {"name": "B", "run_dir": run},
                               {"name": "C", "run_dir": run}])
        longest = max((t for t in texts if "ZZZ" in t), key=len)
        self.assertLess(len(longest), 80, "an untruncated error paints over the next panel")
        self.assertTrue(longest.endswith("..."))


class CopyPayloadTests(unittest.TestCase):
    """The text a copy click produces. Pure - no Tk, so it runs headless.

    Owner, 2026-08-24: "give me a 'copy' button by each of the error boxes that
    saves that information to my clipboard."
    """

    @staticmethod
    def _rows(n):
        return [{"n": i, "kind": "provider", "phase": "rotation",
                 "error": f"boom {i}", "where": f"flexfactor.py:{100 + i}",
                 "fix": f"fix {i}", "fix_source": "signature"}
                for i in range(1, n + 1)]

    def test_it_carries_the_three_facts_the_box_shows(self):
        out = dash.format_error_clipboard("Demo", "C:/run/errors.md",
                                          "2 errors: 2 provider",
                                          self._rows(2), 2)
        for expected in ("Demo", "2 errors: 2 provider", "C:/run/errors.md",
                         "boom 1", "flexfactor.py:101", "fix 1", "boom 2"):
            self.assertIn(expected, out)

    def test_an_unverified_model_guess_is_still_labelled_in_the_paste(self):
        rows = self._rows(1)
        rows[0]["fix_source"] = "model"
        out = dash.format_error_clipboard("Demo", "", "", rows, 1)
        self.assertIn("(unverified)", out,
                      "a model guess must not be pasted as a known fix")

    def test_truncation_is_announced_never_silent(self):
        out = dash.format_error_clipboard("Demo", "C:/run/errors.md", "",
                                          self._rows(10), 10, limit=4)
        self.assertIn("boom 4", out)
        self.assertNotIn("boom 5", out)
        self.assertIn("6 more of 10", out)
        self.assertIn("C:/run/errors.md", out,
                      "a truncated paste must still say where the rest lives")

    def test_no_rows_says_so_rather_than_pasting_an_empty_string(self):
        out = dash.format_error_clipboard("Demo", "", "", [], 0)
        self.assertIn("no errors recorded", out)
        self.assertTrue(out.strip())


class CopyButtonRenderTests(unittest.TestCase):
    """The button itself, drawn on a real canvas and read back."""

    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
            cls.root = tk.Tk()
            cls.root.withdraw()
            cls.canvas = tk.Canvas(cls.root, width=960, height=620)
        except Exception as ex:  # noqa: BLE001 - headless: skip, never pass quietly
            raise unittest.SkipTest(f"no display for Tk: {ex}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffcopy-")
        dash._ERR_CACHE.clear()
        dash._COPIED_UNTIL.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, entries):
        run = os.path.join(self.tmp, "run-c")
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, "errors.json"), "w", encoding="utf-8") as fh:
            json.dump({"program": "Demo", "entries": entries}, fh)
        with open(os.path.join(run, "errors.md"), "w", encoding="utf-8") as fh:
            fh.write("# Errors")
        return run

    def _draw(self, progs, w=960, h=620):
        hits: list = []
        self.canvas.delete("all")
        dash.draw_frame(self.canvas, hits, {}, w, h, progs, status_label="test")
        texts = [self.canvas.itemcget(i, "text") for i in self.canvas.find_all()
                 if self.canvas.type(i) == "text"]
        return texts, hits

    def test_a_box_with_errors_gets_a_copy_button(self):
        run = self._ledger([_entry(1, "provider", "rotation", "boom")])
        texts, _ = self._draw([{"name": "Demo", "run_dir": run}])
        self.assertIn("copy", texts)

    def test_a_box_with_nothing_wrong_gets_no_button(self):
        """No dead control, and the box keeps its no-clickable-region rule."""
        run = self._ledger([])
        texts, _ = self._draw([{"name": "Demo", "run_dir": run}])
        self.assertNotIn("copy", texts)

    def test_the_button_is_reachable_and_not_swallowed_by_the_box(self):
        """on_click fires the FIRST matching hit and the box CONTAINS the
        button, so registration order is the whole ballgame."""
        run = self._ledger([_entry(1, "provider", "rotation", "boom")])
        _, hits = self._draw([{"name": "Demo", "run_dir": run}])
        boxes = [h for h in hits if h[3] - h[1] > 100]      # the tall error box
        self.assertTrue(boxes, f"expected an error box region: {hits}")
        box = boxes[0]
        # Pick the button by CONTAINMENT in this box, not by size - the panel
        # also registers a 24x24 dismiss "x" that a bare size filter grabs.
        inside = [h for h in hits
                  if h is not box and box[0] <= h[0] and h[2] <= box[2]
                  and box[1] <= h[1] and h[3] <= box[3]]
        self.assertTrue(inside, f"no clickable region inside the error box: {hits}")
        btn = inside[0]
        self.assertLess(hits.index(btn), hits.index(box),
                        "the copy button must be registered BEFORE the box-wide "
                        "ledger hit or it can never be clicked")
        self.assertTrue(box[0] <= btn[0] and btn[2] <= box[2]
                        and box[1] <= btn[1] and btn[3] <= box[3],
                        "button should sit inside its own box")

    def test_the_button_does_not_paint_over_newest_first(self):
        run = self._ledger([_entry(1, "provider", "rotation", "boom")])
        texts, hits = self._draw([{"name": "Demo", "run_dir": run}])
        self.assertIn("newest first", texts)
        self.assertIn("copy", texts)

    def test_clicking_copies_EVERY_entry_not_just_the_three_painted(self):
        """The box paints ERR_ROWS; the paste is the whole ledger. That is the
        difference between a screenshot and something you can act on."""
        run = self._ledger([_entry(i, "provider", "rotation", f"boom{i}")
                            for i in range(1, 13)])
        prog = {"name": "Demo", "run_dir": run}
        texts, hits = self._draw([prog])
        painted = " | ".join(texts)
        # Entries render NEWEST FIRST, so the box shows #12/#11/#10 and the
        # OLDEST are the ones off-screen. Assert on one of those.
        self.assertNotIn("boom1 ", painted + " ",
                         "precondition: the oldest entries are not painted")

        payload = dash.error_clipboard_payload(prog)
        for i in range(1, 13):
            self.assertIn(f"boom{i}", payload,
                          f"entry {i} missing from the copied text")

    def test_the_click_action_actually_reaches_the_clipboard(self):
        run = self._ledger([_entry(1, "provider", "rotation", "clipboard-marker")])
        prog = {"name": "Demo", "run_dir": run}
        ok = dash.do_copy(self.canvas, prog, dash.program_key(prog))
        if not ok:
            self.skipTest("no working system clipboard in this environment")
        self.assertIn("clipboard-marker", self.root.clipboard_get())

    def test_a_successful_copy_shows_confirmation_on_the_next_frame(self):
        run = self._ledger([_entry(1, "provider", "rotation", "boom")])
        prog = {"name": "Demo", "run_dir": run}
        if not dash.do_copy(self.canvas, prog, dash.program_key(prog)):
            self.skipTest("no working system clipboard in this environment")
        texts, _ = self._draw([prog])
        self.assertIn("copied!", texts,
                      "a click with no visible result reads as a broken button")


if __name__ == "__main__":
    unittest.main(verbosity=2)
