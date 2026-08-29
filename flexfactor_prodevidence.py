#!/usr/bin/env python3
"""Read-only PRODUCTION EVIDENCE: schema discovery + guarded SELECT execution.

WHY THIS MODULE EXISTS (owner order 2026-08-25, from a real case)
----------------------------------------------------------------
A human diagnosed a live GrantFlow complaint ("can't log in", "can't find
anything").  The true root causes were NOT code:

  1. The user's session was the Facebook in-app browser (`user_agent` contains
     `FB_IAB`), which drops a `sameSite:strict` refresh cookie, so she was
     bounced every 3 hours.  Found by querying prod `user_sessions` and
     comparing refresh-rotation counts per client (0 vs 10).  **No code was
     wrong.**
  2. A code defect that was only visible because prod DATA showed 81 tasks
     across 11 profiles archived by a single sweep.

FlexFactor could reach neither fact: it has no prod read path, so it would have
reviewed source files and emitted a patch.  A tool that can only emit patches
will invent one.  That is the failure this module exists to prevent.

THE THREE RULES THIS MODULE ENFORCES IN CODE, NOT BY CONVENTION
---------------------------------------------------------------
A. READ-ONLY IS A CODE PERIMETER.  `assert_read_only_diagnostic_sql` is the
   single chokepoint, ported from the proven GrantFlow implementation
   (`backend/services/anyaAdminTools.js`).  Belt and braces: the connection
   also opens a `READ ONLY` transaction with a statement timeout, so even a
   guard bug cannot write.
B. NO NEW EXECUTION AUTHORITY.  `railway` stays a BLOCKED deploy exe in
   `flexfactor_cmdpolicy`; this module never launches a process at all.  The
   connection string comes from an explicit env var the owner sets.  Absent ->
   the capability reports UNAVAILABLE and says so.  It must NEVER degrade to
   "no data problems found".
C. SCHEMA BEFORE SQL.  The highest-frequency failure mode of an LLM writing SQL
   is a hallucinated column name; in the motivating session four separate wrong
   guesses had to be corrected by hand (`email`->`primary_email`,
   `match_status`->`match_decision`, `funder_name`->`sponsor`).  So
   `introspect_columns` runs FIRST and `unknown_identifiers` REJECTS a proposed
   query that names a column the database does not have, before it executes,
   naming the real candidates.

TRAP ALREADY PAID FOR: `psql` HANGS on the Railway proxy.  Use a library client
(`psycopg`, or `psycopg2` where v3 is not installed) - never the CLI.
"""
from __future__ import annotations

import os
import re

READONLY_URL_ENV = "FLEXFACTOR_READONLY_DATABASE_URL"
STATEMENT_TIMEOUT_ENV = "FLEXFACTOR_READONLY_STATEMENT_TIMEOUT_MS"
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000
CONNECT_TIMEOUT_ENV = "FLEXFACTOR_DB_CONNECT_TIMEOUT_S"
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 500

# The exact install line a caller must be told when no driver is importable.
# "couldn't connect" is NEVER allowed to read as "clean", so this string is
# carried in the unavailability reason rather than logged and swallowed.
DRIVER_INSTALL_HINT = "pip install 'psycopg[binary]'"


class ReadOnlySqlError(ValueError):
    """The proposed SQL is not a safe single read-only diagnostic SELECT."""


class DriverMissingError(RuntimeError):
    """No PostgreSQL driver is importable. Carries the install command."""


class ProdEvidenceUnavailable(RuntimeError):
    """The runtime-data capability cannot run, with a NAMED reason."""


# --------------------------------------------------------------------------- #
# THE READ-ONLY PERIMETER
# --------------------------------------------------------------------------- #
# Ported verbatim in behaviour from GrantFlow's READ_ONLY_SQL_FORBIDDEN_KEYWORDS.
#
# WHY WORD BOUNDARIES AND NEVER A SUBSTRING TEST.  GrantFlow's guard once read
#   dangerousKeywords.some((k) => sql.includes(k))
# A substring test cannot tell a STATEMENT from an IDENTIFIER, and a real schema
# is full of identifiers that contain these words:
#   updated_at > update    created_at/created_by > create
#   grants     > grant     deleted_at            > delete
#   execution_time_ms > exec   inserted_at       > insert
# i.e. every timestamped table was structurally unqueryable.  Measured in prod
# on 2026-08-01: 90 of 90 all-time query failures were this class, and 0 of them
# contained a real SQL keyword at a word boundary.  Word-boundary matching is
# STRICTLY MORE PRECISE, not weaker - every genuine DROP / DELETE /
# UNION SELECT / INSERT INTO form is still rejected (asserted keyword-by-keyword
# in the tests).  Adding a keyword here is the only supported way to widen it.
READ_ONLY_SQL_FORBIDDEN_KEYWORDS = (
    "drop", "delete", "update", "insert", "alter", "create", "truncate",
    "union", "into", "exec", "execute", "grant", "revoke",
)
_FORBIDDEN_PATTERNS = tuple(
    (kw, re.compile(r"\b" + kw + r"\b")) for kw in READ_ONLY_SQL_FORBIDDEN_KEYWORDS)

