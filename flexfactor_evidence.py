#!/usr/bin/env python3
"""Deterministic repository intelligence and execution-evidence support.

This module is deliberately provider-neutral and stdlib-only.  It gives the
FlexFactor audit loop facts that a model must never be allowed to invent:

* every relevant tracked file and its exact hash;
* symbols, imports, routes, controls, data/config boundaries, and reverse
  dependency blast radius;
* per-file, per-function, and per-workflow execution ledgers;
* normalized quality gates and SARIF output; and
* an append-only, secret-redacted event stream plus an atomic evidence bundle.

The parser is intentionally conservative.  A symbol it cannot prove is marked
unknown rather than absent, and a test command that did not run is never a pass.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

try:
    import flexfactor_ledger as _ff_ledger
except ImportError:  # running as a spec-loaded module: try the file's own dir
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import flexfactor_ledger as _ff_ledger



SCHEMA = "flexfactor.evidence.v1"
INDEX_SCHEMA = "flexfactor.code_index.v1"
PURPOSE_GRAPH_SCHEMA = "flexfactor.purpose_graph.v1"
COVERAGE_SCHEMA = "flexfactor.coverage_ledger.v1"
GATES_SCHEMA = "flexfactor.quality_gates.v1"

SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".cs",
    ".swift", ".dart", ".vue", ".svelte", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".sql", ".sh", ".ps1",
}
TEXT_CONFIG_EXTENSIONS = {
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".xml",
    ".md", ".txt", ".env", ".properties", ".gradle",
}
TEST_MARKERS = ("/test/", "/tests/", "__tests__", ".test.", ".spec.",
                "_test.", "_tests.")
SKIP_DIRS = {
    ".git", "node_modules", "vendor", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "coverage", "__pycache__", ".pytest_cache",
}

# THE ONE STATUS VOCABULARY BOTH INVENTORY GATES KEY ON.
# "analyzed-in-chunks" IS a successful analysis: `_index_large_file_in_chunks`
# returns it when the chunk ledger accounts for every chunk, and
# totals["analyzed_source_files"] counts it.  That same helper returns
# "blocked" for a file that was HASHED AND NEVER SCANNED - past the 64MB hard
# cap, unreadable, or an incomplete ledger.  Spelling the set out twice let
# "blocked" pass the inventory gate as "complete" and let "analyzed-in-chunks"
# fail the rescan gate, so a repo with one changed >4MB source file could never
# converge and the stated reason was wrong.
ANALYZED_SOURCE_STATUSES = frozenset({"analyzed", "analyzed-in-chunks"})

_SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{30,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{24,}\b")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)
_REDACT_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*"
    r"([\"']?)([^\s,;\"']{8,})\2"
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _redact(value: Any) -> Any:
    """Recursively remove credential-shaped material before persistence."""
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    for _kind, rx in _SECRET_PATTERNS:
        out = rx.sub("[REDACTED]", out)
    out = _REDACT_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[REDACTED]", out)
    return out


def atomic_json(path: str | os.PathLike[str], payload: Any) -> str:
    path = os.path.abspath(os.fspath(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".flexfactor-", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(_redact(payload), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


class EventLedger:
    """Append-only observable event model suitable for OTLP translation.

    A JSONL event contains trace/run identity, event name, UTC timestamp,
    duration/cost fields when supplied, and redacted attributes.  Hooks are
    ordinary in-process callables; one broken hook is recorded and cannot abort
    the engineering work.
    """

    def __init__(self, path: str, run_id: str, hooks: Iterable | None = None):
        self.path = os.path.abspath(path)
        self.run_id = run_id
        self.hooks = list(hooks or [])
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def emit(self, name: str, **attributes: Any) -> dict:
        event = {
            "schema": "flexfactor.event.v1",
            "trace_id": self.run_id,
            "run_id": self.run_id,
            "time": _now(),
            "name": str(name),
            "attributes": _redact(attributes),
        }
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
        for hook in self.hooks:
            try:
                hook(dict(event))
            except Exception as ex:  # hooks observe; they never decide truth
                failure = dict(event)
                failure["name"] = "hook.failed"
                failure["attributes"] = {"hook": repr(hook), "error": str(ex)[:300]}
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(_redact(failure), sort_keys=True) + "\n")
        return event


def _git_files(root: str) -> tuple[list[str], str]:
    """Return tracked plus relevant untracked files, with provenance."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=30, check=False,
        )
        if tracked.returncode == 0:
            names = [p.decode("utf-8", "surrogateescape")
                     for p in tracked.stdout.split(b"\0") if p]
            return sorted(set(n.replace("\\", "/") for n in names)), "git-ls-files"
    except (OSError, subprocess.SubprocessError):
        pass
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/"))
    return out, "filesystem-walk"


def _safe_file(root: str, rel: str) -> Path | None:
    if not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
        return None
    p = Path(root, rel)
    try:
        root_real = Path(root).resolve(strict=True)
        resolved = p.resolve(strict=True)
        resolved.relative_to(root_real)
    except (OSError, ValueError):
        return None
    if p.is_symlink() or not resolved.is_file():
        return None
    return resolved


def _read_bytes(root: str, rel: str, cap: int = 4_000_000) -> tuple[bytes, bool] | None:
    got = _read_bytes_full(root, rel, cap)
    if got is None:
        return None
    raw, truncated, _path, _size = got
    return raw, truncated


