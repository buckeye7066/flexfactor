"""Direct per-function execution evidence from real coverage-tool output.

Why this module exists: `flexfactor_evidence.function_coverage()` can only
prove that a MODULE was imported on a passing test path, so it hard-codes
`direct_function_coverage = False` for every function and labels the rest
"module-executed". The governing contract requires DIRECT invocation
evidence per first-party function; importing a module is not it. This module
turns artifacts that coverage tools actually write (Python `coverage` JSON,
Istanbul/c8 JSON, lcov, Go coverprofile, JaCoCo XML, Cobertura XML) into
per-symbol rows and a fail-closed gate.

Containment, same as `flexfactor_prodready.py`: stdlib only, never imports
flexfactor, never executes anything. `detect_coverage_artifacts` only reads
files; `coverage_commands` only *proposes* argv lists and grounds every
`available` flag in a file / spec check.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

COVERAGE_ROWS_SCHEMA = "flexfactor.direct_function_coverage.v1"

# Same heuristic as flexfactor_evidence._is_test (kept verbatim so the two
# modules never disagree about which symbols are product functions).
TEST_MARKERS = ("/test/", "/tests/", "__tests__", ".test.", ".spec.",
                "_test.", "_tests.")
SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", ".tox",
             ".mypy_cache", ".pytest_cache", "__pycache__", ".idea", ".vscode",
             "dist", "bower_components"}
MAX_SCAN_DEPTH = 6
BODY_WINDOW_WITHOUT_END = 50
FUNCTION_LINE_TOLERANCE = 2

FORMAT_PY_JSON = "python-coverage-json"
FORMAT_PY_SQLITE = "python-coverage-sqlite"
FORMAT_ISTANBUL = "istanbul-json"
FORMAT_LCOV = "lcov"
FORMAT_GO = "go-coverprofile"
FORMAT_JACOCO = "jacoco-xml"
FORMAT_COBERTURA = "cobertura-xml"

# Formats whose artifacts carry explicit per-function hit records.
FUNCTION_RECORD_FORMATS = {FORMAT_PY_JSON, FORMAT_ISTANBUL, FORMAT_LCOV,
                           FORMAT_JACOCO, FORMAT_COBERTURA}


def _is_test(rel: str) -> bool:
    low = "/" + str(rel).lower().replace("\\", "/")
    return any(m in low for m in TEST_MARKERS)


def _fwd(path: str) -> str:
    return str(path or "").replace("\\", "/")


def _canon_rel(path: str, project_dir: str) -> str:
    """Normalise a tool-reported path to a forward-slash path relative to
    project_dir. Absolute paths inside the project are relativised; absolute
    paths outside it are kept absolute (they cannot match an indexed file, and
    inventing a relative spelling would make them LOOK matchable)."""
    p = _fwd(path).strip()
    if not p:
        return p
    root = os.path.abspath(project_dir)
    # NOTE: Python 3.13 ntpath.isabs("/abs/x") is False (no drive), so a
    # POSIX-absolute path from a tool is tested explicitly.
    if os.path.isabs(p) or p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        try:
            rel = os.path.relpath(os.path.abspath(p), root)
        except ValueError:  # different drive on Windows
            return p
        rel = _fwd(rel)
        if rel == "." or rel.startswith("../"):
            return p
        return rel
    while p.startswith("./"):
        p = p[2:]
    return p


def _empty_file() -> dict:
    return {"executed_lines": set(), "functions": {}}


def _add_function(entry: dict, name: str, line: int, hits: int) -> None:
    """Record a function hit; a second record with the same name in the same
    file (overloads, methods on two classes) is keyed `name@line` so it is
    never silently overwritten."""
    key = name if name not in entry["functions"] else f"{name}@{line}"
    entry["functions"][key] = {"line": int(line or 0), "hits": int(hits or 0)}


# --------------------------------------------------------------------------
# Detection (read-only, never executes anything)
# --------------------------------------------------------------------------

def _sniff_json(path: str) -> tuple[str | None, str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as ex:
        return None, f"unreadable json: {ex}"
    if not isinstance(data, dict):
        return None, "top-level is not an object"
    files = data.get("files")
    if isinstance(files, dict) and files and all(
            isinstance(v, dict) and "executed_lines" in v for v in files.values()):
        return FORMAT_PY_JSON, f"python coverage json, {len(files)} file(s)"
    if data and all(isinstance(v, dict) and ("fnMap" in v or "statementMap" in v)
                    for v in data.values()):
        return FORMAT_ISTANBUL, f"istanbul/c8 json, {len(data)} file(s)"
    if isinstance(files, dict) and not files:
        return FORMAT_PY_JSON, "python coverage json with ZERO files"
    return None, "json does not match a known coverage shape"


def _sniff_xml_root(path: str) -> str | None:
    try:
        for _event, el in ET.iterparse(path, events=("start",)):
            return el.tag
    except (OSError, ET.ParseError):
        return None
    return None


def _sniff_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def detect_coverage_artifacts(project_dir: str) -> list[dict]:
    """Find coverage outputs under project_dir WITHOUT executing anything.

    Each entry: {path (absolute), rel, format, parse (bool), detail}.
    `parse=False` means the file was recognised but this module cannot read
    it directly (e.g. the `.coverage` sqlite store — produce coverage.json
    with `coverage json`)."""
    root = os.path.abspath(project_dir)
    found: list[dict] = []
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = _fwd(os.path.relpath(dirpath, root))
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and depth < MAX_SCAN_DEPTH)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = _fwd(os.path.relpath(full, root))
            low = name.lower()
            entry = None
            if low == ".coverage" or (low.startswith(".coverage.") and
                                      not low.endswith(".json")):
                entry = {"format": FORMAT_PY_SQLITE, "parse": False,
                         "detail": "coverage.py sqlite data store; not parsed here "
                                   "(run `python -m coverage json -o coverage.json`)"}
            elif low == "coverage-final.json":
                fmt, detail = _sniff_json(full)
                entry = {"format": fmt or FORMAT_ISTANBUL, "parse": fmt is not None,
                         "detail": detail}
            elif low == "coverage.json" or ("coverage" in low and low.endswith(".json")):
                fmt, detail = _sniff_json(full)
                if fmt is None:
                    continue  # some unrelated json that happens to say "coverage"
                entry = {"format": fmt, "parse": True, "detail": detail}
            elif low == "lcov.info" or low.endswith(".lcov"):
                head = _sniff_first_line(full)
                ok = head.startswith(("TN:", "SF:")) or head == ""
                entry = {"format": FORMAT_LCOV, "parse": ok,
                         "detail": "lcov tracefile" if ok else
                         f"does not start with TN:/SF: ({head[:40]!r})"}
            elif low in {"coverage.out", "cover.out", "coverage.txt"} or low.endswith(".coverprofile"):
                head = _sniff_first_line(full)
                ok = head.startswith("mode:")
                if not ok and low == "coverage.txt":
                    continue
                entry = {"format": FORMAT_GO, "parse": ok,
                         "detail": f"go coverprofile ({head})" if ok else
                         f"missing `mode:` header ({head[:40]!r})"}
            elif low in {"jacoco.xml", "jacocotestreport.xml"} or "jacoco" in low and low.endswith(".xml"):
                tag = _sniff_xml_root(full)
                entry = {"format": FORMAT_JACOCO, "parse": tag == "report",
                         "detail": f"root element <{tag}>"}
            elif low in {"coverage.xml", "coverage.cobertura.xml"} or low.endswith(".cobertura.xml"):
                tag = _sniff_xml_root(full)
                entry = {"format": FORMAT_COBERTURA, "parse": tag == "coverage",
                         "detail": f"root element <{tag}>"}
            if entry is not None:
                entry.update({"path": full, "rel": rel})
                found.append(entry)
    return found


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

def _parse_python_json(path: str, project_dir: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    files: dict[str, dict] = {}
    for raw_path, rec in (data.get("files") or {}).items():
        rel = _canon_rel(raw_path, project_dir)
        entry = files.setdefault(rel, _empty_file())
        entry["executed_lines"].update(int(n) for n in rec.get("executed_lines") or [])
        for fname, frec in (rec.get("functions") or {}).items():
            if not fname:
                continue  # coverage.py reports the module body under ""
            executed = [int(n) for n in frec.get("executed_lines") or []]
            missing = [int(n) for n in frec.get("missing_lines") or []]
            all_lines = executed + missing
            if not all_lines:
                continue
            # coverage.py's function region starts at the first BODY line;
            # the def line is typically 1-2 above, inside the match tolerance.
            _add_function(entry, fname, min(all_lines), 1 if executed else 0)
    return {"format": FORMAT_PY_JSON, "artifact": path, "files": files}


def _parse_istanbul(path: str, project_dir: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    files: dict[str, dict] = {}
    for raw_path, rec in data.items():
        if not isinstance(rec, dict):
            continue
        rel = _canon_rel(rec.get("path") or raw_path, project_dir)
        entry = files.setdefault(rel, _empty_file())
        smap, shits = rec.get("statementMap") or {}, rec.get("s") or {}
        for sid, loc in smap.items():
            if int(shits.get(sid, 0) or 0) > 0:
                start = ((loc or {}).get("start") or {}).get("line")
                end = ((loc or {}).get("end") or {}).get("line") or start
                if start:
                    entry["executed_lines"].update(range(int(start), int(end) + 1))
        fmap, fhits = rec.get("fnMap") or {}, rec.get("f") or {}
        for fid, fn in fmap.items():
            decl = (fn.get("decl") or fn.get("loc") or {}).get("start") or {}
            line = decl.get("line") or ((fn.get("loc") or {}).get("start") or {}).get("line")
            if not line:
                continue
            _add_function(entry, fn.get("name") or f"(anonymous_{fid})",
                          int(line), int(fhits.get(fid, 0) or 0))
    return {"format": FORMAT_ISTANBUL, "artifact": path, "files": files}


def _parse_lcov(path: str, project_dir: str) -> dict:
    files: dict[str, dict] = {}
    current: dict | None = None
    fn_lines: dict[str, int] = {}
    fn_index_lines: dict[str, tuple[int, str]] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("SF:"):
                rel = _canon_rel(line[3:], project_dir)
                current = files.setdefault(rel, _empty_file())
                fn_lines, fn_index_lines = {}, {}
            elif current is None:
                continue
            elif line.startswith("FN:"):
                parts = line[3:].split(",", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    fn_lines[parts[1]] = int(parts[0])
            elif line.startswith("FNDA:"):
                parts = line[5:].split(",", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    _add_function(current, parts[1], fn_lines.get(parts[1], 0), int(parts[0]))
            elif line.startswith("FNL:"):  # lcov 2.x indexed form
                parts = line[4:].split(",")
                if len(parts) >= 3 and parts[1].isdigit():
                    fn_index_lines[parts[0]] = (int(parts[1]), ",".join(parts[2:]))
            elif line.startswith("FNA:"):
                parts = line[4:].split(",")
                if len(parts) >= 3 and parts[1].isdigit() and parts[0] in fn_index_lines:
                    ln, name = fn_index_lines[parts[0]]
                    _add_function(current, name, ln, int(parts[1]))
            elif line.startswith("DA:"):
                parts = line[3:].split(",")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    if int(parts[1]) > 0:
                        current["executed_lines"].add(int(parts[0]))
            elif line == "end_of_record":
                current = None
    return {"format": FORMAT_LCOV, "artifact": path, "files": files}


_GO_BLOCK = re.compile(r"^(?P<file>.+?):(?P<sl>\d+)\.(?P<sc>\d+),(?P<el>\d+)\.(?P<ec>\d+)\s+(?P<n>\d+)\s+(?P<count>\d+)\s*$")


def _go_module_name(project_dir: str) -> str:
    try:
        with open(os.path.join(project_dir, "go.mod"), "r", encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                if line.startswith("module "):
                    return line.split(None, 1)[1].strip()
    except OSError:
        pass
    return ""


def _parse_go(path: str, project_dir: str) -> dict:
    files: dict[str, dict] = {}
    module = _go_module_name(project_dir)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("mode:"):
                continue
            m = _GO_BLOCK.match(line)
            if not m:
                continue
            fpath = m.group("file")
            if module and fpath.startswith(module + "/"):
                fpath = fpath[len(module) + 1:]
            rel = _canon_rel(fpath, project_dir)
            entry = files.setdefault(rel, _empty_file())
            if int(m.group("count")) > 0:
                entry["executed_lines"].update(range(int(m.group("sl")), int(m.group("el")) + 1))
    return {"format": FORMAT_GO, "artifact": path, "files": files}


def _parse_jacoco(path: str, project_dir: str) -> dict:
    tree = ET.parse(path)
    files: dict[str, dict] = {}
    for pkg in tree.getroot().iter("package"):
        pkg_name = pkg.get("name") or ""
        for cls in pkg.findall("class"):
            src = cls.get("sourcefilename") or ""
            if not src:
                continue
            rel = _canon_rel(f"{pkg_name}/{src}" if pkg_name else src, project_dir)
            entry = files.setdefault(rel, _empty_file())
            for method in cls.findall("method"):
                covered = 0
                for counter in method.findall("counter"):
                    if counter.get("type") in {"METHOD", "INSTRUCTION"}:
                        covered = max(covered, int(counter.get("covered") or 0))
                name = method.get("name") or ""
                if name in {"<clinit>"}:
                    continue
                _add_function(entry, name, int(method.get("line") or 0), 1 if covered else 0)
        for sf in pkg.findall("sourcefile"):
            src = sf.get("name") or ""
            rel = _canon_rel(f"{pkg_name}/{src}" if pkg_name else src, project_dir)
            entry = files.setdefault(rel, _empty_file())
            for ln in sf.findall("line"):
                if int(ln.get("ci") or 0) > 0:
                    entry["executed_lines"].add(int(ln.get("nr") or 0))
    return {"format": FORMAT_JACOCO, "artifact": path, "files": files}


def _parse_cobertura(path: str, project_dir: str) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    sources = [s.text.strip() for s in root.iter("source") if s.text and s.text.strip()]
    files: dict[str, dict] = {}

    def resolve(filename: str) -> str:
        f = _fwd(filename)
        if os.path.isabs(f) or f.startswith("/") or re.match(r"^[A-Za-z]:/", f):
            return _canon_rel(f, project_dir)
        for src in sources:
            cand = os.path.join(src, filename)
            if os.path.isfile(cand):
                return _canon_rel(cand, project_dir)
        return _canon_rel(f, project_dir)

    for cls in root.iter("class"):
        filename = cls.get("filename") or ""
        if not filename:
            continue
        entry = files.setdefault(resolve(filename), _empty_file())
        lines_el = cls.find("lines")
        if lines_el is not None:
            for ln in lines_el.findall("line"):
                if int(ln.get("hits") or 0) > 0:
                    entry["executed_lines"].add(int(ln.get("number") or 0))
        methods_el = cls.find("methods")
        if methods_el is None:
            continue
        for method in methods_el.findall("method"):
            name = method.get("name") or ""
            mlines = method.find("lines")
            nums = [int(l.get("number") or 0) for l in mlines.findall("line")] if mlines is not None else []
            hits = [int(l.get("hits") or 0) for l in mlines.findall("line")] if mlines is not None else []
            for n, h in zip(nums, hits):
                if h > 0:
                    entry["executed_lines"].add(n)
            line = min(nums) if nums else 0
            hit = max(hits) if hits else (1 if float(method.get("line-rate") or 0) > 0 else 0)
            _add_function(entry, name, line, hit)
    return {"format": FORMAT_COBERTURA, "artifact": path, "files": files}


_PARSERS = {
    FORMAT_PY_JSON: _parse_python_json,
    FORMAT_ISTANBUL: _parse_istanbul,
    FORMAT_LCOV: _parse_lcov,
    FORMAT_GO: _parse_go,
    FORMAT_JACOCO: _parse_jacoco,
    FORMAT_COBERTURA: _parse_cobertura,
}


def parse_coverage(path: str, fmt: str, project_dir: str) -> dict:
    """Parse one artifact into
    {"format", "artifact", "files": {rel: {"executed_lines": set[int],
                                          "functions": {name: {"line", "hits"}}}}}.
    Raises ValueError for a format this module cannot parse (e.g. the sqlite
    store) — a caller must never mistake "not parsed" for "nothing executed"."""
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ValueError(f"cannot parse coverage format {fmt!r} from {path}")
    result = parser(path, project_dir)
    result["has_function_records"] = any(f["functions"] for f in result["files"].values())
    return result


def merge_coverage(parsed: list[dict]) -> dict:
    """Union several parsed artifacts (e.g. lcov + istanbul from one run).
    Each file remembers which artifacts/formats contributed."""
    files: dict[str, dict] = {}
    formats: list[str] = []
    artifacts: list[str] = []
    for cov in parsed:
        formats.append(cov.get("format", "?"))
        artifacts.append(cov.get("artifact", "?"))
        for rel, rec in cov.get("files", {}).items():
            entry = files.setdefault(rel, {"executed_lines": set(), "functions": {},
                                           "sources": []})
            entry["executed_lines"] |= set(rec.get("executed_lines", ()))
            for name, frec in rec.get("functions", {}).items():
                prev = entry["functions"].get(name)
                if prev is None or frec.get("hits", 0) > prev.get("hits", 0):
                    entry["functions"][name] = dict(frec)
            entry["sources"].append({"format": cov.get("format"),
                                     "artifact": cov.get("artifact")})
    return {"format": "+".join(formats) if formats else "none",
            "artifact": ";".join(artifacts), "files": files,
            "has_function_records": any(f["functions"] for f in files.values())}


# --------------------------------------------------------------------------
# Commands: propose how to run the project's OWN tests under a coverage tool
# --------------------------------------------------------------------------

def _read_package_json(project_dir: str) -> dict:
    try:
        with open(os.path.join(project_dir, "package.json"), "r", encoding="utf-8",
                  errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _node_bin_exists(project_dir: str, name: str) -> bool:
    bin_dir = os.path.join(project_dir, "node_modules", ".bin")
    return any(os.path.exists(os.path.join(bin_dir, name + ext))
               for ext in ("", ".cmd", ".ps1", ".exe"))


def _file_contains(path: str, needle: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return needle.lower() in fh.read().lower()
    except OSError:
        return False


def _cmd(ecosystem: str, argv: list[str], produces: str, *, available: bool,
         reason: str, tool: str, step: str = "run") -> dict:
    return {"ecosystem": ecosystem, "tool": tool, "step": step, "argv": list(argv),
            "produces": produces, "available": bool(available), "reason": reason}


def coverage_commands(project_dir: str, stack: dict) -> list[dict]:
    """Candidate argv lists that run the project's existing tests UNDER a
    coverage tool. `available` is grounded in a file/spec check — a tool that
    is not demonstrably present is reported `available: False` with the
    reason, never invented."""
    root = os.path.abspath(project_dir)
    eco = str((stack or {}).get("ecosystem") or "").lower()
    test_cmd = [str(a) for a in ((stack or {}).get("test_cmd") or [])]
    out: list[dict] = []

    if eco == "python":
        spec = importlib.util.find_spec("coverage")
        py = sys.executable or "python"
        if spec is None:
            out.append(_cmd(eco, [], "coverage.json", available=False, tool="coverage",
                            reason="`coverage` is not importable in this interpreter "
                                   "(pip install coverage)"))
            return out
        if test_cmd and "pytest" in " ".join(test_cmd):
            # keep the project's own pytest args, drop the interpreter prefix
            args = list(test_cmd)
            if args and os.path.basename(args[0]).lower().startswith("python"):
                args = args[1:]
            if args[:1] == ["-m"]:
                args = args[1:]
            run = [py, "-m", "coverage", "run", "--branch", "-m", *args]
        elif test_cmd and os.path.basename(test_cmd[0]).lower().startswith("python") and "-m" in test_cmd:
            run = [py, "-m", "coverage", "run", "--branch", *test_cmd[test_cmd.index("-m"):]]
        else:
            run = [py, "-m", "coverage", "run", "--branch", "-m", "pytest"]
        out.append(_cmd(eco, run, ".coverage", available=True, tool="coverage",
                        reason="coverage importable: " + str(spec.origin or spec.name)))
        out.append(_cmd(eco, [py, "-m", "coverage", "json", "-o", "coverage.json"],
                        "coverage.json", available=True, tool="coverage", step="report",
                        reason="converts the sqlite store into parseable json"))
        return out

    if eco == "node":
        pkg = _read_package_json(root)
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        has_c8 = "c8" in deps or _node_bin_exists(root, "c8")
        uses_vitest = "vitest" in deps or any("vitest" in a for a in test_cmd)
        uses_node_test = "--test" in test_cmd
        if not test_cmd:
            scripts = pkg.get("scripts") or {}
            if "test" in scripts:
                pm = str((stack or {}).get("package_manager") or "npm")
                test_cmd = [pm, "test"] if pm != "npm" else ["npm", "test"]
        if has_c8 and test_cmd:
            out.append(_cmd(eco, ["npx", "c8", "--reporter=json", "--reporter=lcov",
                                  "--reports-dir=coverage", *test_cmd],
                            "coverage/coverage-final.json", available=True, tool="c8",
                            reason="c8 present in package.json deps or node_modules/.bin"))
            return out
        if uses_vitest:
            provider = next((d for d in ("@vitest/coverage-v8", "@vitest/coverage-istanbul")
                             if d in deps), None)
            base = test_cmd if test_cmd else ["npx", "vitest", "run"]
            if "run" not in base and base[-1:] == ["vitest"]:
                base = [*base, "run"]
            out.append(_cmd(eco, [*base, "--coverage", "--coverage.reporter=json",
                                  "--coverage.reporter=lcov"],
                            "coverage/coverage-final.json", available=provider is not None,
                            tool="vitest",
                            reason=(f"{provider} in deps" if provider else
                                    "vitest found but no @vitest/coverage-v8 / "
                                    "@vitest/coverage-istanbul in deps")))
            return out
        if uses_node_test:
            out.append(_cmd(eco, [*test_cmd, "--experimental-test-coverage",
                                  "--test-reporter=lcov", "--test-reporter-destination=lcov.info"],
                            "lcov.info", available=True, tool="node:test",
                            reason="node --test runner accepts --experimental-test-coverage"))
            return out
        out.append(_cmd(eco, [], "coverage/coverage-final.json", available=False, tool="c8",
                        reason="no c8 in deps/node_modules/.bin, no vitest, no `node --test`"
                               + ("" if test_cmd else "; no test command either")))
        return out

    if eco == "go":
        has_mod = os.path.isfile(os.path.join(root, "go.mod"))
        go = shutil.which("go")
        ok = bool(has_mod and go)
        out.append(_cmd(eco, ["go", "test", "./...", "-covermode=atomic",
                              "-coverprofile=coverage.out"], "coverage.out",
                        available=ok, tool="go test",
                        reason=("go.mod present; go at " + go) if ok else
                        ("no go.mod" if not has_mod else "`go` not on PATH")))
        return out

    if eco == "rust":
        llvm_cov = shutil.which("cargo-llvm-cov") or (
            os.path.join(os.path.expanduser("~"), ".cargo", "bin", "cargo-llvm-cov")
            if os.path.exists(os.path.join(os.path.expanduser("~"), ".cargo", "bin", "cargo-llvm-cov"))
            else None)
        out.append(_cmd(eco, ["cargo", "llvm-cov", "--lcov", "--output-path", "lcov.info"],
                        "lcov.info", available=bool(llvm_cov), tool="cargo-llvm-cov",
                        reason=("found " + llvm_cov) if llvm_cov else
                        "cargo-llvm-cov not installed (cargo install cargo-llvm-cov)"))
        return out

    if eco == "java":
        pom = os.path.join(root, "pom.xml")
        gradle = next((p for p in ("build.gradle.kts", "build.gradle")
                       if os.path.isfile(os.path.join(root, p))), None)
        if os.path.isfile(pom):
            has = _file_contains(pom, "jacoco")
            out.append(_cmd(eco, ["mvn", "-q", "test", "jacoco:report"],
                            "target/site/jacoco/jacoco.xml", available=has, tool="jacoco",
                            reason="jacoco plugin declared in pom.xml" if has else
                            "pom.xml does not mention jacoco"))
        if gradle:
            has = _file_contains(os.path.join(root, gradle), "jacoco")
            wrapper = "gradlew.bat" if os.name == "nt" else "./gradlew"
            if not os.path.isfile(os.path.join(root, wrapper.lstrip("./"))):
                wrapper = "gradle"
            out.append(_cmd(eco, [wrapper, "test", "jacocoTestReport"],
                            "build/reports/jacoco/test/jacocoTestReport.xml",
                            available=has, tool="jacoco",
                            reason=f"jacoco plugin declared in {gradle}" if has else
                            f"{gradle} does not mention jacoco"))
        if not out:
            out.append(_cmd(eco, [], "jacoco.xml", available=False, tool="jacoco",
                            reason="no pom.xml / build.gradle(.kts) found"))
        return out

    if eco == "dotnet":
        projects = [p for p in Path(root).rglob("*.csproj")
                    if not any(part in SKIP_DIRS or part in {"bin", "obj"} for part in p.parts)]
        coverlet = any(_file_contains(str(p), "coverlet") for p in projects)
        dotnet = shutil.which("dotnet")
        out.append(_cmd(eco, ["dotnet", "test", "--collect:XPlat Code Coverage"],
                        "TestResults/**/coverage.cobertura.xml",
                        available=bool(projects and coverlet and dotnet), tool="coverlet",
                        reason=("coverlet referenced in a .csproj; dotnet at " + dotnet)
                        if (projects and coverlet and dotnet) else
                        ("no .csproj found" if not projects else
                         "no coverlet.collector reference in any .csproj" if not coverlet
                         else "`dotnet` not on PATH")))
        return out

    out.append(_cmd(eco or "unknown", [], "", available=False, tool="",
                    reason=f"no coverage recipe for ecosystem {eco!r}"))
    return out


# --------------------------------------------------------------------------
# Per-symbol direct-execution rows
# --------------------------------------------------------------------------

def _match_file(rel: str, cov_files: dict) -> tuple[str | None, dict | None]:
    """Exact relative match first; otherwise a UNIQUE suffix match (JaCoCo
    reports `com/x/Foo.java`, the index has `src/main/java/com/x/Foo.java`).
    Ambiguous suffixes match nothing — guessing would manufacture evidence."""
    rel = _fwd(rel)
    while rel.startswith("./"):
        rel = rel[2:]
    if rel in cov_files:
        return rel, cov_files[rel]
    cands = [k for k in cov_files
             if rel.endswith("/" + k) or k.endswith("/" + rel)]
    if len(cands) == 1:
        return cands[0], cov_files[cands[0]]
    return None, None


def _short_name(name: str) -> str:
    return str(name or "").split("@", 1)[0].rsplit(".", 1)[-1]


def direct_function_rows(index: dict, coverage: dict) -> list[dict]:
    """One row per first-party function symbol in `index` (test files
    skipped). `status` is "direct" only on tool evidence that the FUNCTION
    ran — a function hit record, or the def line plus a line strictly inside
    the body. A def line alone is module import and never counts."""
    cov_files = (coverage or {}).get("files") or {}
    fmt = (coverage or {}).get("format") or "none"
    artifact = (coverage or {}).get("artifact")
    rows: list[dict] = []
    for sym in (index or {}).get("symbols", []):
        if not str(sym.get("kind", "")).endswith("function"):
            continue
        rel = _fwd(sym.get("file") or "")
        if _is_test(rel):
            continue
        line = int(sym.get("line") or 0)
        end_line = int(sym.get("end_line") or 0)
        base = {"id": sym.get("id"), "file": rel, "line": line, "name": sym.get("name")}
        matched_rel, rec = _match_file(rel, cov_files)
        if rec is None:
            rows.append({**base, "status": "unproven", "evidence": None,
                         "reason": "no coverage artifact covers this file"})
            continue
        # (a) explicit function-hit record
        zero_hit_record = None
        hit = None
        for fname, frec in rec.get("functions", {}).items():
            fline = int(frec.get("line") or 0)
            same_line = fline and abs(fline - line) <= FUNCTION_LINE_TOLERANCE
            same_name = _short_name(fname) == _short_name(sym.get("name") or "") and (
                not fline or end_line == 0 or line <= fline <= max(end_line, line + BODY_WINDOW_WITHOUT_END))
            if same_line or same_name:
                if int(frec.get("hits") or 0) > 0:
                    hit = (fname, frec)
                    break
                zero_hit_record = (fname, frec)
        if hit is not None:
            rows.append({**base, "status": "direct",
                         "evidence": {"format": fmt, "artifact": artifact,
                                      "kind": "function-record", "record": hit[0],
                                      "hits": int(hit[1].get("hits") or 0),
                                      "coverage_file": matched_rel},
                         "reason": f"coverage function record {hit[0]!r} hits={hit[1].get('hits')}"})
            continue
        # (b) line-based: def line AND a line strictly inside the body
        executed = rec.get("executed_lines") or set()
        body_end = end_line if end_line > line else line + BODY_WINDOW_WITHOUT_END
        body_hits = sorted(n for n in executed if line < n <= body_end)
        if line in executed and body_hits:
            rows.append({**base, "status": "direct",
                         "evidence": {"format": fmt, "artifact": artifact,
                                      "kind": "line-based", "def_line_executed": True,
                                      "executed_lines_in_body": body_hits,
                                      "coverage_file": matched_rel},
                         "reason": f"def line {line} and {len(body_hits)} body line(s) executed"})
            continue
        if zero_hit_record is not None:
            reason = f"coverage function record {zero_hit_record[0]!r} shows 0 hits"
        elif line in executed:
            reason = ("def line executed but no body line executed "
                      "(module import only - NOT direct)")
        elif body_hits:
            reason = (f"body lines {body_hits[:3]} executed but def line {line} not "
                      "in executed set (symbol/line mismatch - not accepted)")
        else:
            reason = "file covered by artifact but no executed line in this function"
        rows.append({**base, "status": "unproven", "evidence": None, "reason": reason})
    return rows


def direct_function_gate(rows: list[dict], *, blocked: dict[str, str] | None = None) -> dict:
    """Fail-closed gate: complete ONLY when every first-party function is
    either directly proven or explicitly blocked WITH a reason. Module-level
    execution is never counted — rows only carry "direct" or "unproven"."""
    blocked = {str(k): str(v or "").strip() for k, v in (blocked or {}).items()}
    ids = [r.get("id") for r in rows]
    direct_ids = [r["id"] for r in rows if r.get("status") == "direct"]
    direct_set = set(direct_ids)
    blocked_ids, blocked_without_reason, unknown_blocked = [], [], []
    for bid, reason in blocked.items():
        if bid not in ids:
            unknown_blocked.append(bid)
        elif bid in direct_set:
            continue  # proven beats blocked; count it once, as direct
        elif not reason:
            blocked_without_reason.append(bid)
        else:
            blocked_ids.append(bid)
    blocked_set = set(blocked_ids)
    unproven_ids = [r["id"] for r in rows
                    if r.get("status") != "direct" and r["id"] not in blocked_set]
    total = len(rows)
    return {
        "schema": COVERAGE_ROWS_SCHEMA,
        "total": total, "direct": len(direct_ids), "unproven": len(unproven_ids),
        "blocked": len(blocked_ids),
        "complete": total == len(direct_ids) + len(blocked_ids),
        "unproven_ids": unproven_ids, "blocked_ids": blocked_ids,
        "blocked_reasons": {b: blocked[b] for b in blocked_ids},
        "blocked_without_reason": blocked_without_reason,
        "unknown_blocked_ids": unknown_blocked,
    }


def merge_into_function_coverage(fc: dict, rows: list[dict], *,
                                 blocked: dict[str, str] | None = None) -> dict:
    """Overlay direct rows (by id) on a `function_coverage()` dict from
    flexfactor_evidence, recompute the direct total, attach the gate and
    state the basis honestly. Every other key is preserved."""
    merged = dict(fc or {})
    by_id = {r.get("id"): r for r in rows}
    functions = []
    gate_rows = []
    for fn in list(merged.get("functions") or []):
        fn = dict(fn)
        row = by_id.get(fn.get("id"))
        if row is not None:
            fn["direct_function_coverage"] = row.get("status") == "direct"
            fn["direct_evidence"] = row.get("evidence")
            fn["direct_reason"] = row.get("reason")
            if row.get("status") == "direct":
                fn["status"] = "direct"
            gate_rows.append(row)
        else:
            fn["direct_function_coverage"] = False
            fn.setdefault("direct_evidence", None)
            fn["direct_reason"] = "no direct-coverage row for this function"
            gate_rows.append({"id": fn.get("id"), "file": fn.get("file"),
                              "line": fn.get("line"), "name": fn.get("name"),
                              "status": "unproven", "evidence": None,
                              "reason": fn["direct_reason"]})
        functions.append(fn)
    merged["functions"] = functions
    merged["function_total"] = len(functions)
    merged["function_direct_coverage_total"] = sum(
        bool(f.get("direct_function_coverage")) for f in functions)
    merged["direct_gate"] = direct_function_gate(gate_rows, blocked=blocked)
    merged["function_coverage_basis"] = (
        "direct-tool-evidence" if merged["function_direct_coverage_total"] > 0
        else "module-execution-only (NOT direct)")
    return merged


__all__ = [
    "detect_coverage_artifacts", "parse_coverage", "merge_coverage",
    "coverage_commands", "direct_function_rows", "direct_function_gate",
    "merge_into_function_coverage",
]