# A query already carrying its own row bound.  Requires a DIGIT after the
# keyword so a literal (`WHERE note LIKE '%limit%'`) or an identifier alias
# cannot suppress the clamp - the same substring-vs-token confusion as above,
# except there it silently ran an UNBOUNDED query.
_EXPLICIT_ROW_LIMIT_RX = re.compile(r"\blimit\s+\d")
_LIMIT_VALUE_RX = re.compile(r"\blimit\s+(\d+)")

# information_schema is METADATA, not production data: it holds no PII, so the
# aggregate rule and the 30-row diagnostic cap do not apply to it. They did.
# `introspect_columns` went through the ordinary executor, so the schema read
# was clamped to the diagnostic row limit and ORDER BY table_name meant a
# database with more columns than the cap simply LOST its later tables. The
# planner was then handed a truncated schema as if it were the whole one, and
# every probe against a missing table came back rejected as nonexistent.
SCHEMA_ROW_LIMIT = int(os.environ.get("FLEXFACTOR_SCHEMA_ROW_LIMIT", "20000"))
_SELECT_RX = re.compile(r"\bselect\b")

# AGGREGATE-ONLY, ENFORCED RATHER THAN ASKED FOR. DIAGNOSTIC_PLAN_SYSTEM has
# always told the planner "this is production data and must not be exfiltrated
# row by row", and nothing checked. `SELECT * FROM users` passed both guards
# and up to 30 rows of real production values - emails, tokens, whatever the
# table holds - were serialized into the next judge prompt and sent to a model
# provider. A rule that lives only in a prompt is not a rule.
_AGGREGATE_FUNCTIONS = frozenset((
    "count", "sum", "avg", "min", "max", "array_agg", "string_agg",
    "bool_and", "bool_or", "every", "percentile_cont", "percentile_disc",
    "stddev", "variance", "corr", "json_agg", "jsonb_agg",
))
_AGGREGATE_RX = re.compile(
    r"\bgroup\s+by\b|\b(?:" + "|".join(sorted(_AGGREGATE_FUNCTIONS))
    + r")\s*\(")