def _read_bytes_full(root: str, rel: str, cap: int = 4_000_000
                     ) -> tuple[bytes, bool, Path, int] | None:
    """One containment resolve, one stat, one read - and hand all of it back.

    The index used to call `_safe_file` twice, `stat` twice and read every file
    TWICE (once here for content, once inside `_sha256_file` for the digest).
    On this machine each filesystem op costs 11-70ms under AV scanning, so on a
    ~4k-file repository that duplicated work is minutes of pure overhead before
    the audit prints anything. Callers that need the digest hash the bytes they
    already hold; only a TRUNCATED file still needs a streaming pass, because
    the bytes in memory are not the whole file.
    """
    path = _safe_file(root, rel)
    if path is None:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            raw = fh.read(cap + 1)
        return raw[:cap], size > cap, path, size
    except OSError:
        return None


def _is_test(rel: str) -> bool:
    low = "/" + rel.lower().replace("\\", "/")
    return any(m in low for m in TEST_MARKERS)


def _symbol(symbol_id: str, name: str, kind: str, rel: str, line: int,
            end_line: int | None = None, exported: bool | None = None) -> dict:
    return {
        "id": symbol_id, "name": name, "kind": kind, "file": rel,
        "line": int(line or 0), "end_line": int(end_line or line or 0),
        "exported": exported,
    }


def _parse_python(rel: str, text: str) -> dict:
    result = {"symbols": [], "imports": [], "calls": [], "routes": [],
              "controls": [], "data": [], "config": [], "parse_error": None}
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError) as ex:
        result["parse_error"] = f"{type(ex).__name__}: {ex}"
        return result
    parents: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            q = ".".join([*parents, node.name])
            result["symbols"].append(_symbol(f"{rel}::{q}", q, "class", rel,
                                                    node.lineno, node.end_lineno,
                                                    not node.name.startswith("_")))
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            q = ".".join([*parents, node.name])
            result["symbols"].append(_symbol(
                f"{rel}::{q}", q, "async-function" if isinstance(node, ast.AsyncFunctionDef)
                else "function", rel, node.lineno, node.end_lineno,
                not node.name.startswith("_")))
            for dec in node.decorator_list:
                try:
                    d = ast.unparse(dec)
                except Exception:
                    d = ""
                m = re.search(r"\.(get|post|put|patch|delete|route)\((['\"])(.*?)\2", d)
                if m:
                    result["routes"].append({"id": f"{rel}:{node.lineno}:{m.group(3)}",
                        "method": m.group(1).upper(), "path": m.group(3), "file": rel,
                        "line": node.lineno, "handler": q})
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

        def visit_Import(self, node: ast.Import) -> None:
            for item in node.names:
                result["imports"].append({"module": item.name, "line": node.lineno})

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            result["imports"].append({"module": "." * node.level + (node.module or ""),
                                      "line": node.lineno})

        def visit_Call(self, node: ast.Call) -> None:
            try:
                name = ast.unparse(node.func)
            except Exception:
                name = ""
            if name:
                result["calls"].append({"target": name, "line": node.lineno})
            self.generic_visit(node)

    Visitor().visit(tree)
    return result


_JS_FUNCTIONS = (
    re.compile(r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^\n]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"(?m)^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*([A-Za-z_$][\w$]*)\s*\([^;{}\n]*\)\s*\{"),
)
_JS_IMPORT = re.compile(r"(?m)(?:import[^\n]*?from\s*|require\s*\()\s*['\"]([^'\"]+)['\"]")
_JS_ROUTE = re.compile(r"(?i)(?:\b(?:app|router)\s*\.\s*(get|post|put|patch|delete|use)|<Route\b[^>]*\bpath\s*=)\s*(?:\(\s*)?['\"]([^'\"]+)['\"]")
_JS_CONTROL = re.compile(r"(?is)<(button|a|input|select|textarea|[^>]+\brole\s*=\s*['\"](?:button|tab|menuitem)['\"])[^>]*>")
_ENV_DEP = re.compile(r"(?:process\.env\.([A-Z][A-Z0-9_]+)|os\.environ(?:\.get)?\(\s*['\"]([^'\"]+))")


def _parse_javascript(rel: str, text: str) -> dict:
    result = {"symbols": [], "imports": [], "calls": [], "routes": [],
              "controls": [], "data": [], "config": [], "parse_error": None}
    seen = set()
    for rx in _JS_FUNCTIONS:
        for m in rx.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            key = (name, line)
            if key in seen or name in {"if", "for", "while", "switch", "catch"}:
                continue
            seen.add(key)
            result["symbols"].append(_symbol(f"{rel}::{name}@{line}", name,
                "function", rel, line, line, not name.startswith("_")))
    for m in _JS_IMPORT.finditer(text):
        result["imports"].append({"module": m.group(1),
                                  "line": text.count("\n", 0, m.start()) + 1})
    for m in _JS_ROUTE.finditer(text):
        method, path = (m.group(1) or "UI"), m.group(2)
        line = text.count("\n", 0, m.start()) + 1
        result["routes"].append({"id": f"{rel}:{line}:{path}", "method": method.upper(),
                                  "path": path, "file": rel, "line": line})
    for i, m in enumerate(_JS_CONTROL.finditer(text), 1):
        line = text.count("\n", 0, m.start()) + 1
        result["controls"].append({"id": f"{rel}:{line}:{i}", "kind": m.group(1)[:80],
                                    "file": rel, "line": line})
    for m in _ENV_DEP.finditer(text):
        result["config"].append({"name": m.group(1) or m.group(2),
                                  "line": text.count("\n", 0, m.start()) + 1})
    return result


