"""Persistence-defect gates for flexfactor_prodready.assess_readiness.

Stdlib only. Never imports flexfactor. Never launches a subprocess.
Called from assess_readiness so the rubric stays one engine — this module
is the GrantFlow Factory Deck class (PR #1266 / SHA 3060385), not a
second scorecard.

Fail closed and honest: unread/unparsed evidence is not a pass. `na`
only when the project has no such surface. Router-rewrite-vs-nest is
omitted (false-flood risk).
"""
from __future__ import annotations

import os
import re

# Bound every walk: first N product files, first N migrations, prefix reads.
MAX_CONFIG_BYTES = 512 * 1024
MAX_PERSISTENCE_SOURCE_FILES = 400
MAX_PERSISTENCE_SQL_FILES = 80
_SQL_READ_LIMIT = 2 * 1024 * 1024
_JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_FRONTEND_ROOTS = frozenset({"src", "app", "ui"})
_SPINE_PATHS = (
    "src/api/client.js",
    "backend/db/migrate.js",
    "backend/server.js",
    "backend/db/schema.sql",
)
_SCHEMA_CANDIDATES = (
    "backend/db/schema.sql",
    "db/schema.sql",
    "backend/schema.sql",
)
_TEST_DIR_NAMES = frozenset({"test", "tests", "spec", "specs", "__tests__",
                             "e2e", "it", "testing"})
_TEST_FILE_PAT = re.compile(
    r"(?:^|[/\\])(?:tests?[_.-][^/\\]+|[^/\\]+[_.-]tests?|[^/\\]+\.(?:test|spec))"
    r"\.[\w]+$", re.I)
_OVERLAY_ROOT_RX = re.compile(r"^_(?:gh|restore)_")
_CLIENT_COUNTER_RX = re.compile(
    r"(?:"
    r"last_invoice_number|lastInvoiceNumber|"
    r"last_order_number|lastOrderNumber|"
    r"last_ticket_number|lastTicketNumber|"
    r"next_invoice_number|nextInvoiceNumber|"
    r"\bnextInvoice\b|"
    r"lastNumber\s*\+\s*1|"
    r"last_number\s*\+\s*1|"
    r"(?:invoice|order|ticket)_number\s*=\s*[^=;\n]{0,80}\+\s*1"
    r")",
    re.I,
)
_NEXT_NUMBER_RX = re.compile(r"\bnextNumber\s*[=:]", re.I)
_COUNTER_DOMAIN_RX = re.compile(r"\b(?:invoice|order|ticket)\b", re.I)
_STUB_CLIENT_RX = re.compile(r"createStubEntityClient|in-memory stub", re.I)
_KNOWN_STUBS_ASSIGN_RX = re.compile(
    r"KNOWN_STUB_ENTITIES\s*=\s*(\[[^\]]*\]|\{[^}]*\})",
    re.S,
)
_CREATE_TABLE_RX = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:\w+|\"[^\"]+\"|`[^`]+`)\.)?"
    r"(?:\"([^\"]+)\"|`([^`]+)`|'([^']+)'|(\w+))",
    re.I,
)
_NUMBERED_SQL_RX = re.compile(r"/\d{3,}[^/]*\.sql$", re.I)
_EXTRAS_BOOTSTRAP_RX = re.compile(
    r"workspacePersistenceTables|ensureSqliteSchema|applyWorkspace",
    re.I,
)


def _norm_rel(rel: str) -> str:
    return rel.replace("\\", "/")


def _is_test_path(rel: str) -> bool:
    rel_n = _norm_rel(rel)
    parts = rel_n.lower().split("/")
    if any(p in _TEST_DIR_NAMES for p in parts[:-1]):
        return True
    return bool(_TEST_FILE_PAT.search(rel_n))


def _is_frontend_product(rel: str) -> bool:
    if _is_test_path(rel):
        return False
    rel_n = _norm_rel(rel)
    if not rel_n.lower().endswith(_JS_EXTS):
        return False
    return rel_n.split("/", 1)[0].lower() in _FRONTEND_ROOTS


def _is_js_ts_product(rel: str) -> bool:
    if _is_test_path(rel):
        return False
    return _norm_rel(rel).lower().endswith(_JS_EXTS)