def mask_sql_noise(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments, IN PLACE.

    Every character removed is replaced by a space, so the result is the same
    length as the input and every index still points at the same character of
    the original. That is what lets the clamp below rewrite an over-large LIMIT
    by span without re-parsing the original text.

    Why it is needed at all: the guards ran against the raw lowered SQL, so a
    VALUE could impersonate a statement.
    `SELECT count(*) FROM audit_events WHERE action = 'delete'` was rejected as
    a DELETE - and audit tables are precisely what a data-shaped diagnosis wants
    to group by. In the other direction `SELECT * FROM events -- LIMIT 5` looked
    like it already carried a row bound, so the clamp was skipped and the query
    ran unbounded against production.
    """
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" or ch == '"':
            quote = ch
            out[i] = " "
            i += 1
            while i < n:
                if sql[i] == quote:
                    out[i] = " "
                    i += 1
                    if i < n and sql[i] == quote:   # '' / "" escape
                        out[i] = " "
                        i += 1
                        continue
                    break
                out[i] = " "
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            while i < n and not (sql[i] == "*" and i + 1 < n and sql[i + 1] == "/"):
                out[i] = " "
                i += 1
            for _ in range(2):
                if i < n:
                    out[i] = " "
                    i += 1
            continue
        i += 1
    return "".join(out)


def find_forbidden_sql_keyword(lowered_sql: str) -> str | None:
    """Return the forbidden keyword this SQL uses AS A KEYWORD, or None.

    Callers pass MASKED sql (see mask_sql_noise): a keyword inside a literal or
    a comment is data, not a statement."""
    for keyword, rx in _FORBIDDEN_PATTERNS:
        if rx.search(lowered_sql):
            return keyword
    return None


def assert_read_only_diagnostic_sql(sql: str, *,
                                    allow_raw_rows: bool = False) -> tuple[str, bool]:
    """The single chokepoint deciding whether a string is a safe read-only
    diagnostic query.  Returns (lowered, has_explicit_limit); raises
    ReadOnlySqlError otherwise.

    The message NAMES the offending token on purpose: the caller here is a
    language model, and "query contains forbidden keywords" gives it nothing to
    correct, so it re-sends the same statement (prod: one identical rejected
    query was re-issued 83 times in 30 minutes).
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ReadOnlySqlError("SQL query is required")
    lowered = sql.strip().lower()
    # Every structural check below runs on the MASKED text, so a value or a
    # comment can never impersonate a statement (or a row bound). The query
    # that EXECUTES is always the caller's original.
    masked = mask_sql_noise(lowered)
    if not masked.startswith("select"):
        raise ReadOnlySqlError(
            "only a single SELECT statement is allowed (this one starts with "
            f"{(masked.split(None, 1) or [chr(39)])[0]!r})")
    if ";" in masked:
        raise ReadOnlySqlError(
            "';' is not allowed - it permits multi-statement injection")
    if len(_SELECT_RX.findall(masked)) > 1:
        raise ReadOnlySqlError(
            "subqueries are not allowed - exactly one SELECT per query")
    keyword = find_forbidden_sql_keyword(masked)
    if keyword:
        raise ReadOnlySqlError(
            f"forbidden keyword {keyword.upper()!r}: this connection is "
            "read-only, so only a single SELECT with no subquery, no semicolon "
            "and no data-modifying keyword is accepted. A COLUMN named like a "
            "keyword (created_at, updated_at) is fine - re-sending the same "
            "statement will not help.")
    if not allow_raw_rows and not _AGGREGATE_RX.search(masked):
        raise ReadOnlySqlError(
            "this query returns raw rows. Production values are serialized into "
            "the next judge prompt and sent to a model provider, so every "
            "diagnostic query must AGGREGATE: use GROUP BY, or one of "
            + ", ".join(sorted(_AGGREGATE_FUNCTIONS)).upper()
            + ". Count or bucket the rows that show the problem instead of "
            "selecting them.")
    return lowered, bool(_EXPLICIT_ROW_LIMIT_RX.search(masked))


def clamp_read_only_sql(sql: str, limit: int = DEFAULT_ROW_LIMIT, *,
                        allow_raw_rows: bool = False) -> str:
    """Validate then bound.  The clamp goes on its OWN LINE so a trailing
    `-- comment` cannot swallow it (which would run the query unbounded).

    A LIMIT the query already carries is CLAMPED, not trusted: `LIMIT 1000000`
    used to be accepted verbatim because the guard only asked whether a numeric
    limit was PRESENT, so MAX_ROW_LIMIT bounded nothing and fetchall() pulled
    the whole result set out of production. Masking preserves offsets, so the
    effective limit is rewritten in place rather than having a second LIMIT
    appended after it - which Postgres rejects outright."""
    _lowered, has_limit = assert_read_only_diagnostic_sql(
        sql, allow_raw_rows=allow_raw_rows)
    ceiling = SCHEMA_ROW_LIMIT if allow_raw_rows else MAX_ROW_LIMIT
    safe_limit = max(1, min(int(limit or DEFAULT_ROW_LIMIT), ceiling))
    text = sql.strip()
    if not has_limit:
        return text + "\nLIMIT " + str(safe_limit)
    masked = mask_sql_noise(text.lower())
    match = None
    for match in _LIMIT_VALUE_RX.finditer(masked):
        pass                      # the LAST effective limit is the binding one
    if match is None:             # a limit that lives only in a literal/comment
        return text + "\nLIMIT " + str(safe_limit)
    if int(match.group(1)) <= safe_limit:
        return text
    return text[:match.start(1)] + str(safe_limit) + text[match.end(1):]


# --------------------------------------------------------------------------- #
# SCHEMA BEFORE SQL (gap B)
# --------------------------------------------------------------------------- #
# Tokens that appear in a legitimate SELECT and are NOT column references.
# Deliberately generous: a false ACCEPT here only means the database itself
# rejects an unknown column (loudly, named); a false REJECT would block a good
# query, so anything ambiguous belongs in this set.
_SQL_NON_IDENTIFIERS = frozenset("""
select from where group by order having limit offset as on and or not in is
null true false case when then else end distinct all asc desc join inner left
right full outer cross using between like ilike similar to escape any some
exists count sum avg min max coalesce nullif greatest least cast extract date
interval now current_date current_time current_timestamp length lower upper
trim substring position strpos split_part replace concat concat_ws left right
round floor ceil ceiling abs mod div age date_trunc date_part to_char
to_timestamp to_number to_date justify_days justify_hours make_interval
array array_agg string_agg json jsonb text integer bigint smallint numeric
decimal real double precision boolean varchar char timestamp timestamptz time
filter over partition rows range unbounded preceding following current row
nulls first last with recursive lateral only local session default
""".split())

_IDENT_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUALIFIED_RX = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_$]*)")
_FROM_JOIN_RX = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_$]*)?)"
    r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_$]*))?", re.I)