def _parse_generic(rel: str, text: str) -> dict:
    result = {"symbols": [], "imports": [], "calls": [], "routes": [],
              "controls": [], "data": [], "config": [], "parse_error": None}
    for i, line in enumerate(text.splitlines(), 1):
        m = re.search(r"\b(?:func|fn|def|function)\s+([A-Za-z_][\w]*)", line)
        if m:
            name = m.group(1)
            result["symbols"].append(_symbol(f"{rel}::{name}@{i}", name,
                                                     "function", rel, i, i,
                                                     not name.startswith("_")))
        route = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\s+[\"']?(/[\w{}:./-]+)", line)
        if route:
            result["routes"].append({"id": f"{rel}:{i}:{route.group(2)}",
                                      "method": route.group(1), "path": route.group(2),
                                      "file": rel, "line": i})
    return result


_LARGE_FILE_HARD_CAP = 64 * 1024 * 1024  # beyond this, content is hashed but not scanned


def _index_large_file_in_chunks(root: str, rel: str, safe_path: str, *, cap: int) -> dict:
    """Chunk-ledger analysis for a source file above the structural-parser cap.

    Returns {"record": {...status/chunks...}, "symbols": [...], "routes": [...]}.
    Every chunk is hashed and scanned with the generic (regex) structural
    scanner at a line offset, so symbol lines are file-absolute. Completion:
    expected chunks == scanned chunks, or the record says BLOCKED with why."""
    try:
        size = os.path.getsize(safe_path)
    except OSError as ex:
        return {"record": {"status": "blocked", "chunk_error": f"stat failed: {ex}"},
                "symbols": [], "routes": []}
    if size > _LARGE_FILE_HARD_CAP:
        return {"record": {"status": "blocked",
                           "chunk_error": f"{size} bytes exceeds the {_LARGE_FILE_HARD_CAP}-byte "
                                          "hard cap; content hashed, not scanned"},
                "symbols": [], "routes": []}
    try:
        with open(safe_path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError as ex:
        return {"record": {"status": "blocked", "chunk_error": f"read failed: {ex}"},
                "symbols": [], "routes": []}
    chunks = _ff_ledger.chunk_text(text, file=rel, max_chars=cap // 4)
    symbols: list[dict] = []
    routes: list[dict] = []
    ledger = []
    for ch in chunks:
        parsed = _parse_generic(rel, ch.text)
        offset = ch.line_start - 1
        for sym in parsed["symbols"]:
            sym = dict(sym)
            sym["line"] = int(sym.get("line", 1)) + offset
            if "end_line" in sym and sym["end_line"] is not None:
                sym["end_line"] = int(sym["end_line"]) + offset
            sym["id"] = f"{rel}::{sym['name']}@{sym['line']}"
            sym["chunk_id"] = ch.id
            symbols.append(sym)
        for rt in parsed["routes"]:
            rt = dict(rt); rt["line"] = int(rt.get("line", 1)) + offset
            rt["id"] = f"{rel}:{rt['line']}:{rt['path']}"
            routes.append(rt)
        row = ch.to_dict(); row["status"] = "scanned"; row["symbols"] = len(parsed["symbols"])
        ledger.append(row)
    expected = len(chunks)
    scanned = sum(1 for r in ledger if r["status"] == "scanned")
    return {"record": {"status": "analyzed-in-chunks" if scanned == expected else "blocked",
                       "parser": "generic-chunked", "chunk_total": expected,
                       "chunk_scanned": scanned, "chunks": ledger,
                       "chunk_ledger_complete": scanned == expected},
            "symbols": symbols, "routes": routes}


def build_repository_index(root: str, run_id: str, progress=None) -> dict:
    """Build a content-addressed, measurable repository-wide index.

    `progress(done, total, rel)` is called per file when supplied. It exists
    because this runs BEFORE the audit's first phase transition: without it a
    large repository shows "starting" and prints nothing for minutes, which is
    indistinguishable from a hang (live 2026-08-19: GrantFlow, ~4k files, sat
    at phase "starting" while smaller programs in the same batch had reached
    the baseline gate).
    """
    root = os.path.abspath(root)
    paths, discovery = _git_files(root)
    total_paths = len(paths)
    files: list[dict] = []
    symbols: list[dict] = []
    imports: list[dict] = []
    routes: list[dict] = []
    controls: list[dict] = []
    config: list[dict] = []
    for rel in paths:
        ext = Path(rel).suffix.lower()
        category = ("source" if ext in SOURCE_EXTENSIONS else
                    "text" if ext in TEXT_CONFIG_EXTENSIONS or Path(rel).name in {
                        "Dockerfile", "Makefile", "LICENSE", "README"} else "asset")
        got = _read_bytes_full(root, rel)
        if got is None:
            files.append({"path": rel, "category": category, "status": "refused",
                          "sha256": None, "size": None, "analysis_run_id": run_id})
            continue
        raw, truncated, safe_path, size = got
        # Digest the bytes already in memory. Identical result to the old
        # streaming hash for every whole file; a truncated one still streams,
        # since `raw` is only the first `cap` bytes and hashing it would
        # silently publish a digest that is NOT the file's.
        record = {"path": rel, "category": category, "status": "inventoried",
                  "sha256": (_sha256_file(safe_path) if truncated
                             else hashlib.sha256(raw).hexdigest()),
                  "size": size,
                  "analysis_run_id": run_id, "test": _is_test(rel),
                  "content_truncated": truncated}
        if progress is not None:
            progress(len(files) + 1, total_paths, rel)
        if category == "source":
            if truncated:
                # NO SILENT TRUNCATION: a source file above the in-memory cap is
                # divided into complete, content-addressed line-range chunks,
                # each structurally scanned, with a ledger that accounts for
                # every chunk. The file never disappears into a label.
                chunk_info = _index_large_file_in_chunks(root, rel, safe_path, cap=4_000_000)
                record.update(chunk_info["record"])
                symbols.extend(chunk_info["symbols"])
                routes.extend(chunk_info["routes"])
            else:
                text = raw.decode("utf-8", "replace")
                if ext in {".py", ".pyi"}:
                    parsed = _parse_python(rel, text)
                elif ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}:
                    parsed = _parse_javascript(rel, text)
                else:
                    parsed = _parse_generic(rel, text)
                record["status"] = "analyzed" if not parsed["parse_error"] else "parse-error"
                record["parse_error"] = parsed["parse_error"]
                symbols.extend(parsed["symbols"])
                routes.extend(parsed["routes"])
                controls.extend(parsed["controls"])
                config.extend({"file": rel, **v} for v in parsed["config"])
                imports.extend({"file": rel, **v} for v in parsed["imports"])
        files.append(record)
    file_map = {f["path"]: f for f in files}
    index = {
        "schema": INDEX_SCHEMA, "run_id": run_id, "generated_at": _now(),
        "root_fingerprint": hashlib.sha256(root.encode("utf-8")).hexdigest(),
        "discovery": discovery, "files": files, "symbols": symbols,
        "imports": imports, "routes": routes, "controls": controls,
        "config_dependencies": config,
        "totals": {
            "files": len(files),
            "tracked_or_relevant_source_files": sum(f["category"] == "source" for f in files),
            "analyzed_source_files": sum(f["status"] in ("analyzed", "analyzed-in-chunks")
                                         for f in files),
            "chunk_analyzed_source_files": sum(f["status"] == "analyzed-in-chunks" for f in files),
            "refused_files": sum(f["status"] == "refused" for f in files),
            "symbols": len(symbols), "functions": sum(s["kind"].endswith("function") for s in symbols),
            "routes": len(routes), "controls": len(controls),
            "test_files": sum(bool(f.get("test")) for f in files),
        },
    }
    index["complete_source_inventory"] = all(
        f["path"] in file_map
        # "parse-error" still counts: the file WAS opened and scanned and the
        # parser's failure is recorded on its record.  "blocked" does not - its
        # bytes were never scanned at all.
        and (f["status"] in ANALYZED_SOURCE_STATUSES
             or f["status"] == "parse-error")
        for f in files if f["category"] == "source")
    return index


def diff_indexes(before: dict, after: dict) -> list[str]:
    left = {f["path"]: f.get("sha256") for f in before.get("files", [])}
    right = {f["path"]: f.get("sha256") for f in after.get("files", [])}
    return sorted(p for p in set(left) | set(right) if left.get(p) != right.get(p))


def changed_file_rescan(after: dict, changed: Iterable[str]) -> dict:
    records = {f["path"].replace("\\", "/"): f for f in after.get("files", [])}
    rows = []
    for raw in sorted(set(str(p).replace("\\", "/") for p in changed if p)):
        row = records.get(raw)
        # A non-source file's complete record IS "inventoried" (it is hashed,
        # never parsed); a source file's is one of ANALYZED_SOURCE_STATUSES.
        rescanned = bool(row and (row.get("status") in ANALYZED_SOURCE_STATUSES
                                  or row.get("status") == "inventoried"))
        rows.append({"path": raw, "status": row.get("status") if row else "missing",
                     "sha256": row.get("sha256") if row else None,
                     "analysis_run_id": row.get("analysis_run_id") if row else None,
                     "rescanned": rescanned})
    return {"changed": len(rows), "rescanned": sum(r["rescanned"] for r in rows),
            "complete": all(r["rescanned"] for r in rows), "files": rows}


def dependency_blast_radius(index: dict, changed: Iterable[str]) -> dict:
    """Conservative reverse-import closure. Unknown mappings are surfaced."""
    changed_set = {str(p).replace("\\", "/") for p in changed}
    source_files = [f["path"] for f in index.get("files", []) if f.get("category") == "source"]
    stems: dict[str, set[str]] = {}
    for rel in source_files:
        noext = str(Path(rel).with_suffix("")).replace("\\", "/")
        for key in {noext, noext.replace("/", "."), Path(noext).name}:
            stems.setdefault(key, set()).add(rel)
    reverse: dict[str, set[str]] = {f: set() for f in source_files}
    unresolved = []
    for imp in index.get("imports", []):
        owner, module = imp.get("file"), str(imp.get("module") or "")
        keys = {module, module.lstrip("."), module.replace(".", "/"),
                Path(module.replace(".", "/")).name}
        targets = set().union(*(stems.get(k, set()) for k in keys))
        if not targets and module.startswith((".", "@/", "~/")):
            unresolved.append({"file": owner, "module": module, "line": imp.get("line")})
        for target in targets:
            reverse.setdefault(target, set()).add(owner)
    affected = set(changed_set)
    frontier = list(changed_set)
    edges = []
    while frontier:
        target = frontier.pop()
        for dependent in reverse.get(target, set()):
            edges.append({"from": dependent, "to": target})
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    tests = sorted(f for f in affected if _is_test(f))
    return {"changed": sorted(changed_set), "affected": sorted(affected),
            "affected_count": len(affected), "test_impact": tests,
            "edges": edges, "unresolved_local_imports": unresolved,
            "ran": True}


def purpose_graph(contract: dict | None, purpose_gap: dict | None,
                  index: dict, run_id: str) -> dict:
    contract = contract or {}
    purpose_gap = purpose_gap or {}
    purpose_text = contract.get("purpose") or purpose_gap.get("purpose") or ""
    nodes: list[dict] = [{"id": "purpose:root", "type": "purpose", "label": purpose_text,
                         "verified": bool(contract),
                         "evidence": contract.get("source") or purpose_gap.get("evidence") or []}]
    edges: list[dict] = []
    criteria = contract.get("acceptance_criteria") or []
    for i, criterion in enumerate(criteria, 1):
        cid = f"outcome:{i}"
        nodes.append({"id": cid, "type": "outcome", "label": str(criterion),
                      "verified": True, "evidence": contract.get("source")})
        edges.append({"from": "purpose:root", "to": cid, "relation": "requires"})
    for route in index.get("routes", []):
        rid = "workflow:" + route["id"]
        nodes.append({"id": rid, "type": "workflow", "label": f"{route.get('method')} {route.get('path')}",
                      "verified": True, "evidence": {"file": route.get("file"), "line": route.get("line")}})
        edges.append({"from": "purpose:root", "to": rid, "relation": "implemented-by"})
    for symbol in index.get("symbols", []):
        sid = "function:" + symbol["id"]
        nodes.append({"id": sid, "type": "function", "label": symbol["name"],
                      "verified": True, "evidence": {"file": symbol["file"], "line": symbol["line"]}})
    return {"schema": PURPOSE_GRAPH_SCHEMA, "run_id": run_id,
            "generated_at": _now(), "nodes": nodes, "edges": edges,
            "contradictions": list(purpose_gap.get("contradictions") or []),
            "confidence": "authored" if contract else "inferred-unverified"}


def coverage_ledger(index: dict, *, run_id: str, test_command: list[str] | None,
                    tests_ran: bool, tests_passed: bool | None,
                    generated_test_modules: Iterable[str], e2e: dict | None) -> dict:
    """Produce module-execution proof from successful native runs and imports.

    A test file's mere existence is never evidence.  When the native suite is
    green, however, a statically imported module (and its transitive static
    imports) necessarily loaded during that execution.  Likewise, a successful
    browser run loads the production entry graph.  This is stronger and far less
    destructive than generating a second speculative test suite for every module.
    It remains module-level evidence, never falsely labelled direct function
    coverage.
    """
    generated = {str(p).replace("\\", "/") for p in generated_test_modules}
    e2e = e2e or {}
    source_files = {f["path"] for f in index.get("files", [])
                    if f.get("category") == "source"}
    test_files = {p for p in source_files if _is_test(p)}

    # Resolve the conservative subset of local imports that can be tied to an
    # indexed file without executing repository code.
    by_noext: dict[str, set[str]] = {}
    by_name: dict[str, set[str]] = {}
    for rel in source_files:
        noext = str(Path(rel).with_suffix("")).replace("\\", "/")
        by_noext.setdefault(noext, set()).add(rel)
        by_name.setdefault(Path(noext).name, set()).add(rel)

    def targets(owner: str, module: str) -> set[str]:
        module = str(module or "").replace("\\", "/")
        keys: set[str] = set()
        if module.startswith("@/"):
            keys.add("src/" + module[2:])
        elif module.startswith("~/"):
            keys.add("src/" + module[2:])
        elif module.startswith("."):
            keys.add(os.path.normpath(os.path.join(
                os.path.dirname(owner), module)).replace("\\", "/"))
        else:
            keys.update({module, module.replace(".", "/")})
        resolved: set[str] = set()
        for key in list(keys):
            key = key.lstrip("./")
            key_noext = (str(Path(key).with_suffix(""))
                         if Path(key).suffix.lower() in SOURCE_EXTENSIONS else key)
            resolved |= by_noext.get(key_noext, set())
            resolved |= by_noext.get(key_noext + "/index", set())
            # Bare local module names are accepted only when unambiguous.
            named = by_name.get(Path(key_noext).name, set())
            if len(named) == 1:
                resolved |= named
        return resolved

    forward: dict[str, set[str]] = {rel: set() for rel in source_files}
    for item in index.get("imports", []):
        owner = str(item.get("file") or "").replace("\\", "/")
        if owner in forward:
            forward[owner] |= targets(owner, str(item.get("module") or ""))

    roots: set[str] = set()
    root_kind: dict[str, str] = {}
    evidence_kind: dict[str, str] = {}
    if tests_ran and tests_passed is True:
        roots |= test_files
        roots |= {p for p in generated if p in source_files}
        for root in roots:
            root_kind[root] = "native-test-import-path"
    if e2e.get("ok") is True:
        entry_rx = re.compile(
            r"(?i)(?:^|/)(?:main|index|app|server|start|routes?)\.(?:[cm]?[jt]sx?|py)$")
        browser_roots = {p for p in source_files
                         if not _is_test(p) and entry_rx.search(p)}
        roots |= browser_roots
        for root in browser_roots:
            root_kind.setdefault(root, "browser-entry-import-path")
    executed_modules: set[str] = set()
    frontier = [(root, root_kind[root]) for root in roots]
    while frontier:
        owner, kind = frontier.pop()
        if owner in executed_modules:
            continue
        executed_modules.add(owner)
        evidence_kind[owner] = kind
        for target in forward.get(owner, set()):
            if target not in executed_modules:
                frontier.append((target, kind))

    function_rows = []
    for sym in index.get("symbols", []):
        if not str(sym.get("kind", "")).endswith("function"):
            continue
        source = sym["file"]
        if _is_test(source):
            continue  # test helpers are evidence producers, not product functions
        executed = source in executed_modules
        function_rows.append({"id": sym["id"], "file": source, "line": sym["line"],
            "name": sym["name"], "status": "module-executed" if executed else "unproven",
            "invocation_evidence": ({"command": test_command,
                                      "type": evidence_kind.get(source, "static-import-path"),
                                      "roots": sorted(roots)} if executed else None),
            "direct_function_coverage": False})
    route_evidence = list(e2e.get("route_evidence") or [])
    control_evidence = list(e2e.get("control_evidence") or [])
    discovered_routes = index.get("routes", [])
    discovered_controls = index.get("controls", [])
    return {
        "schema": COVERAGE_SCHEMA, "run_id": run_id, "generated_at": _now(),
        "files": [{"path": f["path"], "analysis_status": f["status"],
                   "sha256": f.get("sha256"), "analysis_run_id": f.get("analysis_run_id")}
                  for f in index.get("files", []) if f.get("category") == "source"],
        "functions": function_rows,
        "routes": route_evidence,
        "controls": control_evidence,
        "discovered_route_total": len(discovered_routes),
        "executed_route_total": sum(r.get("status") == "passed" for r in route_evidence),
        "discovered_control_total": len(discovered_controls),
        "executed_control_total": sum(r.get("status") == "passed" for r in control_evidence),
        "function_total": len(function_rows),
        "function_module_execution_total": sum(r["status"] == "module-executed" for r in function_rows),
        "function_direct_coverage_total": sum(bool(r["direct_function_coverage"]) for r in function_rows),
        "tests": {"command": test_command, "ran": bool(tests_ran), "passed": tests_passed,
                  "collected": bool(tests_ran and test_files)},
        "executed_modules": sorted(executed_modules),
        "unproven_modules": sorted({r["file"] for r in function_rows
                                     if r["status"] == "unproven"}),
    }


def secret_findings(root: str, index: dict) -> list[dict]:
    """Return credential-shaped material with explicit baseline disposition.

    A repository may intentionally contain fabricated credential shapes in a
    scanner test corpus.  Those samples are never silently ignored: an exact
    SHA-256 fingerprint must be declared in ``.flexfactor-secret-baseline.json``
    with a reason.  Changed material therefore becomes unresolved again.
    """
    baseline_path = os.path.join(root, ".flexfactor-secret-baseline.json")
    accepted: dict[tuple[str, str, str], str] = {}
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            baseline = json.load(fh)
        for item in baseline.get("accepted_test_fixtures", []):
            key = (str(item.get("file", "")).replace("\\", "/"),
                   str(item.get("rule_id", "")), str(item.get("fingerprint", "")))
            if all(key) and str(item.get("reason", "")).strip():
                accepted[key] = str(item["reason"]).strip()
    except (OSError, ValueError, TypeError):
        pass
    findings = []
    cap = 1_000_000
    for file in index.get("files", []):
        if file.get("category") not in {"source", "text"}:
            continue
        got = _read_bytes(root, file["path"], cap=cap)
        if got is None:
            continue
        raw, truncated = got
        text = raw.decode("utf-8", "replace")
        if truncated:
            # A CLEAN SECRETS GATE MUST NOT BE A CLAIM ABOUT BYTES NEVER READ.
            # The truncation flag used to be discarded, so everything past the
            # cap was silently unscanned. The fingerprint is the digest of the
            # prefix that WAS scanned, so the record re-arms when the file
            # changes, exactly like every other finding here.
            findings.append({
                "rule_id": "secret.scan-truncated", "severity": "critical",
                "message": (f"only the first {cap} bytes were scanned for "
                            "credentials; the rest of this file is UNSCANNED"),
                "file": file["path"], "line": 1,
                "fingerprint": hashlib.sha256(raw).hexdigest(),
                "disposition": "unresolved", "baseline_reason": None})
        for kind, rx in _SECRET_PATTERNS:
            for m in rx.finditer(text):
                rule_id = f"secret.{kind}"
                fingerprint = hashlib.sha256(m.group(0).encode("utf-8")).hexdigest()
                key = (file["path"], rule_id, fingerprint)
                reason = accepted.get(key)
                path_lower = file["path"].lower()
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                if line_end < 0:
                    line_end = len(text)
                nearby = text[max(0, line_start - 240):min(len(text), line_end + 240)].lower()
                fixture_path = any(part in path_lower for part in (
                    "/test", "tests/", "__tests__/", "/fixture", "fixtures/",
                    "docs/", "security.md"))
                fixture_words = any(word in nearby for word in (
                    "fake", "example", "fixture", "placeholder", "redacted",
                    "dummy", "sample", "should detect", "false positive"))
                contextual = bool(not reason and fixture_path and fixture_words)
                findings.append({"rule_id": f"secret.{kind}", "severity": "critical",
                    "message": f"Credential-shaped {kind} material is committed",
                    "file": file["path"], "line": text.count("\n", 0, m.start()) + 1,
                    "fingerprint": fingerprint,
                    "disposition": ("accepted-test-fixture" if reason else
                                    "accepted-contextual-example" if contextual else
                                    "unresolved"),
                    "baseline_reason": (reason or (
                        "deterministic docs/test context contains an explicit fake/example marker"
                        if contextual else None))})
    return findings


def quality_gates(*, run_id: str, baseline_ran: bool, baseline_passed: bool | None,
                  suite_command: list[str] | None, suite_ran: bool,
                  suite_passed: bool | None, tests_collected: bool,
                  e2e: dict | None, rescan: dict, blast: dict,
                  secrets: list[dict], index: dict, coverage: dict,
                  suite_evidence: dict | None = None) -> dict:
    def gate(gid: str, name: str, ran: bool, passed: bool | None, evidence: Any,
             category: str = "quality") -> dict:
        status = "pass" if ran and passed is True else "fail" if ran and passed is False else "blocked"
        return {"id": gid, "name": name, "category": category, "ran": ran,
                "passed": passed if ran else None, "status": status, "evidence": evidence}
    e2e = e2e or {}
    # DIRECT execution evidence only. Module-level execution (an import
    # succeeded) is recorded for context but NEVER satisfies this gate. The
    # gate is complete only when every first-party function is directly
    # covered or explicitly BLOCKED with a reason (flexfactor_coverage gate).
    direct_gate = coverage.get("direct_gate") or {}
    functions_proven = bool(direct_gate.get("complete")) if direct_gate else (
        coverage.get("function_total", 0) == 0)
    behavior_applicable = bool(index.get("routes") or index.get("controls") or e2e.get("ran"))
    behavior_complete = bool(
        e2e.get("ok")
        and coverage.get("executed_route_total", 0) >= coverage.get("discovered_route_total", 0)
        and coverage.get("executed_control_total", 0) >= coverage.get("discovered_control_total", 0))
    unresolved_secrets = [f for f in secrets
                          if not str(f.get("disposition", "")).startswith("accepted-")]
    gates = [
        gate("build", "Compilation/build", baseline_ran, baseline_passed,
             {"result": baseline_passed}),
        gate("tests", "Unit/integration tests", suite_ran,
             bool(suite_passed is True and tests_collected) if suite_ran else None,
             {"command": suite_command, "result": suite_passed,
              "tests_collected": tests_collected, **(suite_evidence or {})}),
        gate("secrets", "Committed secret detection", True, not unresolved_secrets,
             {"unresolved": unresolved_secrets,
              "accepted_test_fixtures": [f for f in secrets
                                           if str(f.get("disposition", "")).startswith("accepted-")]},
             "security"),
        gate("inventory", "Relevant source inventory", True,
             bool(index.get("complete_source_inventory")), index.get("totals")),
        gate("rescan", "Changed-file rescan", True, bool(rescan.get("complete")), rescan),
        # NOT a tautology. `dependency_blast_radius` hardcodes "ran": True on
        # every return path, so ran-implies-passed was a gate that could not
        # fail while still counting toward totals["pass"] and the overall all().
        # It now asserts the thing the evidence claims to prove: an unresolved
        # LOCAL import means the reverse closure is incomplete, so the blast
        # radius below is a floor, not the answer.
        gate("blast-radius", "Dependency blast-radius analysis", bool(blast.get("ran")),
             bool(blast.get("ran") and not blast.get("unresolved_local_imports")),
             blast),
        gate("function-coverage", "Direct function invocation evidence", True,
             functions_proven, {"functions": coverage.get("function_total", 0),
                                 "direct": coverage.get("function_direct_coverage_total", 0),
                                 "module_executed_only": coverage.get("function_module_execution_total", 0),
                                 "blocked": direct_gate.get("blocked", 0),
                                 # A block is only evidence when its REASON is
                                 # recorded next to it, and a REJECTED block has
                                 # to appear too - a declaration that vanishes
                                 # is indistinguishable from one never made.
                                 "blocked_ids": list(direct_gate.get("blocked_ids") or [])[:200],
                                 "blocked_reasons": dict(direct_gate.get("blocked_reasons") or {}),
                                 "blocked_without_reason": list(
                                     direct_gate.get("blocked_without_reason") or [])[:200],
                                 "unknown_blocked_ids": list(
                                     direct_gate.get("unknown_blocked_ids") or [])[:200],
                                 "blocked_superseded_by_direct": list(
                                     direct_gate.get("blocked_superseded_by_direct") or [])[:200],
                                 "blocked_declared": direct_gate.get("blocked_declared", 0),
                                 "blocked_rejected": list(
                                     direct_gate.get("blocked_rejected") or [])[:200],
                                 "basis": coverage.get("function_coverage_basis",
                                                       "module-execution-only (NOT direct)"),
                                 "unproven_ids": list(direct_gate.get("unproven_ids") or [])[:200]}),
        gate("behavior", "Route/control behavioral execution",
             bool(e2e.get("ran")) or not behavior_applicable,
             behavior_complete if behavior_applicable else True,
             {"pages": e2e.get("pages", 0), "controls": e2e.get("controls", 0),
              "applicable": behavior_applicable,
              "accessibility": e2e.get("accessibility"), "performance": e2e.get("performance")}),
    ]
    return {"schema": GATES_SCHEMA, "run_id": run_id, "generated_at": _now(),
            "gates": gates, "totals": {"pass": sum(g["status"] == "pass" for g in gates),
                                        "fail": sum(g["status"] == "fail" for g in gates),
                                        "blocked": sum(g["status"] == "blocked" for g in gates)},
            "passed": all(g["status"] == "pass" for g in gates)}


def sarif(findings: Iterable[dict], *, tool_version: str, run_id: str) -> dict:
    findings = list(findings)
    rules = {}
    results = []
    levels = {"critical": "error", "high": "error", "medium": "warning",
              "low": "note", "info": "note"}
    for item in findings:
        rid = str(item.get("rule_id") or item.get("category") or "flexfactor.finding")
        rules.setdefault(rid, {"id": rid, "name": rid, "shortDescription": {
            "text": str(item.get("title") or item.get("message") or rid)[:200]}})
        result = {"ruleId": rid, "level": levels.get(str(item.get("severity", "")).lower(), "warning"),
                  "message": {"text": str(item.get("problem") or item.get("message") or item.get("title") or rid)}}
        if item.get("file") and not str(item.get("file")).startswith("("):
            result["locations"] = [{"physicalLocation": {
                "artifactLocation": {"uri": str(item["file"]).replace("\\", "/")},
                "region": {"startLine": max(1, int(item.get("line") or 1))}}}]
        results.append(result)
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "FlexFactor", "version": tool_version,
                                             "rules": list(rules.values())}},
                      "automationDetails": {"id": run_id}, "results": results}]}