def _read_prefix(path: str, limit: int = MAX_CONFIG_BYTES) -> str:
    """Read the first `limit` characters even when the file is larger."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except (OSError, ValueError):
        return ""


def _file_size(project_dir: str, rel: str) -> int | None:
    path = os.path.join(project_dir, *_norm_rel(rel).split("/"))
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError:
        return None
    return None


def _known_stubs_nonempty(text: str) -> str | None:
    if "KNOWN_STUB_ENTITIES" not in text:
        return None
    m = _KNOWN_STUBS_ASSIGN_RX.search(text)
    if not m:
        return "KNOWN_STUB_ENTITIES (assignment unparsed — fail closed)"
    inner = m.group(1)[1:-1]
    cleaned = re.sub(r"//.*?$|/\*.*?\*/", "", inner, flags=re.M | re.S)
    cleaned = re.sub(r"[\s,]", "", cleaned)
    if not cleaned:
        return None
    return "KNOWN_STUB_ENTITIES has members"


def _sql_create_tables(text: str) -> set[str]:
    names: set[str] = set()
    for m in _CREATE_TABLE_RX.finditer(text or ""):
        name = next((g for g in m.groups() if g), None)
        if name:
            names.add(name.strip().lower())
    return names


def _mentions_ident(text: str, name: str) -> bool:
    return bool(re.search(rf"\b{re.escape(name)}\b", text or "", re.I))


def _is_numbered_sqlite_migration(rel: str) -> bool:
    n = _norm_rel(rel)
    if not _NUMBERED_SQL_RX.search(n):
        return False
    lower = n.lower()
    if "/postgres/" in lower:
        return False
    return "/migrations/" in lower


def _is_numbered_postgres_migration(rel: str) -> bool:
    n = _norm_rel(rel)
    return "/postgres/migrations/" in n.lower() and bool(_NUMBERED_SQL_RX.search(n))


def _is_extras_bootstrap(rel: str) -> bool:
    return bool(_EXTRAS_BOOTSTRAP_RX.search(_norm_rel(rel)))


def _scan_client_unique_counters(project_dir: str, files: list[str]) -> list[str]:
    hits: list[str] = []
    scanned = 0
    for rel in files:
        if not _is_frontend_product(rel):
            continue
        scanned += 1
        if scanned > MAX_PERSISTENCE_SOURCE_FILES:
            break
        text = _read_prefix(os.path.join(project_dir, *_norm_rel(rel).split("/")))
        if not text:
            continue
        if _CLIENT_COUNTER_RX.search(text) or (
                _NEXT_NUMBER_RX.search(text) and _COUNTER_DOMAIN_RX.search(text)):
            hits.append(_norm_rel(rel))
            if len(hits) >= 8:
                break
    return hits


def _scan_entity_stubs(project_dir: str, files: list[str]) -> list[str]:
    hits: list[str] = []
    scanned = 0
    for rel in files:
        if not _is_js_ts_product(rel):
            continue
        scanned += 1
        if scanned > MAX_PERSISTENCE_SOURCE_FILES:
            break
        text = _read_prefix(os.path.join(project_dir, *_norm_rel(rel).split("/")))
        if not text:
            continue
        why = []
        if _STUB_CLIENT_RX.search(text):
            why.append("createStubEntityClient/in-memory stub")
        nonempty = _known_stubs_nonempty(text)
        if nonempty:
            why.append(nonempty)
        if why:
            hits.append(f"{_norm_rel(rel)} ({'; '.join(why)})")
            if len(hits) >= 8:
                break
    return hits


def _root_factory_overlays(files: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in files:
        norm = _norm_rel(rel)
        if "/" in norm:
            continue
        if _OVERLAY_ROOT_RX.match(norm):
            hits.append(norm)
            if len(hits) >= 12:
                break
    return hits


def _spine_collapse_reasons(project_dir: str, files: list[str]) -> list[str]:
    present = []
    file_set = {_norm_rel(f) for f in files}
    for p in _SPINE_PATHS:
        if p in file_set or _file_size(project_dir, p) is not None:
            present.append(p)
    if not present:
        return []

    schema_size = _file_size(project_dir, "backend/db/schema.sql") or 0
    reasons: list[str] = []
    for rel in present:
        size = _file_size(project_dir, rel)
        if size is None:
            continue
        # REASON SHAPE IS LOAD-BEARING: "<rel> (<why>)" is the one form
        # `_paths_from_hits` can recover a file from (it splits on " ("). The
        # old strings put prose between the rel and the paren - "<rel> is N
        # bytes (...)" - so recovery yielded "backend/server.js is 412 bytes",
        # not a file, the gate shipped no `paths`, and this HIGH (blocking)
        # gate was filed against the "(readiness)" placeholder the fix loop
        # never edits: reported every run, fixable never.
        if rel == "backend/server.js":
            if size < 800:
                reasons.append(f"{rel} ({size} bytes; collapsed Express spine)")
            elif size < 5 * 1024 and schema_size >= 50 * 1024:
                reasons.append(
                    f"{rel} ({size} bytes beside a {schema_size}-byte schema.sql)")
        elif rel == "src/api/client.js":
            text = _read_prefix(os.path.join(project_dir, *rel.split("/")), 16 * 1024)
            if size < 250:
                reasons.append(f"{rel} ({size} bytes; API client collapsed)")
            elif size < 800 and re.search(
                    r"createStub|not implemented|TODO stub", text, re.I):
                reasons.append(f"{rel} ({size}-byte stub client)")
        elif rel == "backend/db/migrate.js":
            if size < 200:
                reasons.append(f"{rel} ({size} bytes; migrator collapsed)")
            elif size < 400 and schema_size >= 10 * 1024:
                reasons.append(
                    f"{rel} ({size} bytes beside a {schema_size}-byte schema.sql)")
        elif rel == "backend/db/schema.sql":
            if size < 80:
                reasons.append(f"{rel} ({size} bytes; schema emptied)")
    return reasons


def _schema_bootstrap_holes(project_dir: str, files: list[str]
                            ) -> tuple[str, list[str]]:
    file_set = {_norm_rel(f) for f in files}
    schema_rel = next((p for p in _SCHEMA_CANDIDATES
                       if p in file_set or _file_size(project_dir, p) is not None),
                      None)
    sqlite_migs = [f for f in files if _is_numbered_sqlite_migration(f)]
    pg_migs = [f for f in files if _is_numbered_postgres_migration(f)]
    if not schema_rel or not sqlite_migs:
        return "na", []

    schema_text = _read_prefix(
        os.path.join(project_dir, *schema_rel.split("/")), _SQL_READ_LIMIT)
    extras_rels = [f for f in files if _is_extras_bootstrap(f)][:MAX_PERSISTENCE_SQL_FILES]
    extras_texts = [
        _read_prefix(os.path.join(project_dir, *_norm_rel(rel).split("/")),
                     _SQL_READ_LIMIT)
        for rel in extras_rels
    ]
    pg_texts = [
        _read_prefix(os.path.join(project_dir, *_norm_rel(rel).split("/")),
                     _SQL_READ_LIMIT)
        for rel in pg_migs[:MAX_PERSISTENCE_SQL_FILES]
    ]

    fresh_miss: list[str] = []
    twin_miss: list[str] = []
    saw_create = False
    for rel in sqlite_migs[:MAX_PERSISTENCE_SQL_FILES]:
        text = _read_prefix(
            os.path.join(project_dir, *_norm_rel(rel).split("/")), _SQL_READ_LIMIT)
        for table in sorted(_sql_create_tables(text)):
            saw_create = True
            in_schema = _mentions_ident(schema_text, table)
            in_extras = any(_mentions_ident(t, table) for t in extras_texts)
            if not in_schema and not in_extras:
                fresh_miss.append(f"{table} ({_norm_rel(rel)})")
            elif pg_migs and not in_schema and not any(
                    _mentions_ident(t, table) for t in pg_texts):
                # DELIBERATELY extras-only (authored this way in the original
                # GrantFlow-class commit, re-examined 2026-08-30): schema.sql
                # is the engine-shared fresh-DB bootstrap, so a table it names
                # is covered on Postgres too. Only a table whose sole coverage
                # is a sqlite-specific extras bootstrap (ensureSqliteSchema /
                # workspacePersistenceTables) needs its own Postgres twin. The
                # gate's remediation text used to demand the twin for EVERY
                # table, promising more than this check verifies - the text is
                # now scoped to match.
                twin_miss.append(f"{table} ({_norm_rel(rel)}; no postgres twin)")
            if len(fresh_miss) + len(twin_miss) >= 8:
                break
        if len(fresh_miss) + len(twin_miss) >= 8:
            break

    if not saw_create:
        return "na", []
    holes = fresh_miss + twin_miss
    if holes:
        return "fail", holes
    return "pass", []


def _paths_from_hits(project_dir: str, hits: list[str]) -> list[str]:
    """Repo-relative files named by a gate's own hit strings.

    These gates already know exactly which file is wrong - `_scan_entity_stubs`
    emits "<rel> (<why>)" and the counter scan emits a bare "<rel>" - but that
    knowledge died in a prose evidence string, so the blocker reached the audit
    with no file and could never be handed to the fix loop.

    Never a guess: a candidate is kept only when it exists on disk, because a
    caller must be able to open what it is handed.
    """
    out: list[str] = []
    for hit in hits or []:
        # "<rel> (<why>)" -> "<rel>"; a bare "<rel>" is unchanged.
        cand = str(hit).split(" (", 1)[0].strip().replace("\\", "/")
        if not cand or cand in out:
            continue
        if os.path.isfile(os.path.join(project_dir, cand)):
            out.append(cand)
    return out


def apply_persistence_gates(add, project_dir: str, files: list[str]) -> None:
    """Append the five high persistence gates onto assess_readiness via add()."""
    counter_hits = _scan_client_unique_counters(project_dir, files)
    add(id="no_client_unique_counters",
        title="Unique counters are minted on the server",
        status="fail" if counter_hits else "pass",
        severity="high",
        evidence=("client-minted counter: " + ", ".join(counter_hits[:5]))
        if counter_hits else "no frontend unique-counter increment",
        remediation="Mint invoice/order/ticket numbers with an atomic server/DB "
                    "counter (ON CONFLICT / RETURNING). Do not increment "
                    "last_invoice_number / lastNumber+1 in the browser.",
        paths=_paths_from_hits(project_dir, counter_hits))

    js_layer = any(_is_js_ts_product(f) for f in files)
    stub_hits = _scan_entity_stubs(project_dir, files) if js_layer else []
    add(id="no_in_memory_entity_stubs",
        title="User-facing entities persist beyond in-memory stubs",
        status="na" if not js_layer else ("fail" if stub_hits else "pass"),
        severity="high",
        evidence=("in-memory stub: " + ", ".join(stub_hits[:5]))
        if stub_hits else (
            "no JS/TS client layer" if not js_layer
            else "no createStubEntityClient / populated KNOWN_STUB_ENTITIES"),
        remediation="Replace createStubEntityClient / KNOWN_STUB_ENTITIES with "
                    "a real persist path. A named user-facing entity that only "
                    "lives in a Map (toast-then-vanish) is not production-ready.",
        paths=_paths_from_hits(project_dir, stub_hits))

    overlay_hits = _root_factory_overlays(files)
    add(id="no_factory_overlay",
        title="No leftover factory overlay files at repo root",
        status="fail" if overlay_hits else "pass",
        # MEDIUM, not high, on purpose (2026-08-30): high makes this a BLOCKER,
        # and a blocker must be closable by an action the run can take. The
        # only remedy here is DELETING files, which the text-editing fix loop
        # cannot do - so at high severity a repo with one leftover _gh_* file
        # was permanently NOT PRODUCTION READY with no closing action, the
        # unclosable-finding shape this codebase keeps re-hitting. Junk at the
        # repo root is real and stays REPORTED with the manual action named;
        # it does not veto the verdict.
        severity="medium",
        evidence=("root overlay: " + ", ".join(overlay_hits[:8]))
        if overlay_hits else "no tracked _gh_* / _restore_* at repo root",
        remediation="Delete leftover Factory Deck overlay files (_gh_*, _restore_*) "
                    "from the repository root - a manual (or autoclean) step; "
                    "FlexFactor's fix loop edits files and cannot remove them. "
                    "They are not part of the product.")

    spine_present = False
    file_set = {_norm_rel(f) for f in files}
    for p in _SPINE_PATHS:
        if p in file_set or _file_size(project_dir, p) is not None:
            spine_present = True
            break
    spine_reasons = _spine_collapse_reasons(project_dir, files) if spine_present else []
    add(id="spine_modules_intact",
        title="Host spine modules are not collapsed stubs",
        status="na" if not spine_present else ("fail" if spine_reasons else "pass"),
        severity="high",
        evidence="; ".join(spine_reasons[:5]) if spine_reasons else (
            "no host spine paths" if not spine_present
            else "spine modules present and not implausibly tiny"),
        remediation="Restore backend/server.js, src/api/client.js, "
                    "backend/db/migrate.js, and backend/db/schema.sql. Do not "
                    "replace the host spine with a 2-route stub.",
        # The collapsed file itself is the thing to fix; without paths this
        # HIGH gate landed on the "(readiness)" placeholder - see the reason
        # shape note in _spine_collapse_reasons.
        paths=_paths_from_hits(project_dir, spine_reasons))

    schema_status, schema_holes = _schema_bootstrap_holes(project_dir, files)
    add(id="schema_bootstrap_covers_extras",
        title="Fresh-DB schema bootstrap covers migrated tables",
        status=schema_status,
        severity="high",
        evidence=("fresh-DB miss: " + ", ".join(schema_holes[:5]))
        if schema_holes else (
            "no dual schema.sql + numbered-migration layout"
            if schema_status == "na"
            else "migrated tables named in schema.sql or extras bootstrap"),
        remediation="Add new tables to the fresh-DB schema.sql (or an "
                    "IF NOT EXISTS extras file applied after it: "
                    "workspacePersistenceTables / ensureSqliteSchema / "
                    "applyWorkspace); a table covered ONLY by a "
                    "sqlite-specific extras bootstrap also needs its Postgres "
                    "twin when backend/db/postgres/migrations exists. A hidden "
                    "`npm run migrate` must not be required to create them.",
        # The file a remediation EDITS is the fresh-DB schema, not the migration
        # its evidence quotes: the hole is that schema.sql does not create the
        # table, and the migration is already correct. Pointing the fix loop at
        # the migration would ask it to repair a file that is not broken.
        paths=([schema] if schema_status == "fail" and
               (schema := next((p for p in _SCHEMA_CANDIDATES
                                if os.path.isfile(os.path.join(project_dir, *p.split("/")))),
                               None)) else []))
