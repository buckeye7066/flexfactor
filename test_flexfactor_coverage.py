"""Tests for flexfactor_coverage (stdlib unittest, no API keys, no subprocess).

Run: python test_flexfactor_coverage.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "eval_fixtures", "coverage")
sys.path.insert(0, HERE)

import flexfactor_coverage as cov  # noqa: E402

# The fixtures report absolute paths under /abs/proj (istanbul + cobertura);
# relativising them needs a project_dir that resolves to that root. On
# Windows os.path.abspath("/abs/proj") becomes "<drive>:/abs/proj", and the
# fixtures' "/abs/proj/..." resolve the same way, so they still agree.
PROJ = os.path.abspath("/abs/proj")


def _sym(sid, file, line, name, end_line=None, kind="function"):
    return {"id": sid, "file": file, "line": line, "name": name,
            "kind": kind, "end_line": end_line or line, "exported": True}


class ParserTests(unittest.TestCase):
    def test_istanbul_json(self):
        got = cov.parse_coverage(os.path.join(FIX, "coverage-final.json"), cov.FORMAT_ISTANBUL, PROJ)
        self.assertEqual(got["format"], cov.FORMAT_ISTANBUL)
        self.assertEqual(set(got["files"]), {"src/math.js", "src/util.js"})
        math = got["files"]["src/math.js"]
        self.assertEqual(math["functions"]["add"], {"line": 1, "hits": 4})
        self.assertEqual(math["functions"]["sub"], {"line": 5, "hits": 0})
        self.assertEqual(math["executed_lines"], {1, 2, 5})
        self.assertEqual(got["files"]["src/util.js"]["functions"]["slug"]["hits"], 2)
        self.assertTrue(got["has_function_records"])

    def test_lcov(self):
        got = cov.parse_coverage(os.path.join(FIX, "lcov.info"), cov.FORMAT_LCOV, PROJ)
        self.assertEqual(set(got["files"]), {"src/math.js", "src/util.js"})
        math = got["files"]["src/math.js"]
        self.assertEqual(math["functions"], {"add": {"line": 1, "hits": 4},
                                             "sub": {"line": 5, "hits": 0}})
        self.assertEqual(math["executed_lines"], {1, 2, 5})
        self.assertEqual(got["files"]["src/util.js"]["executed_lines"], {1, 2})

    def test_python_coverage_json(self):
        got = cov.parse_coverage(os.path.join(FIX, "coverage.json"), cov.FORMAT_PY_JSON, PROJ)
        self.assertEqual(list(got["files"]), ["pkg/calc.py"])  # backslashes normalised
        calc = got["files"]["pkg/calc.py"]
        self.assertEqual(calc["executed_lines"], {1, 4, 5, 8, 12})
        self.assertNotIn("", calc["functions"])  # module body is not a function
        self.assertEqual(calc["functions"]["add"], {"line": 5, "hits": 1})
        self.assertEqual(calc["functions"]["sub"], {"line": 9, "hits": 0})
        self.assertEqual(calc["functions"]["Calc.mul"], {"line": 13, "hits": 0})

    def test_go_coverprofile_strips_module_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "go.mod"), "w") as fh:
                fh.write("module example.com/proj\n\ngo 1.22\n")
            got = cov.parse_coverage(os.path.join(FIX, "coverage.out"), cov.FORMAT_GO, td)
        self.assertEqual(list(got["files"]), ["pkg/calc.go"])
        self.assertEqual(got["files"]["pkg/calc.go"]["executed_lines"], {5, 6, 7, 13, 14, 15})
        self.assertFalse(got["has_function_records"])

    def test_cobertura_xml(self):
        got = cov.parse_coverage(os.path.join(FIX, "coverage.cobertura.xml"), cov.FORMAT_COBERTURA, PROJ)
        self.assertEqual(list(got["files"]), ["src/Calc.cs"])
        calc = got["files"]["src/Calc.cs"]
        self.assertEqual(calc["functions"]["Add"], {"line": 6, "hits": 3})
        self.assertEqual(calc["functions"]["Sub"], {"line": 10, "hits": 0})
        self.assertEqual(calc["executed_lines"], {6, 7, 14})

    def test_jacoco_xml(self):
        got = cov.parse_coverage(os.path.join(FIX, "jacoco.xml"), cov.FORMAT_JACOCO, PROJ)
        self.assertEqual(list(got["files"]), ["com/example/core/Calc.java"])
        calc = got["files"]["com/example/core/Calc.java"]
        self.assertEqual(calc["functions"]["add"], {"line": 8, "hits": 1})
        self.assertEqual(calc["functions"]["sub"], {"line": 12, "hits": 0})
        self.assertIn("<init>", calc["functions"])
        self.assertEqual(calc["executed_lines"], {5, 8, 9})

    def test_unparseable_format_raises(self):
        with self.assertRaises(ValueError):
            cov.parse_coverage("x", cov.FORMAT_PY_SQLITE, PROJ)

    def test_merge_coverage_unions_and_keeps_best_hit(self):
        a = cov.parse_coverage(os.path.join(FIX, "lcov.info"), cov.FORMAT_LCOV, PROJ)
        b = cov.parse_coverage(os.path.join(FIX, "coverage-final.json"), cov.FORMAT_ISTANBUL, PROJ)
        m = cov.merge_coverage([a, b])
        self.assertEqual(m["format"], "lcov+istanbul-json")
        self.assertEqual(m["files"]["src/math.js"]["functions"]["add"]["hits"], 4)
        self.assertEqual(len(m["files"]["src/math.js"]["sources"]), 2)


class DetectTests(unittest.TestCase):
    def test_detects_every_fixture_without_executing(self):
        with tempfile.TemporaryDirectory() as td:
            for name in os.listdir(FIX):
                shutil.copy(os.path.join(FIX, name), td)
            os.makedirs(os.path.join(td, "node_modules", "x"))
            shutil.copy(os.path.join(FIX, "lcov.info"), os.path.join(td, "node_modules", "x", "lcov.info"))
            with open(os.path.join(td, ".coverage"), "wb") as fh:
                fh.write(b"SQLite format 3\x00")
            with open(os.path.join(td, "not-coverage.json"), "w") as fh:
                json.dump({"coverage": "of a different kind"}, fh)
            found = cov.detect_coverage_artifacts(td)
        by_rel = {f["rel"]: f for f in found}
        self.assertEqual(by_rel["coverage-final.json"]["format"], cov.FORMAT_ISTANBUL)
        self.assertEqual(by_rel["coverage.json"]["format"], cov.FORMAT_PY_JSON)
        self.assertEqual(by_rel["lcov.info"]["format"], cov.FORMAT_LCOV)
        self.assertEqual(by_rel["coverage.out"]["format"], cov.FORMAT_GO)
        self.assertEqual(by_rel["jacoco.xml"]["format"], cov.FORMAT_JACOCO)
        self.assertEqual(by_rel["coverage.cobertura.xml"]["format"], cov.FORMAT_COBERTURA)
        self.assertEqual(by_rel[".coverage"]["format"], cov.FORMAT_PY_SQLITE)
        self.assertFalse(by_rel[".coverage"]["parse"])
        for rel, f in by_rel.items():
            if rel != ".coverage":
                self.assertTrue(f["parse"], rel)
        self.assertNotIn("not-coverage.json", by_rel)
        self.assertFalse(any("node_modules" in r for r in by_rel))
        for f in found:
            self.assertEqual(set(f) >= {"path", "format", "parse", "detail"}, True)

    def test_missing_dir_is_empty_not_error(self):
        self.assertEqual(cov.detect_coverage_artifacts(os.path.join(FIX, "nope")), [])


class DirectRowsTests(unittest.TestCase):
    def setUp(self):
        self.index = {"symbols": [
            _sym("src/math.js::add@1", "src/math.js", 1, "add", 3),
            _sym("src/math.js::sub@5", "src/math.js", 5, "sub", 7),
            _sym("src/util.js::slug@1", "src/util.js", 1, "slug", 3),
            _sym("src/other.js::orphan@1", "src/other.js", 1, "orphan", 3),
            _sym("src/__tests__/x.test.js::t@1", "src/__tests__/x.test.js", 1, "t"),
            {"id": "src/math.js::Klass@9", "file": "src/math.js", "line": 9,
             "name": "Klass", "kind": "class", "end_line": 20},
        ]}

    def test_function_records_decide_direct(self):
        c = cov.parse_coverage(os.path.join(FIX, "coverage-final.json"), cov.FORMAT_ISTANBUL, PROJ)
        rows = {r["id"]: r for r in cov.direct_function_rows(self.index, c)}
        self.assertEqual(set(rows), {"src/math.js::add@1", "src/math.js::sub@5",
                                     "src/util.js::slug@1", "src/other.js::orphan@1"})
        self.assertEqual(rows["src/math.js::add@1"]["status"], "direct")
        self.assertEqual(rows["src/math.js::add@1"]["evidence"]["kind"], "function-record")
        self.assertEqual(rows["src/math.js::add@1"]["evidence"]["hits"], 4)
        self.assertEqual(rows["src/math.js::sub@5"]["status"], "unproven")
        self.assertIn("0 hits", rows["src/math.js::sub@5"]["reason"])
        self.assertEqual(rows["src/other.js::orphan@1"]["status"], "unproven")
        self.assertEqual(rows["src/other.js::orphan@1"]["reason"],
                         "no coverage artifact covers this file")

    def test_line_based_requires_def_line_AND_a_body_line(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "go.mod"), "w") as fh:
                fh.write("module example.com/proj\n")
            c = cov.parse_coverage(os.path.join(FIX, "coverage.out"), cov.FORMAT_GO, td)
        index = {"symbols": [
            _sym("pkg/calc.go::Add@5", "pkg/calc.go", 5, "Add", 7),
            _sym("pkg/calc.go::Sub@9", "pkg/calc.go", 9, "Sub", 11),
            _sym("pkg/calc.go::Mul@13", "pkg/calc.go", 13, "Mul"),  # no end_line -> 50-line window
        ]}
        rows = {r["id"]: r for r in cov.direct_function_rows(index, c)}
        self.assertEqual(rows["pkg/calc.go::Add@5"]["status"], "direct")
        self.assertEqual(rows["pkg/calc.go::Add@5"]["evidence"]["kind"], "line-based")
        self.assertEqual(rows["pkg/calc.go::Add@5"]["evidence"]["executed_lines_in_body"], [6, 7])
        self.assertEqual(rows["pkg/calc.go::Sub@9"]["status"], "unproven")
        self.assertEqual(rows["pkg/calc.go::Mul@13"]["status"], "direct")

    def test_module_import_alone_is_never_direct(self):
        # def line executed (import), body never ran: the exact trap.
        c = {"format": "synthetic", "artifact": "x", "files": {
            "pkg/calc.py": {"executed_lines": {1, 4, 8}, "functions": {}}}}
        index = {"symbols": [_sym("pkg/calc.py::add@4", "pkg/calc.py", 4, "add", 6)]}
        [row] = cov.direct_function_rows(index, c)
        self.assertEqual(row["status"], "unproven")
        self.assertIn("module import only", row["reason"])

    def test_python_json_name_match_with_body_offset(self):
        c = cov.parse_coverage(os.path.join(FIX, "coverage.json"), cov.FORMAT_PY_JSON, PROJ)
        index = {"symbols": [
            _sym("pkg/calc.py::add@4", "pkg/calc.py", 4, "add", 5),
            _sym("pkg/calc.py::sub@8", "pkg/calc.py", 8, "sub", 9),
            _sym("pkg/calc.py::Calc.mul@12", "pkg/calc.py", 12, "mul", 13),
        ]}
        rows = {r["id"]: r for r in cov.direct_function_rows(index, c)}
        self.assertEqual(rows["pkg/calc.py::add@4"]["status"], "direct")
        self.assertEqual(rows["pkg/calc.py::sub@8"]["status"], "unproven")
        self.assertEqual(rows["pkg/calc.py::Calc.mul@12"]["status"], "unproven")

    def test_jacoco_suffix_match_against_src_layout(self):
        c = cov.parse_coverage(os.path.join(FIX, "jacoco.xml"), cov.FORMAT_JACOCO, PROJ)
        index = {"symbols": [
            _sym("src/main/java/com/example/core/Calc.java::add@8",
                 "src/main/java/com/example/core/Calc.java", 8, "add", 10),
            _sym("src/main/java/com/example/core/Calc.java::sub@12",
                 "src/main/java/com/example/core/Calc.java", 12, "sub", 14),
        ]}
        rows = {r["id"]: r for r in cov.direct_function_rows(index, c)}
        self.assertEqual(rows["src/main/java/com/example/core/Calc.java::add@8"]["status"], "direct")
        self.assertEqual(rows["src/main/java/com/example/core/Calc.java::sub@12"]["status"], "unproven")


class GateTests(unittest.TestCase):
    def _rows(self):
        return [
            {"id": "a", "status": "direct", "evidence": {"kind": "function-record"}, "reason": ""},
            {"id": "b", "status": "unproven", "evidence": None, "reason": "module import only"},
            {"id": "c", "status": "unproven", "evidence": None, "reason": "no artifact"},
        ]

    def test_module_import_never_completes(self):
        g = cov.direct_function_gate(self._rows())
        self.assertFalse(g["complete"])
        self.assertEqual((g["total"], g["direct"], g["unproven"], g["blocked"]), (3, 1, 2, 0))
        self.assertEqual(g["unproven_ids"], ["b", "c"])

    def test_blocked_with_reason_counts_and_completes_only_at_identity(self):
        g = cov.direct_function_gate(self._rows(), blocked={"b": "destructive: drops production table"})
        self.assertFalse(g["complete"])
        self.assertEqual(g["blocked_ids"], ["b"])
        self.assertEqual(g["unproven_ids"], ["c"])
        g2 = cov.direct_function_gate(self._rows(), blocked={
            "b": "destructive: drops production table", "c": "requires live payment gateway"})
        self.assertTrue(g2["complete"])
        self.assertEqual(g2["total"], g2["direct"] + g2["blocked"])

    def test_blocked_without_reason_does_not_count(self):
        g = cov.direct_function_gate(self._rows(), blocked={"b": "", "c": "  "})
        self.assertFalse(g["complete"])
        self.assertEqual(g["blocked"], 0)
        self.assertEqual(g["blocked_without_reason"], ["b", "c"])

    def test_unknown_blocked_ids_are_named_not_counted(self):
        g = cov.direct_function_gate(
            self._rows(), blocked={"zzz": "hardware-bound: needs the label printer"})
        self.assertEqual(g["unknown_blocked_ids"], ["zzz"])
        self.assertEqual(g["blocked"], 0)


class BlockedDeclarationTests(unittest.TestCase):
    """g-4. A blocked function must carry a reason, and an unreasoned block has
    to be IMPOSSIBLE TO EXPRESS - not merely ignored, and not silently dropped.

    Both of the failure modes here are ones the governing contract rejects by
    name: a dropped declaration UNDER-REPORTS (the owner declared something and
    no surface says what became of it), and an accepted reason-less block
    produces FALSE CONFIDENCE (a function counts as accounted for with no
    account given).
    """

    def test_a_block_without_a_reason_cannot_be_constructed(self):
        for bad in ("", "   ", None, "n/a", "-", "x"):
            with self.assertRaises(cov.BlockedDeclarationError, msg=repr(bad)):
                cov.BlockedFunction("sym", bad)

    def test_a_block_without_an_id_cannot_be_constructed(self):
        with self.assertRaises(cov.BlockedDeclarationError):
            cov.BlockedFunction("  ", "destructive against production data")

    def test_a_valid_block_is_immutable_and_normalised(self):
        b = cov.BlockedFunction("  sym  ", "  destructive  against\n production ")
        self.assertEqual(b.id, "sym")
        self.assertEqual(b.reason, "destructive against production")
        with self.assertRaises(AttributeError):
            b.reason = "something else"

    def test_a_rejected_declaration_is_reported_never_dropped(self):
        accepted, rejected = cov.blocked_declarations({"a": "", "b": "needs live gateway"})
        self.assertEqual([b.id for b in accepted], ["b"])
        self.assertEqual([r["id"] for r in rejected], ["a"])
        self.assertIn("no usable reason", rejected[0]["why"])

    def test_a_non_mapping_payload_is_one_named_rejection(self):
        accepted, rejected = cov.blocked_declarations(["a", "b"])
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("got list", rejected[0]["why"])

    def test_the_gate_accounts_for_every_declared_entry(self):
        rows = [{"id": "a", "status": "direct"},
                {"id": "b", "status": "unproven"},
                {"id": "c", "status": "unproven"}]
        g = cov.direct_function_gate(rows, blocked={
            "a": "declared, but the tool proved it anyway",
            "b": "",
            "c": "requires a live payment gateway",
            "zzz": "names no function in this repository",
        })
        self.assertEqual(g["blocked_declared"], 4)
        self.assertEqual(g["blocked_ids"], ["c"])
        self.assertEqual(g["blocked_without_reason"], ["b"])
        self.assertEqual(g["unknown_blocked_ids"], ["zzz"])
        self.assertEqual(g["blocked_superseded_by_direct"], ["a"])
        # The identity: nothing declared may go unaccounted for.
        self.assertEqual(
            g["blocked_declared"],
            g["blocked"] + len(g["blocked_without_reason"])
            + len(g["unknown_blocked_ids"]) + len(g["blocked_superseded_by_direct"]))

    def test_an_unreasoned_block_never_counts_as_covered(self):
        rows = [{"id": "b", "status": "unproven"}]
        g = cov.direct_function_gate(rows, blocked={"b": ""})
        self.assertFalse(g["complete"])
        self.assertEqual(g["blocked"], 0)
        self.assertEqual(g["unproven_ids"], ["b"])

    def test_blocked_functions_are_accepted_as_objects_too(self):
        rows = [{"id": "b", "status": "unproven"}]
        g = cov.direct_function_gate(
            rows, blocked=[cov.BlockedFunction("b", "destructive: drops the table")])
        self.assertTrue(g["complete"])
        self.assertEqual(g["blocked_reasons"], {"b": "destructive: drops the table"})

    def test_load_reports_a_missing_file_as_absent_not_as_an_error(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        accepted, rejected, meta = cov.load_blocked_declarations(d)
        self.assertEqual((accepted, rejected), ([], []))
        self.assertFalse(meta["present"])

    def test_load_reports_an_unparseable_file_instead_of_ignoring_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, cov.BLOCKED_DECLARATION_FILE), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        accepted, rejected, meta = cov.load_blocked_declarations(d)
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertTrue(meta["present"])
        self.assertIn("could not be read", rejected[0]["why"])

    def test_load_keeps_the_reason_less_entry_visible(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, cov.BLOCKED_DECLARATION_FILE), "w",
                  encoding="utf-8") as fh:
            json.dump({"good": "hardware-bound: needs the label printer",
                       "bad": ""}, fh)
        accepted, rejected, meta = cov.load_blocked_declarations(d)
        self.assertEqual([b.id for b in accepted], ["good"])
        self.assertEqual([r["id"] for r in rejected], ["bad"])
        self.assertEqual((meta["declared"], meta["accepted"], meta["rejected"]),
                         (2, 1, 1))

    def test_the_merged_view_labels_blocked_distinctly_from_covered(self):
        fc = {"functions": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"},
                            {"id": "c", "name": "c"}]}
        rows = [{"id": "a", "status": "direct", "evidence": {"k": 1}, "reason": "ran"},
                {"id": "b", "status": "unproven", "evidence": None, "reason": "no"},
                {"id": "c", "status": "unproven", "evidence": None, "reason": "no"}]
        merged = cov.merge_into_function_coverage(
            fc, rows, blocked={"b": "destructive: wipes the production bucket"})
        states = {f["id"]: f["coverage_state"] for f in merged["functions"]}
        self.assertEqual(states, {"a": "direct", "b": "blocked-with-reason",
                                  "c": "unproven"})
        self.assertEqual(merged["function_blocked_total"], 1)
        # Blocked never inflates the DIRECT count.
        self.assertEqual(merged["function_direct_coverage_total"], 1)
        self.assertEqual(merged["functions"][1]["blocked_reason"],
                         "destructive: wipes the production bucket")

    def test_a_status_that_is_not_direct_is_never_counted(self):
        rows = [{"id": "m", "status": "module-executed", "evidence": None, "reason": ""}]
        g = cov.direct_function_gate(rows)
        self.assertEqual(g["direct"], 0)
        self.assertFalse(g["complete"])

    def test_empty_rows_is_vacuously_complete(self):
        self.assertTrue(cov.direct_function_gate([])["complete"])


class CoverageCommandsTests(unittest.TestCase):
    def test_node_without_c8_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w") as fh:
                json.dump({"name": "x", "scripts": {"test": "mocha"}, "devDependencies": {"mocha": "^10"}}, fh)
            got = cov.coverage_commands(td, {"ecosystem": "node", "test_cmd": ["npm", "test"]})
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["available"])
        self.assertIn("c8", got[0]["reason"])

    def test_node_with_c8_binary_is_available(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w") as fh:
                json.dump({"name": "x", "scripts": {"test": "mocha"}}, fh)
            os.makedirs(os.path.join(td, "node_modules", ".bin"))
            with open(os.path.join(td, "node_modules", ".bin", "c8.cmd"), "w"):
                pass
            got = cov.coverage_commands(td, {"ecosystem": "node", "test_cmd": ["npm", "test"]})
        self.assertTrue(got[0]["available"])
        self.assertEqual(got[0]["argv"][:3], ["npx", "c8", "--reporter=json"])
        self.assertEqual(got[0]["argv"][-2:], ["npm", "test"])
        self.assertEqual(got[0]["produces"], "coverage/coverage-final.json")

    def test_node_c8_in_devdependencies_is_available(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w") as fh:
                json.dump({"devDependencies": {"c8": "^9"}, "scripts": {"test": "node --test"}}, fh)
            got = cov.coverage_commands(td, {"ecosystem": "node", "test_cmd": ["npm", "test"]})
        self.assertTrue(got[0]["available"])

    def test_vitest_without_coverage_provider_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w") as fh:
                json.dump({"devDependencies": {"vitest": "^2"}}, fh)
            got = cov.coverage_commands(td, {"ecosystem": "node", "test_cmd": ["npx", "vitest", "run"]})
        self.assertFalse(got[0]["available"])
        self.assertIn("--coverage", got[0]["argv"])

    def test_python_is_grounded_in_find_spec(self):
        with tempfile.TemporaryDirectory() as td:
            got = cov.coverage_commands(td, {"ecosystem": "python", "test_cmd": ["python", "-m", "pytest", "-q"]})
        have = importlib.util.find_spec("coverage") is not None
        self.assertEqual(got[0]["available"], have)
        if have:
            self.assertEqual(got[0]["argv"][1:7], ["-m", "coverage", "run", "--branch", "-m", "pytest"])
            self.assertEqual(got[0]["argv"][-1], "-q")
            self.assertEqual(got[1]["argv"][-2:], ["-o", "coverage.json"])
        else:
            self.assertIn("not importable", got[0]["reason"])

    def test_go_without_go_mod_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            got = cov.coverage_commands(td, {"ecosystem": "go"})
        self.assertFalse(got[0]["available"])
        self.assertEqual(got[0]["reason"], "no go.mod")

    def test_java_pom_without_jacoco_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pom.xml"), "w") as fh:
                fh.write("<project></project>")
            got = cov.coverage_commands(td, {"ecosystem": "java"})
        self.assertFalse(got[0]["available"])
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pom.xml"), "w") as fh:
                fh.write("<project><plugin><artifactId>jacoco-maven-plugin</artifactId></plugin></project>")
            got = cov.coverage_commands(td, {"ecosystem": "java"})
        self.assertTrue(got[0]["available"])

    def test_dotnet_without_coverlet_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "App.csproj"), "w") as fh:
                fh.write("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>")
            got = cov.coverage_commands(td, {"ecosystem": "dotnet"})
        self.assertFalse(got[0]["available"])
        self.assertIn("coverlet", got[0]["reason"])

    def test_unknown_ecosystem_never_invents_a_tool(self):
        got = cov.coverage_commands(FIX, {"ecosystem": "cobol"})
        self.assertFalse(got[0]["available"])
        self.assertEqual(got[0]["argv"], [])


class MergeIntoFunctionCoverageTests(unittest.TestCase):
    def test_overlay_preserves_other_keys_and_recomputes(self):
        fc = {
            "schema": "flexfactor.coverage_ledger.v1", "run_id": "r1",
            "functions": [
                {"id": "a", "file": "src/a.js", "line": 1, "name": "a", "status": "module-executed",
                 "invocation_evidence": {"type": "native-test-import-path"}, "direct_function_coverage": False},
                {"id": "b", "file": "src/b.js", "line": 1, "name": "b", "status": "unproven",
                 "invocation_evidence": None, "direct_function_coverage": False},
            ],
            "routes": [1, 2], "function_total": 2,
            "function_module_execution_total": 1, "function_direct_coverage_total": 0,
            "tests": {"ran": True}, "executed_modules": ["src/a.js"],
        }
        rows = [{"id": "a", "status": "direct", "evidence": {"kind": "function-record", "hits": 2},
                 "reason": "hit"}]
        merged = cov.merge_into_function_coverage(fc, rows, blocked={"b": "destructive"})
        self.assertEqual(merged["routes"], [1, 2])
        self.assertEqual(merged["executed_modules"], ["src/a.js"])
        self.assertEqual(merged["schema"], fc["schema"])
        self.assertEqual(merged["function_direct_coverage_total"], 1)
        self.assertEqual(merged["function_coverage_basis"], "direct-tool-evidence")
        self.assertTrue(merged["functions"][0]["direct_function_coverage"])
        self.assertEqual(merged["functions"][0]["status"], "direct")
        self.assertFalse(merged["functions"][1]["direct_function_coverage"])
        self.assertEqual(merged["functions"][1]["status"], "unproven")
        self.assertTrue(merged["direct_gate"]["complete"])
        self.assertEqual(merged["direct_gate"]["blocked_ids"], ["b"])
        # the input is not mutated
        self.assertFalse(fc["functions"][0]["direct_function_coverage"])
        self.assertNotIn("direct_gate", fc)

    def test_module_execution_only_basis_when_no_direct_rows(self):
        fc = {"functions": [{"id": "a", "status": "module-executed", "direct_function_coverage": False}],
              "function_direct_coverage_total": 0, "extra": "kept"}
        merged = cov.merge_into_function_coverage(fc, [])
        self.assertEqual(merged["function_coverage_basis"], "module-execution-only (NOT direct)")
        self.assertFalse(merged["direct_gate"]["complete"])
        self.assertEqual(merged["extra"], "kept")


if __name__ == "__main__":
    unittest.main(verbosity=1)
