#!/usr/bin/env python3
"""Each program's errors land in THAT program's ledger, even with --parallel.

`--parallel N` audits several programs on threads of ONE process. The error
ledger used to be a bare process-global set by whichever program started last,
so on a five-program night four panels would have shown an empty box and the
fifth would have shown everyone's failures. The dashboard's new per-program
error box makes that mis-filing visible as a lie, so the routing is now a
ContextVar plus `_CtxThreadPoolExecutor`, and these tests pin both halves.

    python flexfactor_ledger_routing_tests.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load_flexfactor():
    """Hermetic import, and NEVER against the owner's real state.

    Same contract as flexfactor_tests.py: the module writes brain.json /
    status.json / runs under ~/.flexfactor, and a test run that touched those
    has already destroyed real project memory once (2026-08-11).
    """
    if "flexfactor" in sys.modules:
        return sys.modules["flexfactor"]
    tmp = tempfile.mkdtemp(prefix="ff-route-state-")
    spec = importlib.util.spec_from_file_location(
        "flexfactor", os.path.join(HERE, "flexfactor.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["flexfactor"] = module          # BEFORE exec_module
    spec.loader.exec_module(module)
    module.BRAIN_PATH = os.path.join(tmp, "brain.json")
    module.STATUS_PATH = os.path.join(tmp, "status.json")
    module.RUNS_PATH = os.path.join(tmp, "runs")
    module._auto_activate_fcc_proxy = lambda *a, **k: None
    return module


ff = _load_flexfactor()
import flexfactor_errors as fe  # noqa: E402


class _Checkpoint:
    def __init__(self, path):
        self.path = path


class LedgerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ff-route-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = ff._ERROR_LEDGER
        def restore():
            ff._ERROR_LEDGER = self._saved
            ff._ERROR_LEDGER_VAR.set(None)
        self.addCleanup(restore)

    def _open(self, name):
        run = os.path.join(self.tmp, name)
        os.makedirs(run, exist_ok=True)
        return ff._start_error_ledger(_Checkpoint(os.path.join(run, "checkpoint.json")), name)

    def test_a_single_program_still_records_through_the_global(self):
        led = self._open("solo")
        ff._ledger("review", "TypeError: boom")
        self.assertEqual(len(led.entries), 1)
        self.assertEqual(len(fe.load_entries(led.run_dir)), 1, "written to disk")

    def test_two_programs_on_two_threads_do_not_share_a_ledger(self):
        opened = {}
        done = threading.Barrier(3, timeout=30)

        def program(name, message):
            led = self._open(name)
            opened[name] = led
            done.wait()                        # both ledgers open before either records
            ff._ledger("review", message)

        threads = [threading.Thread(target=program, args=("alpha", "AAA failure")),
                   threading.Thread(target=program, args=("beta", "BBB failure"))]
        for t in threads:
            t.start()
        done.wait()
        for t in threads:
            t.join(timeout=30)

        a, b = opened["alpha"], opened["beta"]
        self.assertEqual([e["error"] for e in a.entries], ["AAA failure"])
        self.assertEqual([e["error"] for e in b.entries], ["BBB failure"])

    def test_work_submitted_to_a_pool_files_under_the_submitting_program(self):
        # This is the half a plain ThreadPoolExecutor breaks: a pool worker
        # starts with an EMPTY context, so without _CtxThreadPoolExecutor the
        # review/fix tasks would fall back to the last-opened global ledger.
        results = {}
        start = threading.Barrier(2, timeout=30)

        def program(name, message):
            led = self._open(name)
            results[name] = led
            start.wait()                       # force both globals to have been set
            with ff._CtxThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda i: ff._ledger("fix", f"{message} {i}"), range(3)))

        threads = [threading.Thread(target=program, args=("alpha", "AAA")),
                   threading.Thread(target=program, args=("beta", "BBB"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        a_errors = [e["error"] for e in results["alpha"].entries]
        b_errors = [e["error"] for e in results["beta"].entries]
        self.assertEqual(len(a_errors), 3, a_errors)
        self.assertEqual(len(b_errors), 3, b_errors)
        self.assertTrue(all(e.startswith("AAA") for e in a_errors), a_errors)
        self.assertTrue(all(e.startswith("BBB") for e in b_errors), b_errors)

    def test_a_plain_pool_is_what_breaks_it(self):
        # Verification of the verification: prove the context copy is doing the
        # work, by showing the same shape mis-files with a stock executor.
        import concurrent.futures as cf
        a = self._open("alpha2")
        ff._ERROR_LEDGER_VAR.set(a)
        b = self._open("beta2")          # global now points at beta2
        ff._ERROR_LEDGER_VAR.set(a)      # ...but THIS thread is still alpha2
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ff._ledger, "fix", "mis-filed").result()
        self.assertEqual(len(a.entries), 0, "a stock pool loses the context")
        self.assertEqual(len(b.entries), 1, "and falls back to the global")
        with ff._CtxThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ff._ledger, "fix", "correctly filed").result()
        self.assertEqual([e["error"] for e in a.entries], ["correctly filed"])

    def test_the_report_line_names_this_programs_ledger(self):
        a = self._open("alpha3")
        ff._ledger("review", "AAA failure")
        b = self._open("beta3")            # a later program opens its own
        ff._ERROR_LEDGER_VAR.set(a)        # ...this thread is still alpha3
        line = ff._error_ledger_report_line()
        self.assertIn("1 (see the Errors section below", line)
        self.assertIn("alpha3", line)
        self.assertNotIn("beta3", line, "the report must not cite another run")

    def test_nothing_reads_the_bare_global_any_more(self):
        """Every reader must go through _current_error_ledger().

        A direct `_ERROR_LEDGER` read is last-writer-wins, which is the whole
        defect: it silently attributes one program's failures to another. Only
        the definition and the assignment inside _start_error_ledger may name it.
        """
        with open(os.path.join(HERE, "flexfactor.py"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        offenders = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "_ERROR_LEDGER" not in stripped:
                continue
            if "_ERROR_LEDGER_VAR" in stripped or "_ERROR_LEDGER_LOCK" in stripped:
                continue
            if stripped in ("_ERROR_LEDGER = None", "global _ERROR_LEDGER",
                            "_ERROR_LEDGER = led"):
                continue
            offenders.append(f"{i}: {stripped}")
        self.assertEqual(offenders, [],
                         "read the ledger through _current_error_ledger(): "
                         + "; ".join(offenders))

    def test_recording_before_any_ledger_opens_is_a_no_op_not_a_crash(self):
        ff._ERROR_LEDGER = None
        ff._ERROR_LEDGER_VAR.set(None)
        ff._ledger("setup", "nothing is open yet")     # must not raise

    def test_the_audit_publishes_the_run_dir_so_the_dashboard_can_find_it(self):
        # The dashboard prefers status.json's run_dir over scanning for one.
        # This pins the publishing call site, which is otherwise easy to drop.
        with open(os.path.join(HERE, "flexfactor.py"), encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("_error_ledger = _start_error_ledger(checkpoint, display_name)")
        window = src[i:i + 700]
        self.assertIn("report(run_dir=_error_ledger.run_dir", window)
        self.assertIn("errors_ledger=_error_ledger.md_path", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
