#!/usr/bin/env python3
"""Executable pins for the three capabilities added 2026-08-25.

  A  read-only PRODUCTION evidence  - enforced in code, not by convention
  B  SCHEMA DISCOVERY before SQL    - a hallucinated column is rejected, named
  F  NON-CODE verdicts              - reported to the owner, NEVER patched

Every test here drives the REAL call path (the module's own functions, and
FlexFactor's real `should_fix_finding` / `_normalize_finding` / report writer).
A test that only asserts a constant exists proves nothing - that is the exact
"wired but unreachable" trap this repo has hit four times.

Offline: no database, no network, no provider. The session takes an injected
`connect`, and the judge is a scripted fake.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flexfactor_evidence as ev  # noqa: E402
import flexfactor_prodevidence as pe  # noqa: E402
import flexfactor_scout_contract as sc  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
SCHEMA_ROWS = [
    ("user_sessions", "id", "uuid"),
    ("user_sessions", "user_id", "uuid"),
    ("user_sessions", "user_agent", "text"),
    ("user_sessions", "refresh_count", "integer"),
    ("user_sessions", "created_at", "timestamp with time zone"),
    ("profiles", "id", "uuid"),
    ("profiles", "primary_email", "text"),
    ("profiles", "sponsor", "text"),
    ("profiles", "match_decision", "text"),
]


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows: list[tuple] = []

    def execute(self, sql):
        self.conn.executed.append(sql)
        low = sql.strip().lower()
        if low.startswith("set "):
            self.conn.session_sets.append(sql)
            self._rows = []
            self.description = None
            return
        if "information_schema.columns" in low:
            self._rows = list(SCHEMA_ROWS)
            self.description = [("table_name",), ("column_name",), ("data_type",)]
            return
        rows = self.conn.results.get_for(sql)
        if isinstance(rows, Exception):
            raise rows
        self._rows = rows
        self.description = [("client",), ("sessions",), ("rotations",)]

    def fetchall(self):
        return list(self._rows)


class _Results:
    """Answers any probe with the SAME rows unless a specific script is set."""

    def __init__(self, default=None, script=None):
        self.default = default if default is not None else [
            ("FB_IAB", 41, 0), ("Chrome", 380, 10)]
        self.script = dict(script or {})

    def get_for(self, sql):
        for needle, rows in self.script.items():
            if needle in sql:
                return rows
        return self.default


class _Conn:
    def __init__(self, results=None):
        self.executed: list[str] = []
        self.session_sets: list[str] = []
        self.results = results or _Results()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _judge_script(*payloads):
    """Returns a callable answering each judge() call from the script in turn."""
    calls = {"n": 0, "prompts": []}
    seq = list(payloads)

    def judge(system, prompt, schema):
        calls["prompts"].append(prompt)
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        out = seq[i]
        if isinstance(out, Exception):
            raise out
        return out

    judge.calls = calls
    return judge


PLAN_OK = {"queries": [{
    "name": "sessions_by_client",
    "question": "Do sessions from one client family fail to rotate refresh tokens?",
    "sql": "SELECT user_agent, count(*) AS sessions, sum(refresh_count) AS rotations "
           "FROM user_sessions GROUP BY user_agent"}]}

VERDICT_OK = {"findings": [{
    "category": "client",
    "severity": "high",
    "title": "Facebook in-app browser never rotates its refresh cookie",
    "problem": "Sessions whose user_agent contains FB_IAB are signed out every 3h.",
    "evidence": "FB_IAB: 41 sessions, 0 refresh rotations; Chrome: 380 sessions, 10.",
    "query_name": "sessions_by_client",
    "next_step": "Set sameSite=lax on the refresh cookie, or open the link in the "
                 "system browser."}]}


# --------------------------------------------------------------------------- #
# A. THE READ-ONLY PERIMETER
# --------------------------------------------------------------------------- #
class ReadOnlyPerimeterTests(unittest.TestCase):

    def test_a_plain_select_is_accepted_and_clamped_on_its_own_line(self):
        out = pe.clamp_read_only_sql("SELECT count(*) FROM user_sessions", 50)
        self.assertTrue(out.endswith("\nLIMIT 50"), out)
        # The clamp MUST be on its own line: a trailing comment would otherwise
        # swallow it and the query would run unbounded.
        self.assertEqual(out.splitlines()[-1], "LIMIT 50")

    def test_a_trailing_comment_cannot_swallow_the_clamp(self):
        out = pe.clamp_read_only_sql(
            "SELECT count(id) FROM profiles -- everything after this is a comment", 10)
        self.assertEqual(out.splitlines()[-1], "LIMIT 10")

    def test_an_explicit_limit_is_respected_not_doubled(self):
        out = pe.clamp_read_only_sql("SELECT count(id) FROM profiles LIMIT 5", 200)
        self.assertEqual(out.lower().count("limit"), 1)

    def test_a_limit_that_is_a_literal_does_not_suppress_the_clamp(self):
        # `LIKE '%limit%'` is not a row bound. Requiring a DIGIT after the
        # keyword is what tells them apart; a substring test ran it UNBOUNDED.
        out = pe.clamp_read_only_sql(
            "SELECT count(id) FROM profiles WHERE sponsor LIKE '%limit%'", 7)
        self.assertEqual(out.splitlines()[-1], "LIMIT 7")

    def test_a_limit_inside_a_comment_does_not_suppress_the_clamp(self):
        """`-- LIMIT 5` LOOKED like a row bound, so the clamp was skipped and
        the query ran unbounded against production."""
        out = pe.clamp_read_only_sql("SELECT count(*) FROM events -- LIMIT 5", 7)
        self.assertEqual(out.splitlines()[-1], "LIMIT 7")

    def test_an_over_large_limit_is_clamped_not_trusted(self):
        """`LIMIT 1000000` was accepted verbatim: the guard asked only whether a
        numeric limit was PRESENT, so MAX_ROW_LIMIT bounded nothing and
        fetchall() pulled the whole result set out of production."""
        out = pe.clamp_read_only_sql("SELECT count(*) FROM t LIMIT 1000000", 30)
        self.assertEqual("SELECT count(*) FROM t LIMIT 30", out)
        self.assertEqual(1, out.lower().count("limit"))

    def test_a_keyword_inside_a_literal_is_a_value_not_a_statement(self):
        """Audit tables are exactly what a data-shaped diagnosis groups by, and
        `action = 'delete'` was rejected as if it were a DELETE."""
        for sql in ("SELECT count(*) FROM audit_events WHERE action = 'delete'",
                    "SELECT count(*) FROM audit_events WHERE action = 'update'",
                    "SELECT count(*) FROM t WHERE note = 'a; b'",
                    "SELECT count(*) FROM t /* drop this later */"):
            with self.subTest(sql=sql):
                pe.assert_read_only_diagnostic_sql(sql)

    def test_raw_row_selection_is_refused_so_production_values_stay_put(self):
        """The planner prompt has always said "must not be exfiltrated row by
        row"; nothing checked, and up to 30 rows of real values were serialized
        into the next judge prompt and sent to a model provider."""
        with self.assertRaises(pe.ReadOnlySqlError) as caught:
            pe.assert_read_only_diagnostic_sql("SELECT * FROM users")
        self.assertIn("AGGREGATE", str(caught.exception))
        # ...and the metadata path, which reads information_schema, is exempt.
        pe.assert_read_only_diagnostic_sql(pe.INTROSPECT_COLUMNS_SQL,
                                           allow_raw_rows=True)

    def test_keyword_lookalike_columns_are_queryable(self):
        # THE 90-of-90 DEFECT: a substring test made every timestamped table
        # unqueryable because `updated_at` contains "update".
        for sql in ("SELECT max(updated_at) FROM t",
                    "SELECT count(created_at), count(created_by) FROM t",
                    "SELECT max(deleted_at) FROM t",
                    "SELECT max(inserted_at) FROM t",
                    "SELECT avg(execution_time_ms) FROM t",
                    "SELECT count(id) FROM grants"):
            with self.subTest(sql=sql):
                pe.assert_read_only_diagnostic_sql(sql)

    def test_every_forbidden_keyword_is_rejected_at_a_word_boundary(self):
        # Totality: the registry is only meaningful if each entry really blocks.
        forms = {
            "drop": "SELECT 1 FROM t WHERE drop x", "delete": "SELECT delete FROM t",
            "update": "SELECT 1 FROM t WHERE update", "insert": "SELECT insert FROM t",
            "alter": "SELECT alter FROM t", "create": "SELECT create FROM t",
            "truncate": "SELECT truncate FROM t", "union": "SELECT 1 UNION ALL 2",
            "into": "SELECT 1 INTO x FROM t", "exec": "SELECT exec FROM t",
            "execute": "SELECT execute FROM t", "grant": "SELECT grant FROM t",
            "revoke": "SELECT revoke FROM t",
        }
        for keyword in pe.READ_ONLY_SQL_FORBIDDEN_KEYWORDS:
            with self.subTest(keyword=keyword):
                with self.assertRaises(pe.ReadOnlySqlError) as ctx:
                    pe.assert_read_only_diagnostic_sql(forms[keyword])
                self.assertIn(keyword, str(ctx.exception).lower())

    def test_the_three_structural_refusals(self):
        for sql, needle in (
                ("DROP TABLE users", "single select"),
                ("UPDATE users SET x = 1", "single select"),
                ("SELECT 1; DROP TABLE users", "';'"),
                ("SELECT (SELECT 1 FROM t) FROM u", "subquer")):
            with self.subTest(sql=sql):
                with self.assertRaises(pe.ReadOnlySqlError) as ctx:
                    pe.assert_read_only_diagnostic_sql(sql)
                self.assertIn(needle, str(ctx.exception).lower())

    def test_the_error_message_names_the_offending_token(self):
        # A model that is told only "forbidden keywords" re-sends the same
        # statement (measured: 83 times in 30 minutes).
        with self.assertRaises(pe.ReadOnlySqlError) as ctx:
            pe.assert_read_only_diagnostic_sql("SELECT 1 INTO x FROM t")
        self.assertIn("INTO", str(ctx.exception))

    def test_the_session_opens_a_read_only_transaction_with_a_timeout(self):
        conn = _Conn()
        with pe.ReadOnlySession("postgres://x", connect=lambda _u: conn) as s:
            s.execute("SELECT count(*) FROM user_sessions")
        joined = " | ".join(conn.session_sets).lower()
        self.assertIn("statement_timeout", joined)
        self.assertIn("read only", joined)
        self.assertTrue(conn.rolled_back, "the session must never commit")
        self.assertFalse(conn.committed)
        self.assertTrue(conn.closed)

    def test_the_session_refuses_a_write_even_though_the_server_would_too(self):
        conn = _Conn()
        with pe.ReadOnlySession("postgres://x", connect=lambda _u: conn) as s:
            with self.assertRaises(pe.ReadOnlySqlError):
                s.execute("DELETE FROM user_sessions")
        self.assertFalse(any("delete" in q.lower() for q in conn.executed),
                         "a write must not reach the driver at all")

    def test_a_missing_driver_is_LOUD_and_names_the_install_command(self):
        with self.assertRaises(pe.DriverMissingError) as ctx:
            pe.load_driver.__wrapped__() if hasattr(pe.load_driver, "__wrapped__") \
                else _load_driver_with_no_modules()
        self.assertIn("pip install", str(ctx.exception))


def _load_driver_with_no_modules():
    """Run load_driver with both drivers unimportable."""
    import builtins
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name in ("psycopg", "psycopg2"):
            raise ImportError(f"no module named {name}")
        return real(name, *a, **kw)

    builtins.__import__ = fake
    try:
        return pe.load_driver()
    finally:
        builtins.__import__ = real


class AvailabilityIsAVerdictTests(unittest.TestCase):

    def test_no_env_var_is_UNAVAILABLE_and_says_it_is_not_a_clean_bill(self):
        got = pe.availability({})
        self.assertFalse(got["available"])
        self.assertIn(pe.READONLY_URL_ENV, got["reason"])
        # The sentence that stops "unavailable" reading as "nothing found".
        self.assertIn("not evidence that none exists", got["reason"])

    def test_collect_returns_a_record_that_states_the_reason(self):
        rec = pe.collect_runtime_evidence(_judge_script({}), "purpose", env={})
        self.assertFalse(rec["available"])
        self.assertEqual(rec["findings"], [])
        self.assertIn(pe.READONLY_URL_ENV, rec["reason"])

    def test_a_connection_failure_is_an_ERROR_not_a_clean_result(self):
        def boom(_url):
            raise OSError("connection refused")
        rec = pe.collect_runtime_evidence(
            _judge_script(PLAN_OK), "purpose",
            env={pe.READONLY_URL_ENV: "postgres://x"}, connect=boom)
        self.assertEqual(rec["findings"], [])
        self.assertTrue(rec["errors"], "a failed connection must be recorded")
        self.assertIn("connection refused", " ".join(rec["errors"]))

    def test_no_probe_executing_is_recorded_as_no_conclusion_drawn(self):
        rec = pe.collect_runtime_evidence(
            _judge_script({"queries": [
                {"name": "bad", "question": "q", "sql": "DELETE FROM t"}]}),
            "purpose", env={pe.READONLY_URL_ENV: "postgres://x"},
            connect=lambda _u: _Conn())
        self.assertEqual(rec["findings"], [])
        self.assertIn("not a clean bill of health", " ".join(rec["errors"]))


# --------------------------------------------------------------------------- #
# B. SCHEMA DISCOVERY BEFORE SQL
# --------------------------------------------------------------------------- #
class EvidenceIntegrityTests(unittest.TestCase):
    """Three ways this phase could report more than it knows."""

    def test_a_verdict_citing_a_probe_that_never_ran_is_refused(self):
        """The strongest claim this module makes is "production rows demonstrate
        this". Every dict the verdict model returned used to be accepted, so a
        stale or invented query_name carried that claim with nothing under it."""
        def judge(system, prompt, schema):
            if schema is pe.DIAGNOSTIC_PLAN_SCHEMA:
                return {"queries": [{
                    "name": "real", "question": "q",
                    "sql": "SELECT user_agent, count(*) AS sessions "
                           "FROM user_sessions GROUP BY user_agent"}]}
            return {"findings": [
                {"query_name": "real", "title": "kept", "severity": "high",
                 "problem": "p", "evidence": "e", "next_step": "n"},
                {"query_name": "never_ran", "title": "invented", "severity": "high",
                 "problem": "p", "evidence": "e", "next_step": "n"}]}

        rec = pe.collect_runtime_evidence(
            judge, "purpose", env={pe.READONLY_URL_ENV: "postgres://x"},
            connect=lambda _u: _Conn())
        self.assertEqual(["kept"], [f["title"] for f in rec["findings"]])
        self.assertTrue(any("never executed" in r["reason"]
                            for r in rec["rejected"]),
                        "the invented citation must be recorded, not dropped")

    def test_a_truncated_schema_is_not_presented_as_the_whole_schema(self):
        """information_schema is ordered by table name, so a bound that fills up
        drops the LAST tables - and every probe against them then comes back
        rejected as nonexistent."""
        real = pe.SCHEMA_ROW_LIMIT
        pe.SCHEMA_ROW_LIMIT = 1        # the fixture returns more rows than this
        try:
            rec = pe.collect_runtime_evidence(
                lambda *a, **k: {"queries": []}, "purpose",
                env={pe.READONLY_URL_ENV: "postgres://x"},
                connect=lambda _u: _Conn())
        finally:
            pe.SCHEMA_ROW_LIMIT = real
        self.assertTrue(any("PREFIX of the real" in e for e in rec["errors"]),
                        rec["errors"])

    def test_one_database_is_not_every_programs_database(self):
        """A single `--program A --program B` run diagnosed BOTH from whatever
        one variable pointed at, and attached A's rows to B's report."""
        E = pe.READONLY_URL_ENV
        # One program: the bare variable can only mean that program.
        self.assertEqual(({E: "postgres://a"}, ""),
                         pe.resolve_program_url("A", 1, {E: "postgres://a"}))
        # Several programs, only the bare variable: SKIPPED, with the reason.
        env, why = pe.resolve_program_url("B", 2, {E: "postgres://a"})
        self.assertEqual({}, env)
        self.assertIn("nothing ties that one database", why)
        self.assertIn("not evidence that no data-shaped problem exists", why)
        self.assertIn(pe.program_url_env("B"), why)
        # A per-program variable is unambiguous at any program count.
        self.assertEqual(
            ({E: "postgres://b"}, ""),
            pe.resolve_program_url("B", 2, {E: "postgres://a",
                                            pe.program_url_env("B"): "postgres://b"}))
        # Nothing configured stays the module's own "not set" verdict.
        self.assertEqual(({}, ""), pe.resolve_program_url("A", 3, {}))

    def test_the_connection_attempt_is_bounded(self):
        """statement_timeout is installed only AFTER the connection succeeds, so
        it cannot bound the connect itself; against a blackholed host the
        non-fatal evidence phase could stall the whole audit."""
        self.assertEqual(10, pe.connect_timeout_s({}))
        self.assertEqual(3, pe.connect_timeout_s({pe.CONNECT_TIMEOUT_ENV: "3"}))
        self.assertEqual(60, pe.connect_timeout_s({pe.CONNECT_TIMEOUT_ENV: "9999"}))
        self.assertEqual(1, pe.connect_timeout_s({pe.CONNECT_TIMEOUT_ENV: "0"}))
        seen = {}

        class _Mod:
            @staticmethod
            def connect(url, **kw):
                seen.update(kw)
                return _Conn()

        real = pe.load_driver
        pe.load_driver = lambda: (_Mod, "fake")
        try:
            with pe.ReadOnlySession("postgres://x"):
                pass
        finally:
            pe.load_driver = real
        self.assertEqual(10, seen.get("connect_timeout"))


class SchemaDiscoveryTests(unittest.TestCase):

    def test_introspection_runs_FIRST_and_reads_information_schema(self):
        conn = _Conn()
        pe.collect_runtime_evidence(
            _judge_script(PLAN_OK, VERDICT_OK), "purpose",
            env={pe.READONLY_URL_ENV: "postgres://x"}, connect=lambda _u: conn)
        queries = [q for q in conn.executed if not q.lower().startswith("set ")]
        self.assertIn("information_schema.columns", queries[0],
                      "the FIRST read must be schema discovery, not a guess")

    def test_the_real_column_names_are_put_in_the_planner_prompt(self):
        judge = _judge_script(PLAN_OK, VERDICT_OK)
        pe.collect_runtime_evidence(
            judge, "purpose", env={pe.READONLY_URL_ENV: "postgres://x"},
            connect=lambda _u: _Conn())
        plan_prompt = judge.calls["prompts"][0]
        for real in ("user_sessions", "user_agent", "primary_email",
                     "match_decision", "sponsor"):
            self.assertIn(real, plan_prompt)

    def test_the_four_real_hallucinations_are_each_rejected_and_named(self):
        # These are the exact wrong guesses a human had to correct by hand in
        # the motivating session.
        schema = {"profiles": ["id", "primary_email", "sponsor", "match_decision"]}
        for wrong, right in (("email", "primary_email"),
                             ("match_status", "match_decision"),
                             ("funder_name", "sponsor")):
            with self.subTest(wrong=wrong):
                problems = pe.unknown_identifiers(
                    f"SELECT {wrong} FROM profiles", schema)
                self.assertTrue(problems, f"{wrong} should not have passed")
                self.assertIn(wrong, problems[0])
        self.assertTrue(pe.unknown_identifiers("SELECT id FROM sessions", schema))
        # An ALIASED bad column is caught too, and the message names the real
        # candidates so the model can correct itself instead of re-sending.
        aliased = " ".join(pe.unknown_identifiers(
            "SELECT p.email FROM profiles p", schema))
        self.assertIn("email", aliased)
        self.assertIn(right, aliased)

    def test_a_hallucinated_column_never_reaches_the_database(self):
        conn = _Conn()
        rec = pe.collect_runtime_evidence(
            _judge_script({"queries": [{
                "name": "guess", "question": "q",
                "sql": "SELECT email FROM profiles GROUP BY email"}]}),
            "purpose", env={pe.READONLY_URL_ENV: "postgres://x"},
            connect=lambda _u: conn)
        self.assertFalse(any("email" in q for q in conn.executed),
                         "the rejected query must not be executed")
        self.assertEqual(len(rec["rejected"]), 1)
        self.assertIn("schema mismatch", rec["rejected"][0]["reason"])

    def test_a_correct_query_is_NOT_flagged(self):
        # A guard that cries wolf gets switched off. Aliases, functions, string
        # literals and joins must all pass.
        schema = {"user_sessions": ["id", "user_id", "user_agent", "created_at",
                                    "refresh_count"],
                  "profiles": ["id", "primary_email", "sponsor", "match_decision"]}
        for sql in (
                "SELECT user_agent, count(*) AS n FROM user_sessions "
                "GROUP BY user_agent ORDER BY n DESC",
                "SELECT date_trunc('day', created_at) AS d, count(*) AS c "
                "FROM user_sessions GROUP BY d",
                "SELECT count(*) FROM user_sessions WHERE user_agent LIKE '%FB_IAB%'",
                "SELECT u.user_agent, p.sponsor FROM user_sessions u "
                "JOIN profiles p ON p.id = u.user_id",
                "SELECT match_decision, count(*) FROM profiles "
                "GROUP BY match_decision HAVING count(*) > 5"):
            with self.subTest(sql=sql[:48]):
                self.assertEqual(pe.unknown_identifiers(sql, schema), [])

    def test_an_empty_schema_verifies_nothing_and_says_so(self):
        self.assertTrue(pe.unknown_identifiers("SELECT a FROM b", {}))


# --------------------------------------------------------------------------- #
# F. NON-CODE VERDICTS - REPORTED, NEVER PATCHED
# --------------------------------------------------------------------------- #
class NonCodeFindingsAreNeverPatchedTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import flexfactor_tests  # noqa: F401  (installs the test-hygiene redirects)
        cls.F = sys.modules["flexfactor"]

    def test_every_non_code_category_is_refused_by_the_fix_chokepoint(self):
        F = self.F
        for category in sorted(F.NON_CODE_FINDING_CATEGORIES):
            with self.subTest(category=category):
                finding = {"category": category, "severity": "critical",
                           "evidence_source": "runtime-data"}
                # 'info' is the most permissive floor there is.
                self.assertFalse(F.should_fix_finding(finding, "info"))

    def test_the_discriminator_alone_is_enough(self):
        F = self.F
        self.assertFalse(F.should_fix_finding(
            {"category": "bug", "severity": "critical",
             "evidence_source": "runtime-data"}, "low"))

    def test_the_category_alone_is_enough(self):
        F = self.F
        self.assertFalse(F.should_fix_finding(
            {"category": "environment", "severity": "critical"}, "low"))

    def test_an_ordinary_code_finding_is_STILL_fixed(self):
        F = self.F
        self.assertTrue(F.should_fix_finding(
            {"category": "bug", "severity": "high", "evidence_source": "code"},
            "high"))

    def test_a_missing_evidence_source_defaults_to_code_and_stays_fixable(self):
        F = self.F
        f = F._normalize_finding({"category": "bug", "severity": "high",
                                  "title": "t", "problem": "p", "fix": "f"})
        self.assertEqual(f["evidence_source"], "code")
        self.assertTrue(F.should_fix_finding(f, "high"))

    def test_a_non_code_finding_gets_the_gap_shape(self):
        F = self.F
        f = F._normalize_finding({"category": "client", "severity": "high",
                                  "title": "t", "problem": "p",
                                  "fix": "open in the system browser"})
        self.assertIs(f["code_fixable"], False)
        self.assertEqual(f["next_step"], "open in the system browser")

    def test_a_non_code_finding_never_enters_the_unresolved_fix_ledger(self):
        F = self.F
        pending: dict = {}
        F._update_unresolved_fix_ledger(
            pending,
            findings={"(runtime-data)": [{"category": "data",
                                          "severity": "critical",
                                          "evidence_source": "runtime-data"}]},
            clean=[], min_severity="info")
        self.assertEqual(pending, {},
                         "a non-code finding is not a pending code fix")

    def test_a_non_code_residual_never_triggers_another_fix_round(self):
        F = self.F
        self.assertFalse(F._residual_is_material(
            {"category": "configuration", "realistic_input": True,
             "affects_core": True, "repro": "set FOO=bar and the import fails"}))
        # ...and an ordinary residual with the same classification still does.
        self.assertTrue(F._residual_is_material(
            {"category": "bug", "realistic_input": True,
             "repro": "post {} to /x and it 500s"}))

    def test_the_runtime_manifest_PROVES_the_refusal_rather_than_asserting_it(self):
        F = self.F
        self.assertIs(F.runtime_manifest()["wired"]
                      ["non_code_findings_never_patched"], True)

    def test_the_owner_brief_renders_the_finding_and_its_evidence(self):
        F = self.F
        import tempfile
        rec = pe.collect_runtime_evidence(
            _judge_script(PLAN_OK, VERDICT_OK), "purpose",
            env={pe.READONLY_URL_ENV: "postgres://x"}, connect=lambda _u: _Conn())
        findings = [F._normalize_finding(f) for f in pe.runtime_findings(rec)]
        self.assertEqual(len(findings), 1)
        audit = _audit_stub(F, findings, rec)
        with tempfile.TemporaryDirectory() as d:
            path = F._write_audit_report(d, audit)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("OWNER BRIEF", text)
        self.assertIn("FB_IAB: 41 sessions, 0 refresh rotations", text)
        self.assertIn("Next step (owner):", text)
        # It must NOT be listed as a code defect awaiting a patch.
        head = text.split(
            "## Remaining defects that could not be safely repaired"
        )[1].split("##")[0]
        self.assertNotIn("Facebook in-app browser", head)

    def test_an_UNAVAILABLE_capability_says_so_in_the_report(self):
        F = self.F
        import tempfile
        rec = pe.collect_runtime_evidence(_judge_script({}), "purpose", env={})
        audit = _audit_stub(F, [], rec)
        with tempfile.TemporaryDirectory() as d:
            path = F._write_audit_report(d, audit)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("Runtime-data evidence", text)
        self.assertIn("UNAVAILABLE", text)
        self.assertIn("NOT a clean data bill of health", text)


def _audit_stub(F, findings, runtime_evidence) -> dict:
    """The minimum audit dict `_write_audit_report` needs, plus our two keys."""
    return {
        "name": "demo", "dir": ".", "branch": "main", "files_reviewed": 1,
        "findings": list(findings), "file_findings": {}, "applied_files": [],
        "unverified_files": [], "test_files": [], "test_status": "",
        "e2e": {}, "fix_notes": [], "commit_status": "", "baseline_ok": True,
        "cycles": 1, "providers": [], "converged": True, "stop_reason": "",
        "suite_status": "", "clean_files": [], "usd": 0.0,
        "fix_severity": "high", "manual_review": [], "unresolved_files": [],
        "unresolved_findings": 0, "low_findings": [], "readiness": None,
        "bootstrap": [], "ecosystems": [], "verification_is_real": True,
        "verification_note": "", "purpose_gap": None, "bridged_files": [],
        "purpose_assessment_errors": [], "competitor_research": None,
        "competitors_enabled": False, "runtime_evidence": runtime_evidence,
        "purpose_contract": None, "purpose_before": None, "bridged_early": [],
        "review_incomplete": 0, "noop_stats": {}, "inventory": {},
        "evidence": {}, "evidence_paths": {}, "review_ledger": {},
    }


# --------------------------------------------------------------------------- #
# THE PHASE END TO END
# --------------------------------------------------------------------------- #
class RuntimeEvidencePhaseTests(unittest.TestCase):

    def test_the_motivating_case_end_to_end(self):
        conn = _Conn()
        rec = pe.collect_runtime_evidence(
            _judge_script(PLAN_OK, VERDICT_OK), "a grant-management SaaS",
            env={pe.READONLY_URL_ENV: "postgres://x"}, connect=lambda _u: conn)
        self.assertTrue(rec["available"])
        self.assertEqual(rec["tables"], 2)
        self.assertEqual(len(rec["queries"]), 1)
        self.assertEqual(len(rec["findings"]), 1)
        mapped = pe.runtime_findings(rec)
        self.assertEqual(mapped[0]["category"], "client")
        self.assertEqual(mapped[0]["evidence_source"], "runtime-data")
        self.assertIs(mapped[0]["code_fixable"], False)
        self.assertEqual(mapped[0]["file"], pe.RUNTIME_EVIDENCE_FILE)
        # Every executed probe was clamped.
        probes = [q for q in conn.executed
                  if not q.lower().startswith("set ")
                  and "information_schema" not in q]
        self.assertTrue(probes)
        for q in probes:
            self.assertTrue(q.splitlines()[-1].lower().startswith("limit"), q)

    def test_a_failing_probe_is_named_not_hidden(self):
        conn = _Conn(_Results(default=RuntimeError("relation does not exist")))
        rec = pe.collect_runtime_evidence(
            _judge_script(PLAN_OK, VERDICT_OK), "purpose",
            env={pe.READONLY_URL_ENV: "postgres://x"}, connect=lambda _u: conn)
        self.assertEqual(rec["findings"], [])
        self.assertIn("relation does not exist", " ".join(rec["errors"]))

    def test_the_audit_pipeline_reaches_this_module(self):
        """The wiring, not the module: grep the REAL call site.

        Four times in this repo a module was written, tested and never called.
        `_prodevidence_module` importing is not evidence; the phase calling
        `collect_runtime_evidence` and merging its findings is.
        """
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "flexfactor.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def audit_one_program", 1)[1]
        self.assertIn("_prodevidence_module()", body)
        self.assertIn("collect_runtime_evidence", body)
        self.assertIn("runtime_findings", body)
        self.assertIn("all_findings = list(all_findings) + runtime_evidence_findings",
                      body)
        self.assertIn('"runtime_evidence": runtime_evidence', body)


# --------------------------------------------------------------------------- #
# G. GATES THAT CAN ACTUALLY FAIL (readiness sweep 2026-08-30)
#
# Every test below drives the real function and fails on the pre-sweep code.
# Nothing here touches ~/.flexfactor: the repository fixtures are tmpdirs and
# the schema fixtures are injected.
# --------------------------------------------------------------------------- #
_BIG_SOURCE_PADDING = ("# " + "x" * 77 + "\n") * 52_000   # ~4.16MB > the 4MB cap


def _write_big_source(root: str, name: str = "big.py") -> str:
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("def real_fn():\n    return 1\n" + _BIG_SOURCE_PADDING)
    return path


class UnscannedSourceIsNeverCompleteTests(unittest.TestCase):
    """A file whose bytes were HASHED AND NEVER SCANNED is not inventoried."""

    def test_a_blocked_source_file_cannot_report_a_complete_inventory(self):
        """`_index_large_file_in_chunks` emits status "blocked" for a source
        file past the hard cap - hashed, never scanned. The inventory gate
        excluded only "inventoried"/"refused", so it called that COMPLETE."""
        real_cap = ev._LARGE_FILE_HARD_CAP
        ev._LARGE_FILE_HARD_CAP = 1_000        # the fixture is far above this
        try:
            with tempfile.TemporaryDirectory() as tmp:
                _write_big_source(tmp)
                index = ev.build_repository_index(tmp, "blocked-run")
        finally:
            ev._LARGE_FILE_HARD_CAP = real_cap
        record = next(f for f in index["files"] if f["path"] == "big.py")
        self.assertEqual(record["status"], "blocked")
        self.assertFalse(index["complete_source_inventory"],
                         "source that was never scanned cannot be 'complete'")

    def test_a_changed_chunk_analyzed_file_counts_as_rescanned(self):
        """"analyzed-in-chunks" IS a successful analysis (the totals count it),
        so omitting it from the rescan vocabulary meant a repo with one changed
        >4MB source file could NEVER converge, with a wrong stated reason."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_big_source(tmp)
            index = ev.build_repository_index(tmp, "chunk-run")
        record = next(f for f in index["files"] if f["path"] == "big.py")
        self.assertEqual(record["status"], "analyzed-in-chunks")
        rescan = ev.changed_file_rescan(index, ["big.py"])
        self.assertTrue(rescan["complete"], rescan["files"])

    def test_a_changed_non_source_file_still_counts_as_rescanned(self):
        """Regression guard for the fix above: a hashed-only text file's
        complete record is "inventoried" and must keep passing."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("hello\n")
            index = ev.build_repository_index(tmp, "text-run")
        self.assertTrue(ev.changed_file_rescan(index, ["README.md"])["complete"])


class BlastRadiusGateCanFailTests(unittest.TestCase):
    """Contract 12: `dependency_blast_radius` hardcodes "ran": True on every
    return path, so ran-implies-passed was a gate that could not fail."""

    @staticmethod
    def _gates(index, blast):
        coverage = ev.coverage_ledger(
            index, run_id="r", test_command=["pytest"], tests_ran=True,
            tests_passed=True, generated_test_modules=[], e2e={})
        return ev.quality_gates(
            run_id="r", baseline_ran=True, baseline_passed=True,
            suite_command=["pytest"], suite_ran=True, suite_passed=True,
            tests_collected=True, e2e={}, rescan={"complete": True},
            blast=blast, secrets=[], index=index, coverage=coverage)

    def test_an_unresolved_local_import_fails_the_blast_radius_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "src"))
            with open(os.path.join(tmp, "src", "a.py"), "w", encoding="utf-8") as fh:
                fh.write("from .not_here import gone\ndef a(): return gone()\n")
            index = ev.build_repository_index(tmp, "blast-run")
        blast = ev.dependency_blast_radius(index, ["src/a.py"])
        self.assertTrue(blast["ran"])
        self.assertTrue(blast["unresolved_local_imports"],
                        "fixture must actually produce an unresolved import")
        gate = next(g for g in self._gates(index, blast)["gates"]
                    if g["id"] == "blast-radius")
        self.assertEqual(gate["status"], "fail")

    def test_a_fully_resolved_blast_radius_still_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "src"))
            with open(os.path.join(tmp, "src", "a.py"), "w", encoding="utf-8") as fh:
                fh.write("from .b import work\ndef a(): return work()\n")
            with open(os.path.join(tmp, "src", "b.py"), "w", encoding="utf-8") as fh:
                fh.write("def work(): return 1\n")
            index = ev.build_repository_index(tmp, "blast-ok")
        blast = ev.dependency_blast_radius(index, ["src/b.py"])
        self.assertEqual(blast["unresolved_local_imports"], [])
        gate = next(g for g in self._gates(index, blast)["gates"]
                    if g["id"] == "blast-radius")
        self.assertEqual(gate["status"], "pass")


class SecretScanTruncationTests(unittest.TestCase):
    """A clean secrets gate must not be a claim about bytes never read."""

    def test_content_past_the_scan_cap_is_reported_not_assumed_clean(self):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "big_notes.md"), "w", encoding="utf-8") as fh:
                fh.write("notes\n" + ("y" * 79 + "\n") * 14_000)   # > the 1MB cap
                fh.write('trailing = "' + token + '"\n')           # never read
            index = ev.build_repository_index(tmp, "secret-run")
            findings = ev.secret_findings(tmp, index)
            coverage = ev.coverage_ledger(
                index, run_id="r", test_command=None, tests_ran=False,
                tests_passed=None, generated_test_modules=[], e2e={})
            gates = ev.quality_gates(
                run_id="r", baseline_ran=True, baseline_passed=True,
                suite_command=["pytest"], suite_ran=True, suite_passed=True,
                tests_collected=True, e2e={}, rescan={"complete": True},
                blast={"ran": True}, secrets=findings, index=index,
                coverage=coverage)
        # Proof the bytes really were never read: the token is not reported.
        self.assertEqual([f for f in findings
                          if f["rule_id"] == "secret.github-token"], [])
        truncated = [f for f in findings
                     if f["rule_id"] == "secret.scan-truncated"]
        self.assertEqual(len(truncated), 1, findings)
        self.assertEqual(truncated[0]["disposition"], "unresolved")
        secrets_gate = next(g for g in gates["gates"] if g["id"] == "secrets")
        self.assertEqual(secrets_gate["status"], "fail",
                         "unscanned content cannot produce a clean gate")

    def test_a_fully_scanned_clean_file_still_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "small.md"), "w", encoding="utf-8") as fh:
                fh.write("nothing credential shaped here\n")
            index = ev.build_repository_index(tmp, "clean-run")
            self.assertEqual(ev.secret_findings(tmp, index), [])


class SchemaSliceIsNamedTests(unittest.TestCase):
    """The SCHEMA_ROW_LIMIT fix covered only the ROW bound; the in-Python
    max_tables / max_columns_per_table caps were still silent."""

    def setUp(self):
        # Module state, exactly like _COLUMN_TYPES: leave it as we found it.
        self.addCleanup(pe._SCHEMA_TRUNCATION.clear)
        pe._SCHEMA_TRUNCATION.clear()

    def test_a_dropped_table_is_not_reported_as_nonexistent(self):
        rows = [("t_big", "c%d" % i, "text") for i in range(4)]
        rows += [("t_small", "id", "uuid")]
        columns = pe.introspect_columns(lambda _sql: rows, max_tables=1)
        self.assertEqual(sorted(columns), ["t_big"])
        self.assertEqual(pe._SCHEMA_TRUNCATION["dropped_tables"], ["t_small"])
        problems = " ".join(
            pe.unknown_identifiers("SELECT id FROM t_small", columns))
        self.assertIn("not in the schema slice", problems)
        self.assertNotIn("does not exist", problems)

    def test_an_untruncated_schema_still_says_does_not_exist(self):
        columns = pe.introspect_columns(lambda _sql: [("t_big", "id", "uuid")])
        self.assertEqual(pe._SCHEMA_TRUNCATION, {})
        problems = " ".join(
            pe.unknown_identifiers("SELECT id FROM nope", columns))
        self.assertIn("does not exist", problems)

    def test_dropped_columns_are_recorded_with_their_cap(self):
        rows = [("t", "c%d" % i, "text") for i in range(5)]
        columns = pe.introspect_columns(lambda _sql: rows,
                                        max_columns_per_table=2)
        self.assertEqual(columns["t"], ["c0", "c1"])
        self.assertEqual(pe._SCHEMA_TRUNCATION["dropped_columns"], {"t": 3})
        self.assertEqual(pe._SCHEMA_TRUNCATION["max_columns_per_table"], 2)

    def test_the_runtime_evidence_report_names_the_dropped_tables(self):
        """The whole point: the audit report has to SAY the schema is a slice,
        the way it already says the row bound filled up."""
        big = [("t%03d" % i, "id", "uuid") for i in range(140)]

        class _SchemaCursor(_Cursor):
            def execute(self, sql):
                if "information_schema.columns" in sql.strip().lower():
                    self.conn.executed.append(sql)
                    self._rows = list(big)
                    self.description = [("table_name",), ("column_name",),
                                        ("data_type",)]
                    return
                return super().execute(sql)

        class _SchemaConn(_Conn):
            def cursor(self):
                return _SchemaCursor(self)

        rec = pe.collect_runtime_evidence(
            lambda *a, **k: {"queries": []}, "purpose",
            env={pe.READONLY_URL_ENV: "postgres://x"},
            connect=lambda _u: _SchemaConn())
        joined = " ".join(rec["errors"])
        self.assertIn("SLICE of the real one", joined)
        self.assertIn("120-table cap", joined)
        self.assertIn("t139", joined)


class ScoutSandboxPostureIsTrueTests(unittest.TestCase):
    """Every _scout_report.json asserted a sandbox posture the live path never
    applied: the production call site passes no sandbox_summary and
    `enrich_evidence_from_clone` executes no candidate code."""

    EVALUATION = {
        "need": "n",
        "repo": {"fullName": "a/b", "htmlUrl": "https://x/a/b"},
        "result": {"repo": {"fullName": "a/b", "htmlUrl": "https://x/a/b"}},
        "benefit": {"benefit_score": 70, "how_it_helps": "maybe"},
        "verdicts": {"safe_to_integrate": True, "reasons": []},
        "evidence": {
            "license": "Apache-2.0", "license_compatible": True,
            "commit_sha": "b" * 40, "commit_pin_source": "clone",
            "clone_inspection_ok": True, "safety_verdict": "allow",
            "advisories": "none", "last_activity": "2026-01-01",
            "stars": 10, "language": "JS",
        },
    }

    def _report(self, **kw):
        return sc.build_scout_structured_report(
            "Prog", {"summary": "s", "stack": ["node"], "goals": ["g"]},
            [dict(self.EVALUATION)], **kw)

    def test_the_default_posture_does_not_claim_a_control_that_never_ran(self):
        sandbox = self._report()["sandbox"]
        self.assertEqual(sandbox["candidate_execution"], "not-executed")
        blob = json.dumps(sandbox)
        self.assertNotIn("proxy-poisoned", blob)
        self.assertNotIn("disposable-temp-dir", blob)

    def test_a_caller_that_really_sandboxed_still_reports_its_own_posture(self):
        summary = {"candidate_execution": "disposable-temp-dir",
                   "credentials": "stripped",
                   "egress": "proxy-poisoned-best-effort",
                   "teardown": "required"}
        self.assertEqual(self._report(sandbox_summary=summary)["sandbox"],
                         summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
