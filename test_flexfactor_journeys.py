"""Tests for the FlexFactor journey engine (flexfactor_journeys.py + flexfactor_explorer.js).

    python test_flexfactor_journeys.py

Unit tests need nothing but Python. The integration tests start the fixture app
(eval_fixtures/journeys/app.js) and drive the real explorer under Playwright;
when no usable playwright install exists they skip with a BLOCKED reason that
names every path tried - never a silent pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import flexfactor_journeys as fj  # noqa: E402

FIXTURE = os.path.join(HERE, "eval_fixtures", "journeys", "app.js")
EXPLORER_TIMEOUT_S = 300
PLAYWRIGHT_NODE_MODULES_CANDIDATES = [
    *([os.environ["FLEXFACTOR_PLAYWRIGHT_NODE_MODULES"]] if os.environ.get("FLEXFACTOR_PLAYWRIGHT_NODE_MODULES") else []),
    os.path.join(HERE, "node_modules"),
    r"C:\Users\firer\GrantFlow\node_modules",
    os.path.join(os.path.expanduser("~"), ".eva-playwright", "node_modules"),
]


# --------------------------------------------------------------------------- unit
class ExplorerScriptPathTests(unittest.TestCase):
    def test_resolves_next_to_module(self):
        p = fj.explorer_script_path()
        self.assertTrue(os.path.isfile(p), p)
        self.assertEqual(os.path.basename(p), "flexfactor_explorer.js")
        # Shipped as package data of flexfactor_assets (one source, in the wheel).
        self.assertEqual(os.path.basename(os.path.dirname(p)), "flexfactor_assets")

    def test_script_parses_as_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("BLOCKED: node not on PATH")
        cp = subprocess.run([node, "--check", fj.explorer_script_path()], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)


class JourneyEnvTests(unittest.TestCase):
    def test_defaults(self):
        env = fj.journey_env(None, False, None, None)
        self.assertEqual(env["FLEXFACTOR_E2E_ISOLATED"], "0")
        self.assertEqual(env["FLEXFACTOR_E2E_MAX_PAGES"], "500")
        self.assertEqual(env["FLEXFACTOR_E2E_VIEWPORTS"], "1280x800,390x844")
        self.assertNotIn("FLEXFACTOR_E2E_ROLES", env)

    def test_roles_isolated_viewports_and_cap(self):
        roles = [{"name": "admin", "login": {"url": "/login", "fields": {"#u": "a"}, "submit": "button"}}]
        env = fj.journey_env(roles, True, ["1024x768"], 7)
        self.assertEqual(env["FLEXFACTOR_E2E_ISOLATED"], "1")
        self.assertEqual(env["FLEXFACTOR_E2E_MAX_PAGES"], "7")
        self.assertEqual(env["FLEXFACTOR_E2E_VIEWPORTS"], "1024x768")
        self.assertEqual(json.loads(env["FLEXFACTOR_E2E_ROLES"]), roles)
        for v in env.values():
            self.assertIsInstance(v, str)

    def test_rejects_bad_viewport_and_nameless_role(self):
        with self.assertRaises(ValueError):
            fj.journey_env(None, False, ["wide"], None)
        with self.assertRaises(ValueError):
            fj.journey_env([{"login": {}}], False, None, None)


class ParseResultTests(unittest.TestCase):
    def test_extracts_marker_line_among_noise(self):
        out = "npm warn something\n[explorer] hello\nFLEXFACTOR_E2E_RESULT={\"pages\": 3, \"complete\": false}\ntrailing\n"
        self.assertEqual(fj.parse_result(out), {"pages": 3, "complete": False})

    def test_returns_none_without_marker_or_with_garbage(self):
        self.assertIsNone(fj.parse_result(""))
        self.assertIsNone(fj.parse_result("FLEXFACTOR_E2E_RESULT={not json"))
        self.assertIsNone(fj.parse_result(None))

    def test_last_parseable_marker_wins(self):
        out = "FLEXFACTOR_E2E_RESULT={\"pages\": 1}\nFLEXFACTOR_E2E_RESULT={\"pages\": 2}\nFLEXFACTOR_E2E_RESULT={bad"
        self.assertEqual(fj.parse_result(out), {"pages": 2})


class SummaryAndCompletenessTests(unittest.TestCase):
    def _result(self, **over):
        base = {
            "pages": 2, "errors": [], "skipped": [], "incomplete_reasons": [], "complete": True,
            "journeys": [
                {"id": "j1", "kind": "route", "role": "anonymous", "viewport": "1280x800", "target": "/", "status": "passed"},
                {"id": "j2", "kind": "form", "role": "admin", "viewport": "1280x800", "target": "POST /x", "status": "passed"},
            ],
            "authorization_matrix": [{"route": "/", "role": "anonymous", "outcome": "permitted"}, {"route": "/admin", "role": "anonymous", "outcome": "denied"}],
            "findings": [{"kind": "authz-suspect"}], "formEvidence": [],
        }
        base.update(over)
        return base

    def test_summary_counts(self):
        s = fj.journey_matrix_summary(self._result())
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_status"], {"passed": 2})
        self.assertEqual(s["by_kind"], {"route": {"passed": 1}, "form": {"passed": 1}})
        self.assertEqual(s["by_role"], {"anonymous": {"passed": 1}, "admin": {"passed": 1}})
        self.assertEqual(s["authorization"], {"anonymous:permitted": 1, "anonymous:denied": 1})
        self.assertEqual(dict(s["findings"]), {"authz-suspect": 1})
        self.assertTrue(s["complete"])

    def test_complete_when_nothing_skipped(self):
        ok, reasons = fj.completeness(self._result())
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_named_skip_breaks_completeness(self):
        r = self._result(skipped=["form /contact not submitted: FLEXFACTOR_E2E_ISOLATED not set"], complete=False)
        ok, reasons = fj.completeness(r)
        self.assertFalse(ok)
        self.assertTrue(any("FLEXFACTOR_E2E_ISOLATED" in x for x in reasons), reasons)

    def test_page_cap_breaks_completeness(self):
        r = self._result(incomplete_reasons=["page cap 3 reached; 4 discovered routes unvisited"], complete=False)
        ok, reasons = fj.completeness(r)
        self.assertFalse(ok)
        self.assertIn("page cap 3 reached; 4 discovered routes unvisited", reasons)

    def test_failed_journey_and_errors_break_completeness(self):
        r = self._result(errors=["/x: console: boom"], complete=False)
        r["journeys"].append({"id": "j3", "kind": "control", "role": "anonymous", "viewport": "1280x800", "target": "/ \"Go\"", "status": "failed", "reason": "click timeout"})
        ok, reasons = fj.completeness(r)
        self.assertFalse(ok)
        self.assertTrue(any("failed journey j3" in x for x in reasons), reasons)
        self.assertTrue(any("explorer error" in x for x in reasons), reasons)

    def test_complete_false_without_reason_is_itself_named(self):
        ok, reasons = fj.completeness(self._result(complete=False))
        self.assertFalse(ok)
        self.assertTrue(any("without a named reason" in x for x in reasons), reasons)

    def test_slow_pages_and_a11y_are_findings_not_gaps(self):
        r = self._result(findings=[{"kind": "performance", "durationMs": 9000}, {"kind": "accessibility", "detail": "image missing alt"}],
                         performance={"slow": [{"url": "/x", "durationMs": 9000}]}, accessibility={"violations": [{"url": "/x"}]})
        ok, reasons = fj.completeness(r)
        self.assertTrue(ok, reasons)

    def test_action_timeouts_break_completeness(self):
        r = self._result(timeouts=["re-login admin @390x844"], complete=False)
        ok, reasons = fj.completeness(r)
        self.assertFalse(ok)
        self.assertIn("timeout: re-login admin @390x844", reasons)

    def test_no_payload(self):
        ok, reasons = fj.completeness(None)
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 1)


# -------------------------------------------------------------------- integration
def _find_playwright_node_modules() -> tuple[str | None, list[str]]:
    tried = []
    for nm in PLAYWRIGHT_NODE_MODULES_CANDIDATES:
        pkg = os.path.join(nm, "playwright", "package.json")
        tried.append(pkg)
        if os.path.isfile(pkg):
            return nm, tried
    npm = shutil.which("npm")
    if npm:
        try:
            cp = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            root = (cp.stdout or "").strip()
            pkg = os.path.join(root, "playwright", "package.json")
            tried.append(pkg)
            if root and os.path.isfile(pkg):
                return root, tried
        except Exception as ex:  # pragma: no cover
            tried.append(f"npm root -g failed: {ex}")
    return None, tried


def _chromium_available(node: str, node_modules: str) -> tuple[bool, str]:
    probe = "const {chromium}=require('playwright');const p=chromium.executablePath();console.log(p);process.exit(require('fs').existsSync(p)?0:3)"
    env = {**os.environ, "NODE_PATH": node_modules}
    cp = subprocess.run([node, "-e", probe], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
    return cp.returncode == 0, (cp.stdout or "").strip() + (cp.stderr or "").strip()


class _Fixture:
    def __init__(self, node: str):
        self.proc = subprocess.Popen([node, FIXTURE, "0"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        deadline = time.time() + 20
        self.port = None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError("fixture exited before listening")
                continue
            if line.startswith("FIXTURE_LISTENING="):
                self.port = int(line.split("=", 1)[1].strip())
                break
        if self.port is None:
            self.stop()
            raise RuntimeError("fixture never reported its port")
        self.base = f"http://127.0.0.1:{self.port}/"

    def get_json(self, path: str):
        with urllib.request.urlopen(self.base.rstrip("/") + path, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if self.proc.stdout:
            self.proc.stdout.close()


def _diagnose(r: dict) -> str:
    """Everything CI needs to see when a completeness assertion fails."""
    kinds = {}
    for f in r.get("findings") or []:
        kinds[f.get("kind")] = kinds.get(f.get("kind"), 0) + 1
    failed = [j for j in r.get("journeys") or [] if j.get("status") != "passed"]
    return json.dumps({
        "complete": r.get("complete"), "incomplete_reasons": r.get("incomplete_reasons"), "skipped": r.get("skipped"),
        "summary": r.get("summary"), "timeouts": r.get("timeouts"), "errors": (r.get("errors") or [])[:20],
        "finding_kinds": kinds, "performance_slow": (r.get("performance") or {}).get("slow"),
        "accessibility_violations": (r.get("accessibility") or {}).get("violations"),
        "non_passed_journeys": failed[:20], "elapsedMs": r.get("elapsedMs"),
    }, indent=1, default=str)


class ExplorerIntegrationTests(unittest.TestCase):
    """Drives the real explorer against the fixture app under Playwright."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.node_modules, cls.tried = _find_playwright_node_modules()
        cls.blocked = None
        if not cls.node:
            cls.blocked = "BLOCKED: node not on PATH"
        elif not cls.node_modules:
            cls.blocked = f"BLOCKED: no playwright install found: {cls.tried}"
        else:
            ok, detail = _chromium_available(cls.node, cls.node_modules)
            if not ok:
                cls.blocked = f"BLOCKED: playwright found at {cls.node_modules} but chromium is not installed ({detail}); PLAYWRIGHT_BROWSERS_PATH={os.environ.get('PLAYWRIGHT_BROWSERS_PATH')}"

    def setUp(self):
        if self.blocked:
            self.skipTest(self.blocked)
        self.fixture = _Fixture(self.node)
        self.addCleanup(self.fixture.stop)
        self.artifacts = tempfile.mkdtemp(prefix="flexfactor-journeys-")
        self.addCleanup(shutil.rmtree, self.artifacts, True)

    def _run_explorer(self, roles, isolated, max_pages=None, base=None, extra_env=None, timeout=EXPLORER_TIMEOUT_S):
        env = {**os.environ, "NODE_PATH": self.node_modules, **fj.journey_env(roles, isolated, None, max_pages), **(extra_env or {})}
        started = time.time()
        try:
            cp = subprocess.run([self.node, fj.explorer_script_path(), base or self.fixture.base, self.artifacts],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        except subprocess.TimeoutExpired as ex:
            err = ex.stderr.decode("utf-8", "replace") if isinstance(ex.stderr, bytes) else (ex.stderr or "")
            self.fail(f"explorer exceeded {timeout}s (isolated={isolated}); last progress lines:\n{err[-3000:]}")
        elapsed = time.time() - started
        print(f"\n[explorer run] isolated={isolated} max_pages={max_pages} rc={cp.returncode} elapsed={elapsed:.1f}s", flush=True)
        if os.environ.get("FLEXFACTOR_E2E_DEBUG_LOG"):  # keep the explorer progress log for CI forensics
            with open(os.environ["FLEXFACTOR_E2E_DEBUG_LOG"], "a", encoding="utf-8") as fh:
                header = "===== isolated=%s max_pages=%s rc=%s elapsed=%.1fs" % (isolated, max_pages, cp.returncode, elapsed)
                fh.write(chr(10) + header + chr(10) + (cp.stderr or ""))
        result = fj.parse_result((cp.stdout or "") + "\n" + (cp.stderr or ""))
        self.assertIsNotNone(result, f"no FLEXFACTOR_E2E_RESULT line; rc={cp.returncode}\nstderr tail: {(cp.stderr or '')[-1500:]}")
        return cp, result, elapsed

    ADMIN_ROLE = {"name": "admin", "login": {"url": "/login", "fields": {"#user": "admin", "#pass": "admin"}, "submit": "button[type=submit]"}}

    def test_isolated_run_executes_every_journey_and_is_complete(self):
        cp, r, elapsed = self._run_explorer([self.ADMIN_ROLE], isolated=True)
        self.assertLessEqual(elapsed, EXPLORER_TIMEOUT_S)
        port = str(self.fixture.port)

        def cell(route_suffix, role):
            rows = [m for m in r["authorization_matrix"] if m["route"].endswith(route_suffix) and m["role"] == role]
            self.assertEqual(len(rows), 1, (route_suffix, role, r["authorization_matrix"]))
            return rows[0]

        # authorization matrix: admin permitted, anonymous denied on /admin
        self.assertEqual(cell(f"{port}/admin", "admin")["outcome"], "permitted")
        self.assertEqual(cell(f"{port}/admin", "anonymous")["outcome"], "denied")
        self.assertEqual(cell(f"{port}/admin", "anonymous")["httpStatus"], 403)
        self.assertEqual(sorted(r["roles"]), ["admin", "anonymous"])
        login_rows = [j for j in r["journeys"] if j["kind"] == "login"]
        self.assertEqual([j["status"] for j in login_rows], ["passed"], login_rows)
        # no authz-suspect: /admin is properly protected
        self.assertEqual([f for f in r["findings"] if f["kind"] == "authz-suspect"], [])

        # contact form: real submission, backend state verified via /contact/list
        contact = [f for f in r["formEvidence"] if f["action"] == "/contact"]
        self.assertEqual(len(contact), 1, r["formEvidence"])
        contact = contact[0]
        self.assertEqual(contact["status"], "submitted")
        cases = {c["mode"]: c for c in contact["cases"]}
        self.assertEqual(cases["valid"]["httpStatus"], 201, cases["valid"])
        email = cases["valid"]["data"]["email"]
        self.assertTrue(email.startswith("flexfactor+") and email.endswith("@example.invalid"), email)
        backend = self.fixture.get_json("/contact/list")
        self.assertIn(email, [x["email"] for x in backend["received"]], backend)
        # duplicate submission recorded + rejected by the backend (409)
        self.assertEqual(cases["duplicate"]["httpStatus"], 409, cases["duplicate"])
        self.assertTrue(cases["duplicate"]["rejected"])
        dup = [f for f in r["findings"] if f["kind"] == "duplicate-submission"]
        self.assertEqual(len(dup), 1, r["findings"])
        self.assertEqual(dup[0]["form"], "/contact")
        self.assertTrue(dup[0]["rejected"])
        # failure matrix recorded with backend statuses + observed validation messages
        self.assertEqual(cases["empty"]["httpStatus"], 400)
        self.assertTrue(any("Missing required fields" in m for m in cases["empty"]["observedValidation"]), cases["empty"])
        self.assertTrue(any("fill out this field" in m for m in cases["empty"]["observedValidation"]), cases["empty"])
        self.assertEqual(cases["oversized"]["httpStatus"], 413)
        self.assertEqual(cases["malformed-email"]["httpStatus"], 400)
        self.assertTrue(any("Invalid email" in m for m in cases["malformed-email"]["observedValidation"]), cases["malformed-email"])
        for mode in ("valid", "empty", "oversized", "malformed-email", "duplicate"):
            self.assertEqual(cases[mode]["status"], "passed", cases[mode])
            self.assertTrue(cases[mode].get("screenshot"), mode)
        # login form ran its matrix too; malformed-email is not applicable (no email field) and is NOT a gap
        login_form = [f for f in r["formEvidence"] if f["action"] == "/login"][0]
        self.assertEqual({c["mode"]: c["status"] for c in login_form["cases"]},
                         {"valid": "passed", "empty": "passed", "oversized": "passed", "malformed-email": "not-applicable"})

        # destructive form executed in isolation, and the backend shows it
        danger = [f for f in r["formEvidence"] if f["action"] == "/delete-all"][0]
        self.assertEqual(danger["status"], "submitted-destructive")
        self.assertEqual(danger["cases"][0]["httpStatus"], 200)
        self.assertEqual(backend["deletes"], 1)
        self.assertEqual(backend["submissions"], [])  # wiped AFTER the contact journeys ran

        # viewports: both rendered for every route; overflow finding at 390px on /wide
        self.assertEqual(r["viewports"], ["1280x800", "390x844"])
        vp_rows = [j for j in r["journeys"] if j["kind"] == "viewport"]
        self.assertEqual({j["viewport"] for j in vp_rows}, {"1280x800", "390x844"})
        self.assertEqual(len(vp_rows), 2 * r["pages"])
        self.assertTrue(all(j["screenshot"] for j in vp_rows))
        overflow = [f for f in r["findings"] if f["kind"] == "horizontal-overflow" and f["viewport"] == "390x844"]
        self.assertEqual(len(overflow), 1, r["findings"])
        self.assertTrue(overflow[0]["route"].endswith("/wide"))
        self.assertGreater(overflow[0]["scrollWidth"], overflow[0]["innerWidth"])

        # non-form control clicked for both roles
        self.assertEqual(r["controls"], 2, r["controlEvidence"])
        self.assertEqual({c["label"] for c in r["controlEvidence"]}, {"Toggle panel"})

        # journey rows are well-formed and the run is complete
        for j in r["journeys"]:
            for k in ("id", "kind", "role", "viewport", "target", "status"):
                self.assertIn(k, j, j)
            self.assertIn(j["status"], ("passed", "failed", "skipped"))
        diag = _diagnose(r)
        if not r.get("complete"):
            print(chr(10) + "[explorer diagnostics]" + chr(10) + diag, flush=True)
        self.assertEqual(r["summary"]["total"], len(r["journeys"]), diag)
        self.assertEqual(r["summary"]["failed"], 0, diag)
        self.assertEqual(r["summary"]["skipped"], 0, diag)
        self.assertEqual(r.get("timeouts"), [], diag)
        self.assertEqual(r["errors"], [], diag)
        self.assertEqual(r["skipped"], [], diag)
        self.assertEqual(r["incomplete_reasons"], [], diag)
        self.assertTrue(r["complete"], diag)
        self.assertEqual(cp.returncode, 0, diag)
        expected_kinds = {"login", "route", "control", "form", "form-case", "duplicate", "destructive", "viewport"}
        present_kinds = {j["kind"] for j in r["journeys"]}
        self.assertTrue(expected_kinds <= present_kinds, "missing journey kinds: %s %s %s" % (sorted(expected_kinds - present_kinds), chr(10), diag))
        # slow pages / a11y violations (host speed, heuristics) are findings, never completeness blockers
        for f in r["findings"]:
            self.assertIn(f["kind"], {"authz-suspect", "validation-gap", "duplicate-submission", "horizontal-overflow", "performance", "accessibility", "timeout"}, f)
        ok, reasons = fj.completeness(r)
        self.assertTrue(ok, reasons)
        # artifacts exist on disk: screenshots + one trace per role
        for name in r["artifacts"]:
            self.assertTrue(os.path.isfile(os.path.join(self.artifacts, name)), name)
        self.assertTrue({"playwright-trace-anonymous.zip", "playwright-trace-admin.zip"} <= set(r["artifacts"]), r["artifacts"])
        summary = fj.journey_matrix_summary(r)
        self.assertEqual(summary["by_status"], {"passed": len(r["journeys"])})

    def test_non_isolated_run_names_every_skip_and_is_incomplete(self):
        cp, r, elapsed = self._run_explorer([self.ADMIN_ROLE], isolated=False, max_pages=3)
        self.assertLessEqual(elapsed, EXPLORER_TIMEOUT_S)
        self.assertFalse(r["complete"])
        self.assertEqual(cp.returncode, 1)
        # page cap named, never silent
        self.assertTrue(any(x.startswith("page cap 3 reached;") and "unvisited" in x for x in r["incomplete_reasons"]), r["incomplete_reasons"])
        self.assertEqual(r["pages"], 3)
        # forms were NOT submitted: constraints only + named skip
        forms = {f["action"]: f for f in r["formEvidence"]}
        self.assertIn("/login", forms)
        self.assertEqual(forms["/login"]["status"], "constraints-executed")
        self.assertEqual(forms["/login"]["cases"], [])
        self.assertIn("validEmpty", forms["/login"])
        self.assertTrue(any(s == "form /login not submitted: FLEXFACTOR_E2E_ISOLATED not set" for s in r["skipped"]), r["skipped"])
        backend = self.fixture.get_json("/contact/list")
        self.assertEqual(backend["received"], [])  # nothing reached the backend
        self.assertEqual(backend["deletes"], 0)
        skipped_rows = [j for j in r["journeys"] if j["status"] == "skipped"]
        self.assertTrue(skipped_rows)
        self.assertTrue(all(j.get("reason") for j in skipped_rows), skipped_rows)
        ok, reasons = fj.completeness(r)
        self.assertFalse(ok)
        self.assertTrue(any("page cap" in x for x in reasons), reasons)
        self.assertTrue(any("FLEXFACTOR_E2E_ISOLATED not set" in x for x in reasons), reasons)

    def test_run_watchdog_emits_result_on_hanging_route(self):
        # /hang never answers (fixture holds the socket); the whole-run watchdog must still produce a result
        cp, r, elapsed = self._run_explorer([], isolated=False, base=self.fixture.base + "hang-index",
                                            extra_env={"FLEXFACTOR_E2E_RUN_TIMEOUT_MS": "20000"}, timeout=90)
        self.assertLess(elapsed, 45, f"watchdog did not bound the run: {elapsed:.1f}s")
        self.assertEqual(cp.returncode, 1)
        self.assertFalse(r["complete"])
        self.assertIn("run timeout", r["incomplete_reasons"], r["incomplete_reasons"])
        self.assertEqual(r["runTimeoutMs"], 20000)
        self.assertGreaterEqual(r["elapsedMs"], 20000)
        # evidence gathered before the hang survives: the index page was visited
        self.assertTrue(any(j["kind"] == "route" and j["target"].endswith("/hang-index") for j in r["journeys"]), r["journeys"])
        ok, reasons = fj.completeness(r)
        self.assertFalse(ok)
        self.assertIn("run timeout", reasons)
        self.assertEqual(self.fixture.get_json("/contact/list")["hanging"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