_STRING_LITERAL_RX = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RX = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RX = re.compile(r"/\*.*?\*/", re.S)
_FUNCTION_CALL_RX = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)\s*\(")


def _strip_literals_and_comments(sql: str) -> str:
    """Identifier analysis must not read string literals or comments: 'FB_IAB'
    is DATA, not a column, and `-- drop this later` is prose."""
    out = _BLOCK_COMMENT_RX.sub(" ", sql)
    out = _LINE_COMMENT_RX.sub(" ", out)
    return _STRING_LITERAL_RX.sub(" '' ", out)


def referenced_tables(sql: str) -> list[str]:
    """Bare table names in FROM/JOIN position, schema qualifier dropped."""
    body = _strip_literals_and_comments(sql)
    out: list[str] = []
    for raw, _alias in _FROM_JOIN_RX.findall(body):
        name = raw.replace(" ", "").split(".")[-1].lower()
        if name and name not in out:
            out.append(name)
    return out


_AS_ALIAS_RX = re.compile(r"\bas\s+([A-Za-z_][A-Za-z0-9_$]*)", re.I)


def _table_aliases(sql: str) -> set[str]:
    """Every name the query itself INTRODUCES: table aliases and result-column
    aliases. `count(*) AS n ... ORDER BY n` must not be reported as a missing
    column - a guard that cries wolf gets switched off, which is worse than no
    guard."""
    body = _strip_literals_and_comments(sql)
    aliases: set[str] = set()
    for raw, alias in _FROM_JOIN_RX.findall(body):
        aliases.add(raw.replace(" ", "").split(".")[-1].lower())
        if alias and alias.lower() not in _SQL_NON_IDENTIFIERS:
            aliases.add(alias.lower())
    for alias in _AS_ALIAS_RX.findall(body):
        if alias.lower() not in _SQL_NON_IDENTIFIERS:
            aliases.add(alias.lower())
    return aliases


def unknown_identifiers(sql: str, columns_by_table: dict) -> list[str]:
    """Names this SQL uses that the LIVE schema does not have.

    This is gap B's teeth.  `columns_by_table` comes from
    `introspect_columns` - i.e. from `information_schema.columns`, never from a
    model's memory.  Returns a sorted list of human-readable problems; empty
    means every table and column named by the query really exists.
    """
    body = _strip_literals_and_comments(sql)
    known_tables = {str(t).lower(): {str(c).lower() for c in cols}
                    for t, cols in (columns_by_table or {}).items()}
    problems: list[str] = []

    tables = referenced_tables(sql)
    if not known_tables:
        return ["schema introspection returned no tables, so no column in this "
                "query can be verified"]
    for t in tables:
        if t not in known_tables:
            near = sorted(n for n in known_tables if t in n or n in t)[:5]
            problems.append(
                f"table {t!r} does not exist"
                + (f" (did you mean: {', '.join(near)})" if near else ""))
    # Only columns of tables this query actually names are in scope; a query
    # naming no known table has already been reported above.
    in_scope = {c for t in tables if t in known_tables for c in known_tables[t]}
    aliases = _table_aliases(sql)
    functions = {m.lower() for m in _FUNCTION_CALL_RX.findall(body)}

    for qual, col in _QUALIFIED_RX.findall(body):
        q, c = qual.lower(), col.lower()
        if q in known_tables and c not in known_tables[q]:
            problems.append(
                f"column {q}.{c!r} does not exist; {q} has: "
                + ", ".join(sorted(known_tables[q])[:24]))
        elif q not in known_tables and q in aliases and c not in in_scope:
            # Name the real candidates: a model told only "that column does not
            # exist" re-sends the same statement.
            problems.append(
                f"column {qual}.{col!r} does not exist on any table this query "
                "selects from; available: " + ", ".join(sorted(in_scope)[:24]))

    qualified_cols = {c.lower() for _q, c in _QUALIFIED_RX.findall(body)}
    qualifiers = {q.lower() for q, _c in _QUALIFIED_RX.findall(body)}
    for token in _IDENT_RX.findall(body):
        low = token.lower()
        if (low in _SQL_NON_IDENTIFIERS or low in aliases or low in functions
                or low in known_tables or low in qualifiers
                or low in qualified_cols or low in in_scope):
            continue
        problems.append(
            f"identifier {token!r} is not a column of "
            + (", ".join(tables) or "any selected table"))
    # Stable + deduplicated: the same bad column named twice is one problem.
    seen: set[str] = set()
    out: list[str] = []
    for p in problems:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


INTROSPECT_COLUMNS_SQL = (
    "SELECT table_name, column_name, data_type "
    "FROM information_schema.columns "
    "WHERE table_schema = 'public' "
    "ORDER BY table_name, ordinal_position"
)


def introspect_columns(execute, *, max_tables: int = 120,
                       max_columns_per_table: int = 60) -> dict[str, list[str]]:
    """{table: [column, ...]} read from `information_schema.columns`.

    `execute(sql) -> list[tuple]` is injected so this is testable without a
    database and so every read still crosses the same guarded executor.
    """
    rows = execute(INTROSPECT_COLUMNS_SQL) or []
    out: dict[str, list[str]] = {}
    types: dict[str, dict[str, str]] = {}
    for row in rows:
        if not row or len(row) < 2:
            continue
        table = str(row[0])
        column = str(row[1])
        dtype = str(row[2]) if len(row) > 2 else ""
        cols = out.setdefault(table, [])
        if len(cols) < max_columns_per_table and column not in cols:
            cols.append(column)
            types.setdefault(table, {})[column] = dtype
    if len(out) > max_tables:
        keep = sorted(out, key=lambda t: -len(out[t]))[:max_tables]
        out = {t: out[t] for t in sorted(keep)}
    _COLUMN_TYPES.clear()
    _COLUMN_TYPES.update(types)
    return out


# Types are carried alongside so the schema digest can say `text` vs `timestamp`
# without a second round-trip; kept module-level rather than returned so the
# `{table: [column]}` shape that `unknown_identifiers` consumes stays simple.
_COLUMN_TYPES: dict[str, dict[str, str]] = {}


def schema_digest(columns_by_table: dict, *, max_chars: int = 12_000) -> str:
    """Compact, deterministic rendering of the REAL schema for a model prompt.

    This is the artifact that closes gap B: the query builder is handed actual
    column names instead of guessing them.
    """
    lines: list[str] = []
    for table in sorted(columns_by_table or {}):
        cols = columns_by_table[table] or []
        types = _COLUMN_TYPES.get(table) or {}
        rendered = ", ".join(
            (f"{c} {types[c]}" if types.get(c) else str(c)) for c in cols)
        lines.append(f"{table}({rendered})")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[schema truncated]"
    return text


# --------------------------------------------------------------------------- #
# CONNECTING (gap A)
# --------------------------------------------------------------------------- #
def load_driver():
    """Return (module, name).  psycopg (v3) preferred, psycopg2 accepted.

    A MISSING DRIVER IS LOUD.  "couldn't connect" must never be reported as
    "no data problems found", so this raises with the exact install command
    instead of returning None.
    """
    try:
        import psycopg  # type: ignore
        return psycopg, "psycopg"
    except Exception:
        pass
    try:
        import psycopg2  # type: ignore
        return psycopg2, "psycopg2"
    except Exception:
        pass
    raise DriverMissingError(
        "no PostgreSQL driver is importable (tried psycopg, psycopg2). "
        f"Install one with: {DRIVER_INSTALL_HINT}. "
        "psql is NOT an option: it hangs on the Railway proxy.")


def readonly_database_url(env: dict | None = None) -> str:
    return str((env if env is not None else os.environ).get(READONLY_URL_ENV) or "").strip()


def program_url_env(program: str) -> str:
    """The per-program spelling of the connection-string variable."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(program or "")).strip("_").upper()
    return f"{READONLY_URL_ENV}__{slug}" if slug else READONLY_URL_ENV


def resolve_program_url(program: str, program_count: int,
                        env: dict | None = None) -> tuple[dict, str]:
    """({READONLY_URL_ENV: url} or {}, reason-when-empty) for ONE program.

    ONE DATABASE IS NOT EVERY PROGRAM'S DATABASE. The phase read the process
    environment directly, so a single `--program A --program B` invocation
    diagnosed BOTH from whatever database the one variable pointed at, and
    attached A's schema and A's rows to B's report as demonstrated evidence.
    Nothing tied the connection string to a project directory.

    So: a per-program variable (FLEXFACTOR_READONLY_DATABASE_URL__<PROGRAM>)
    always wins and is unambiguous by construction. The bare variable is honored
    only when the invocation audits exactly one program - the case where it
    cannot mean anything else. With several programs and only the bare variable
    set, the phase is SKIPPED with that named reason rather than guessing.
    """
    source = os.environ if env is None else env
    specific = str(source.get(program_url_env(program)) or "").strip()
    if specific:
        return {READONLY_URL_ENV: specific}, ""
    shared = str(source.get(READONLY_URL_ENV) or "").strip()
    if not shared:
        return {}, ""                      # the module's own "not set" verdict
    if int(program_count or 1) <= 1:
        return {READONLY_URL_ENV: shared}, ""
    return {}, (
        f"{READONLY_URL_ENV} is set but this invocation audits "
        f"{program_count} programs, and nothing ties that one database to "
        f"{program!r}. Diagnosing every program from one program's rows would "
        f"attach the wrong evidence to the wrong report, so this phase was "
        f"SKIPPED (this is not evidence that no data-shaped problem exists). "
        f"Set {program_url_env(program)} to run it for this program.")


def availability(env: dict | None = None, connect=None) -> dict:
    """{'available': bool, 'reason': str, 'driver': str|None}.

    UNAVAILABLE IS A VERDICT, NOT A SILENCE.  Every caller prints this reason;
    the audit report carries it verbatim.  There is no code path in which the
    absence of this capability renders as a clean data bill of health.

    `connect` is the caller-supplied opener ReadOnlySession will actually use.
    When one is supplied the installed driver is irrelevant - the connection is
    not made by this module at all - so requiring one would report "no driver,
    therefore no conclusions" about a probe that was perfectly able to run.
    Measured 2026-08-28: the whole probe suite passed on a machine with psycopg
    installed and failed on one without it, for tests that never touch a real
    database, which is a verdict about the host rather than about the program.
    """
    url = readonly_database_url(env)
    if not url:
        return {"available": False, "driver": None,
                "reason": (f"{READONLY_URL_ENV} is not set - FlexFactor has NO "
                           "read path to production data, so NO data-shaped or "
                           "environment-shaped root cause could be looked for "
                           "(this is not evidence that none exists)")}
    if connect is not None:
        return {"available": True, "driver": "caller-supplied",
                "reason": f"{READONLY_URL_ENV} set; connection opened by the caller"}
    try:
        _mod, name = load_driver()
    except DriverMissingError as ex:
        return {"available": False, "driver": None, "reason": str(ex)}
    return {"available": True, "driver": name,
            "reason": f"{READONLY_URL_ENV} set; driver {name}"}


def connect_timeout_s(env: dict | None = None) -> int:
    """Seconds to wait for the production connection itself. Bounded [1, 60]."""
    raw = (env if env is not None else os.environ).get(CONNECT_TIMEOUT_ENV)
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return DEFAULT_CONNECT_TIMEOUT_S
    return max(1, min(value, 60))


def statement_timeout_ms(env: dict | None = None) -> int:
    raw = (env if env is not None else os.environ).get(STATEMENT_TIMEOUT_ENV)
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return DEFAULT_STATEMENT_TIMEOUT_MS
    return max(1_000, min(value, 120_000))


class ReadOnlySession:
    """A guarded read-only session against the owner-supplied connection string.

    BELT AND BRACES.  `assert_read_only_diagnostic_sql` is the primary
    perimeter; this class adds two independent server-side ones, so a guard bug
    still cannot write:
      * the transaction is opened READ ONLY (Postgres refuses any write in it);
      * `statement_timeout` bounds every statement.
    """

    def __init__(self, url: str, *, connect=None, timeout_ms: int | None = None,
                 row_limit: int = DEFAULT_ROW_LIMIT):
        if not url:
            raise ProdEvidenceUnavailable(
                f"{READONLY_URL_ENV} is not set; there is no connection string")
        self.url = url
        self.row_limit = row_limit
        self.timeout_ms = timeout_ms or statement_timeout_ms()
        self._connect = connect
        self.driver_name: str | None = None
        self.conn = None
        self.queries: list[str] = []
        self.truncated_schema = False

    def __enter__(self) -> "ReadOnlySession":
        if self._connect is None:
            mod, name = load_driver()
            self.driver_name = name
            # A BOUNDED CONNECT. `statement_timeout` is issued below - AFTER the
            # connection succeeds - so it cannot bound the connection attempt
            # itself. Against an unreachable or blackholed host libpq waits for
            # the OS to give up, which can be minutes, and this phase is
            # explicitly a non-fatal side quest: it must never be able to stall
            # the audit that owns it. libpq takes connect_timeout in seconds.
            seconds = max(1, int(connect_timeout_s()))
            try:
                self.conn = mod.connect(self.url, connect_timeout=seconds)
            except TypeError:
                # A driver that does not take the keyword still gets a
                # connection, and the fact that it is unbounded is said out
                # loud rather than assumed away.
                self.conn = mod.connect(self.url)
        else:
            self.driver_name = "injected"
            self.conn = self._connect(self.url)
        # Both server-side guards are issued through the driver, never through
        # the validated-SQL path: they are SET statements, which the read-only
        # SQL guard correctly refuses.
        cur = self.conn.cursor()
        cur.execute(f"SET statement_timeout = {int(self.timeout_ms)}")
        cur.execute("SET default_transaction_read_only = on")
        cur.execute("SET TRANSACTION READ ONLY")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        conn = self.conn
        self.conn = None
        if conn is not None:
            try:
                conn.rollback()          # never commit: nothing was written
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        return False

    def schema_execute(self, sql: str) -> list[tuple]:
        """A metadata read: raw rows allowed, bounded by SCHEMA_ROW_LIMIT.

        Truncation is not silent - a result that exactly fills the bound means
        the schema handed to the planner may be a prefix of the real one, and
        that has to be said rather than presented as the whole schema."""
        rows = self.execute(sql, limit=SCHEMA_ROW_LIMIT, allow_raw_rows=True)
        if len(rows) >= SCHEMA_ROW_LIMIT:
            self.truncated_schema = True
        return rows

    def execute(self, sql: str, *, limit: int | None = None,
                allow_raw_rows: bool = False) -> list[tuple]:
        """Validate -> clamp -> execute.  Every read in this module goes here."""
        if self.conn is None:
            raise ProdEvidenceUnavailable("read-only session is not open")
        final_sql = clamp_read_only_sql(
            sql, self.row_limit if limit is None else limit,
            allow_raw_rows=allow_raw_rows)
        self.queries.append(final_sql)
        cur = self.conn.cursor()
        cur.execute(final_sql)
        try:
            rows = cur.fetchall()
        except Exception:
            rows = []
        return [tuple(r) for r in (rows or [])]

    def describe(self, sql: str) -> dict:
        """Execute and return a report-shaped record (columns + rows)."""
        final_sql = clamp_read_only_sql(sql, self.row_limit)
        cur = self.conn.cursor()
        cur.execute(final_sql)
        cols = [d[0] for d in (cur.description or [])]
        try:
            rows = cur.fetchall()
        except Exception:
            rows = []
        self.queries.append(final_sql)
        return {"sql": final_sql, "columns": cols,
                "rows": [list(r) for r in (rows or [])],
                "row_count": len(rows or [])}


def render_rows(record: dict, *, max_rows: int = 30, max_chars: int = 4_000) -> str:
    """Deterministic text rendering of a result set for a model prompt/report."""
    cols = record.get("columns") or []
    rows = record.get("rows") or []
    out = [" | ".join(str(c) for c in cols)]
    for row in rows[:max_rows]:
        out.append(" | ".join("" if v is None else str(v) for v in row))
    if len(rows) > max_rows:
        out.append(f"[{len(rows) - max_rows} more row(s) not shown]")
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars] + "\n[truncated]"


# --------------------------------------------------------------------------- #
# THE PHASE (gap A + B feeding gap F)
# --------------------------------------------------------------------------- #
DIAGNOSTIC_PLAN_SYSTEM = (
    "You are a production database diagnostician. You are given the REAL schema "
    "of a live production database (read from information_schema - these are the "
    "actual table and column names; do NOT invent any others) and the program's "
    "purpose. Propose read-only diagnostic queries whose RESULTS could reveal a "
    "DATA-shaped, ENVIRONMENT-shaped, CLIENT-shaped or CONFIGURATION-shaped "
    "problem that reading source code cannot reveal - for example a cohort of "
    "sessions from one client family behaving differently from all the others, "
    "rows mass-mutated by a single sweep, a lookup that returns nothing because "
    "the data was never populated, or a flag set to a value the code never "
    "expects.\n\n"
    "HARD CONSTRAINTS on every query, enforced in code before it runs:\n"
    "  * exactly ONE SELECT statement - no subquery, no CTE, no UNION;\n"
    "  * no semicolon;\n"
    "  * no DROP/DELETE/UPDATE/INSERT/ALTER/CREATE/TRUNCATE/GRANT/REVOKE/INTO;\n"
    "  * every table and column you name MUST appear in the schema given to you.\n"
    "A query naming a column that does not exist is REJECTED before execution. "
    "Aggregate (GROUP BY / COUNT) rather than selecting raw rows: this is "
    "production data and must not be exfiltrated row by row. Respond with JSON only."
)

DIAGNOSTIC_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Short slug for this probe."},
                    "question": {"type": "string",
                                 "description": "The question the result answers."},
                    "sql": {"type": "string",
                            "description": "One read-only SELECT, no semicolon."},
                },
                "required": ["name", "question", "sql"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["queries"],
    "additionalProperties": False,
}

RUNTIME_FINDING_SYSTEM = (
    "You are diagnosing a live production system from REAL query results. Your "
    "verdict is NOT a code verdict: you are looking for a root cause that lives "
    "in the DATA, the ENVIRONMENT, the CLIENT, or the CONFIGURATION - the class "
    "of problem where NO code is wrong and a patch would be an invention.\n\n"
    "Report a finding ONLY when the rows you were shown demonstrate it. Quote "
    "the actual numbers in `evidence`. If the results show nothing anomalous, "
    "return an empty list - a fabricated finding is far worse than none.\n\n"
    "Every finding you return is REPORTED TO THE OWNER AS A BRIEF and is NEVER "
    "turned into a patch, so `next_step` must be the concrete human action that "
    "resolves it (a config change, a client/browser workaround, a data repair, "
    "an environment variable), not a code edit. Respond with JSON only."
)

RUNTIME_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string",
                                 "enum": ["data", "environment", "client",
                                          "configuration"],
                                 "description": (
                                     "data = the stored rows are wrong/missing/mass-mutated; "
                                     "environment = the deployment or platform (region, proxy, "
                                     "TLS, clock, resource limit); client = the user's browser, "
                                     "app or device (in-app webview, cookie policy, cache); "
                                     "configuration = a setting/flag/env var set to a value the "
                                     "system does not handle.")},
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium", "low", "info"]},
                    "title": {"type": "string"},
                    "problem": {"type": "string",
                                "description": "What is wrong and how users experience it."},
                    "evidence": {"type": "string",
                                 "description": "The ACTUAL numbers/rows from the query "
                                                "results that prove it."},
                    "query_name": {"type": "string",
                                   "description": "Which probe produced the evidence."},
                    "next_step": {"type": "string",
                                  "description": "The concrete NON-CODE action that "
                                                 "resolves it."},
                },
                "required": ["category", "severity", "title", "problem",
                             "evidence", "query_name", "next_step"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# The pseudo-file every runtime-data finding is filed under, so it can never be
# mistaken for a source file with a patchable line.
RUNTIME_EVIDENCE_FILE = "(runtime-data)"


def collect_runtime_evidence(judge, purpose_blob: str, *, env: dict | None = None,
                             connect=None, log=None, max_queries: int = 6,
                             row_limit: int = DEFAULT_ROW_LIMIT) -> dict:
    """Schema discovery -> guarded probes -> NON-CODE findings.

    `judge(system, prompt, schema) -> dict` is injected (FlexFactor's `_judge`),
    so this module holds no provider knowledge and is testable offline.

    Returns a record that ALWAYS states what happened:
      {"available": bool, "reason": str, "tables": int, "queries": [...],
       "rejected": [...], "findings": [...], "errors": [...]}
    `available: False` with a reason is the honest answer when there is no read
    path.  It is never rendered as "no data problems found".
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    avail = availability(env, connect=connect)
    record: dict = {"available": bool(avail["available"]), "reason": avail["reason"],
                    "driver": avail.get("driver"), "tables": 0, "queries": [],
                    "rejected": [], "findings": [], "errors": []}
    if not avail["available"]:
        _log(f"runtime-data evidence UNAVAILABLE: {avail['reason']}")
        return record

    url = readonly_database_url(env)
    try:
        with ReadOnlySession(url, connect=connect, row_limit=row_limit) as session:
            # ---- gap B: SCHEMA FIRST, always, before any query is written ----
            columns_by_table = introspect_columns(session.schema_execute)
            record["tables"] = len(columns_by_table)
            if getattr(session, "truncated_schema", False):
                record["errors"].append(
                    f"information_schema.columns filled the {SCHEMA_ROW_LIMIT}-row "
                    "read bound, so the schema below may be a PREFIX of the real "
                    "one (the query orders by table name, so later tables are the "
                    "ones missing). Probes against those tables will be rejected "
                    "as nonexistent - raise FLEXFACTOR_SCHEMA_ROW_LIMIT")
                _log(record["errors"][-1])
            if not columns_by_table:
                record["errors"].append(
                    "information_schema.columns returned no rows for schema "
                    "'public' - the connection works but exposes no tables")
                _log(record["errors"][-1])
                return record
            _log(f"runtime-data: {len(columns_by_table)} table(s) introspected "
                 "from information_schema.columns")

            digest = schema_digest(columns_by_table)
            plan = judge(
                DIAGNOSTIC_PLAN_SYSTEM,
                "PROGRAM PURPOSE:\n" + (purpose_blob or "(not supplied)")[:4000]
                + "\n\nREAL PRODUCTION SCHEMA (authoritative - every name you use "
                  "must come from here):\n" + digest
                + "\n\nPropose the diagnostic queries.",
                DIAGNOSTIC_PLAN_SCHEMA) or {}
            proposed = [q for q in (plan.get("queries") or [])
                        if isinstance(q, dict)][:max_queries]
            if not proposed:
                record["errors"].append("the planner proposed no diagnostic queries")
                _log(record["errors"][-1])
                return record

            results: list[dict] = []
            for q in proposed:
                name = str(q.get("name") or "probe")
                sql = str(q.get("sql") or "")
                try:
                    assert_read_only_diagnostic_sql(sql)
                except ReadOnlySqlError as ex:
                    record["rejected"].append({"name": name, "sql": sql,
                                               "reason": f"read-only guard: {ex}"})
                    _log(f"runtime-data: REJECTED probe {name}: {ex}")
                    continue
                unknown = unknown_identifiers(sql, columns_by_table)
                if unknown:
                    record["rejected"].append(
                        {"name": name, "sql": sql,
                         "reason": "schema mismatch: " + "; ".join(unknown[:4])})
                    _log(f"runtime-data: REJECTED probe {name} (hallucinated "
                         f"name): {unknown[0]}")
                    continue
                try:
                    out = session.describe(sql)
                except Exception as ex:      # a failed probe is NAMED, not hidden
                    record["errors"].append(f"{name}: {type(ex).__name__}: {ex}")
                    _log(f"runtime-data: probe {name} failed: {ex}")
                    continue
                out["name"] = name
                out["question"] = str(q.get("question") or "")
                results.append(out)
                record["queries"].append({"name": name, "question": out["question"],
                                          "sql": out["sql"],
                                          "row_count": out["row_count"]})
            if not results:
                record["errors"].append(
                    "no diagnostic probe executed successfully, so NO data-shaped "
                    "conclusion could be drawn (this is not a clean bill of health)")
                _log(record["errors"][-1])
                return record

            blocks = []
            for out in results:
                blocks.append(f"### {out['name']} - {out['question']}\n"
                              f"SQL: {out['sql']}\n{render_rows(out)}")
            verdict = judge(
                RUNTIME_FINDING_SYSTEM,
                "PROGRAM PURPOSE:\n" + (purpose_blob or "(not supplied)")[:2000]
                + "\n\nREAL QUERY RESULTS FROM PRODUCTION:\n\n"
                + "\n\n".join(blocks)
                + "\n\nReport only what these rows demonstrate.",
                RUNTIME_FINDING_SCHEMA) or {}
            # A FINDING MUST CITE A PROBE THAT RAN. Every dict the verdict model
            # returned used to be accepted, so a stale or invented `query_name`
            # was presented in the report as "demonstrated by production rows"
            # when no such query existed - the strongest claim this module makes,
            # resting on nothing. Rejections are recorded, not dropped: a model
            # inventing evidence is itself a finding about the run.
            executed = {str(out["name"]) for out in results}
            for f in (verdict.get("findings") or []):
                if not isinstance(f, dict):
                    continue
                cited = str(f.get("query_name") or "")
                if cited not in executed:
                    record["rejected"].append({
                        "name": cited or "(no query_name)",
                        "sql": "",
                        "reason": ("verdict cited a probe that never executed; "
                                   "probes that ran: "
                                   + (", ".join(sorted(executed)) or "none"))})
                    _log(f"runtime-data: REJECTED finding citing unexecuted probe "
                         f"{cited!r}")
                    continue
                record["findings"].append(f)
    except DriverMissingError as ex:
        record["available"] = False
        record["reason"] = str(ex)
        _log(f"runtime-data evidence UNAVAILABLE: {ex}")
    except Exception as ex:
        # A CONNECTION FAILURE IS NOT A CLEAN RESULT.  available stays True (the
        # owner did configure the capability) and the error is carried into the
        # report, so the run cannot read as "no data problems found".
        record["errors"].append(f"{type(ex).__name__}: {ex}")
        _log(f"runtime-data evidence FAILED: {type(ex).__name__}: {ex}")
    return record


def runtime_findings(record: dict) -> list[dict]:
    """Map runtime-evidence verdicts onto FlexFactor's finding shape.

    `evidence_source='runtime-data'` and a non-code `category` are what make
    `flexfactor.should_fix_finding` refuse to author a patch for these, and what
    routes them into the owner-facing brief instead.
    """
    out: list[dict] = []
    for f in (record or {}).get("findings") or []:
        category = str(f.get("category") or "data").strip().lower()
        severity = str(f.get("severity") or "medium").strip().lower()
        evidence = str(f.get("evidence") or "")
        probe = str(f.get("query_name") or "")
        out.append({
            "file": RUNTIME_EVIDENCE_FILE,
            "line": 0,
            "severity": severity,
            "category": category,
            "evidence_source": "runtime-data",
            "code_fixable": False,
            "title": str(f.get("title") or "runtime-data finding"),
            "problem": str(f.get("problem") or ""),
            "evidence": evidence,
            "query_name": probe,
            "next_step": str(f.get("next_step") or ""),
            # `fix` is the field every existing renderer prints; for a non-code
            # finding it carries the OWNER ACTION, never a code edit.
            "fix": str(f.get("next_step") or ""),
        })
    return out