def write_evidence_bundle(state_root: str, project_root: str, run_id: str, *,
                          index: dict, graph: dict, coverage: dict, gates: dict,
                          blast: dict, rescan: dict, sarif_payload: dict,
                          final_commit: str | None = None) -> dict:
    project_id = hashlib.sha256(os.path.abspath(project_root).encode("utf-8")).hexdigest()[:16]
    run_dir = os.path.join(state_root, "evidence", project_id, run_id)
    paths = {
        "code_index": atomic_json(os.path.join(run_dir, "code-index.json"), index),
        "purpose_graph": atomic_json(os.path.join(run_dir, "purpose-graph.json"), graph),
        "coverage_ledger": atomic_json(os.path.join(run_dir, "coverage-ledger.json"), coverage),
        "quality_gates": atomic_json(os.path.join(run_dir, "quality-gates.json"), gates),
        "blast_radius": atomic_json(os.path.join(run_dir, "blast-radius.json"), blast),
        "changed_file_rescan": atomic_json(os.path.join(run_dir, "changed-file-rescan.json"), rescan),
        "sarif": atomic_json(os.path.join(run_dir, "results.sarif"), sarif_payload),
    }
    manifest = {"schema": SCHEMA, "run_id": run_id, "generated_at": _now(),
                "project_fingerprint": project_id, "final_commit": final_commit,
                "artifacts": paths,
                "claims": {"quality_gate_passed": gates.get("passed"),
                           "changed_files_rescanned": rescan.get("complete"),
                           "blast_radius_ran": blast.get("ran")}}
    paths["manifest"] = atomic_json(os.path.join(run_dir, "manifest.json"), manifest)
    return paths


@dataclasses.dataclass(frozen=True)
class RuntimeMode:
    mode: str
    provider: str
    model: str | None
    local_only: bool
    reason: str


def resolve_runtime_mode(mode: str, provider: str, model: str | None,
                         credentials_present: bool, local_available: bool,
                         cloud_free_available: bool = False) -> RuntimeMode:
    """Provider-neutral, fail-explicit free/paid mode policy.

    TWO modes, matching the CLI exactly (owner order 2026-08-24): a second mode
    vocabulary living in the evidence module is how the launcher-drift trap
    starts - one surface says free/paid while another still says auto/local/paid,
    and the run records a mode name the operator was never offered.

    Free capacity is TWO things and the caller must say so separately: the cloud
    free tiers and the loopback ones. Collapsing them into one flag is the exact
    mistake the retired 'local' mode made - it shut out 126 credentialed cloud
    free-tier routes and pinned runs to CPU-only Ollama. ``cloud_free_available``
    defaults to False so an existing caller resolves exactly as it did before.

    ``local_only`` is therefore a property of the RESOLVED run, not of the mode:
    a free run is local-only only when loopback is the only free capacity there
    is. That distinction is load-bearing because ``local_only`` is what the
    egress record claims, and claiming zero egress for a run that reached a
    cloud free tier would be a false record, not a conservative one.
    """
    raw = str(mode or "free").strip().lower()
    # Retired spellings are ACCEPTED, never offered - a saved command or
    # scheduled task must degrade to the safe mode, not die. Both meant free.
    mode = {"auto": "free", "local": "free"}.get(raw, raw)
    if mode not in {"free", "paid"}:
        raise ValueError("mode must be free or paid")
    free_available = bool(local_available or cloud_free_available)
    if mode == "paid":
        # Unchanged, and deliberately so: paid must never resolve to something
        # cheaper behind the operator's back any more than free may resolve to
        # something billable. Both directions are silent-substitution bugs.
        if not credentials_present:
            hint = " Free routes are available." if free_available else ""
            raise RuntimeError("paid mode requested but credentials are absent." + hint)
        return RuntimeMode("paid", provider, model, False, "explicit paid mode")
    if not free_available:
        raise RuntimeError("free mode requested but no free route is reachable")
    local_only = bool(local_available and not cloud_free_available)
    return RuntimeMode("free", provider, model, local_only,
                       "explicit free mode (loopback only)" if local_only
                       else "explicit free mode (cloud free tiers reachable)")
