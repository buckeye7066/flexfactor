"""The five defects that made a prodready run structurally unable to finish.

Measured on the FreeAndClean prodready run
`freeandclean-20260905-070934-191057-33300-0000` (2026-09-05/06). It was not a
crash: after 10 resumes, 3 commits landed, 64/82 file fixes applied and $20.11
of a $25 cap spent, the process exited NORMALLY with `status: "interrupted"`,
because `run_complete` requires every quality gate to pass and four of them
could never pass -- for reasons in FlexFactor's own code, not in the audited
program.

Each test below reproduces one of those reasons on the exact shape that
produced it. All run offline: no credentials, no network, no tokens spent.
"""

from __future__ import annotations

import inspect
import unittest

import flexfactor as ff
import flexfactor_coverage as ffc
import flexfactor_evidence as ffe
import flexfactor_partial as ffp


class FinalReviewAbsenceIsNotAnAnswer(unittest.TestCase):
    """DEFECT 1: a missing field was read as a substantive negative verdict.

    `_independent_final_review` does `data.get("commit") != final_sha` and
    `data.get("evidence_consistent") is True`. The judges omitted both fields
    on ALL SIX chunks, so the ledger filed six HIGH findings reading
    `expected 9582def..., reviewer said None` -- the reviewer named NOTHING, it
    did not name something DIFFERENT -- and `independent-final-review` became
    mathematically unpassable.

    The guard lives in `_judge`, the chokepoint every judging call passes
    through, and deliberately AFTER the partial-output downgrade: truncation
    already explains an absence, and a salvaged answer has already been made
    unable to authorize anything.
    """

    class _Provider:
        """A provider whose structured() returns exactly what it was given."""

        judge_model = "judge-x"
        model = "author-x"

        def __init__(self, payload, *, truncated=False):
            self._payload = payload
            self._truncated = truncated

        def structured(self, system, prompt, schema, max_tokens=8000, model=None,
                       salvage_truncated=False, **kwargs):
            data = ff._check_structured_type(self._payload, schema, "{}")
            if self._truncated:
                return ff._mark_partial(data, '{"verdict": "appr', "anthropic")
            return data

    def setUp(self):
        ff._PARTIAL_OUTPUT_EVENTS.clear()

    def _judge(self, payload, *, truncated=False):
        return ff._judge(self._Provider(payload, truncated=truncated),
                         "sys", "prompt", ff.FINAL_REVIEW_SCHEMA)

    def test_the_exact_response_that_failed_the_run_is_now_a_provider_fault(self):
        with self.assertRaises(ff.StructuredOutputShapeError) as caught:
            self._judge({"verdict": "approve", "findings": [], "reason": "fine"})
        message = str(caught.exception)
        self.assertIn("commit", message)
        self.assertIn("evidence_consistent", message)

    def test_a_complete_answer_still_passes_even_with_an_empty_reason(self):
        data = self._judge({"verdict": "approve", "commit": "9582def",
                            "evidence_consistent": True, "findings": [],
                            "reason": ""})
        self.assertEqual(data["commit"], "9582def")

    def test_a_genuine_negative_answer_is_not_touched(self):
        # evidence_consistent=False is a REAL verdict and must flow through.
        data = self._judge({"verdict": "reject", "commit": "9582def",
                            "evidence_consistent": False, "findings": [],
                            "reason": "evidence does not support the change"})
        self.assertIs(data["evidence_consistent"], False)

    def test_an_empty_commit_string_is_not_an_attestation(self):
        with self.assertRaises(ff.StructuredOutputShapeError):
            self._judge({"verdict": "approve", "commit": "   ",
                         "evidence_consistent": True, "findings": [],
                         "reason": "r"})

    def test_a_TRUNCATED_answer_is_downgraded_not_raised(self):
        """Section 12 keeps salvaged findings as failure evidence.

        Truncation already explains why `commit` is missing, and the partial
        machinery has already forced the verdict off `approve`. Raising here
        would discard a salvaged review the run is entitled to see.
        """
        data = self._judge({"verdict": "approve", "findings": []}, truncated=True)
        self.assertNotEqual(data["verdict"], "approve")
        self.assertTrue(ffp.is_partial_structured(data))

    def test_other_schemas_keep_their_deliberate_leniency(self):
        # The partial-answer tolerance exists for review schemas whose callers
        # use fail-safe .get() defaults. Only FINAL_REVIEW_SCHEMA is strict.
        data = ff._check_structured_type({"findings": []}, ff.AUDIT_FINDINGS_SCHEMA, "{}")
        self.assertEqual(data["findings"], [])


class RouteDetectorDoesNotInventRoutes(unittest.TestCase):
    r"""DEFECT 2: `os.environ.get("FAC_LIVE")` was indexed as `GET FAC_LIVE`.

    The detector regexed `\.(get|post|...)\(` over the UNPARSED decorator, so a
    nested call anywhere inside any decorator became a route. FreeAndClean -- a
    desktop file-cleaner with no web surface -- reported `routes: 2` from two
    pytest skipif markers. `routes` non-empty makes `behavior_applicable` True,
    and the `behavior` gate is then BLOCKED forever: nothing can behaviorally
    execute a route that does not exist.
    """

    FAC_LIVE_SKIPIF = (
        "import os, pytest\n"
        "@pytest.mark.live\n"
        '@pytest.mark.skipif(os.environ.get("FAC_LIVE") != "1", reason="live G:")\n'
        "def test_harden_refuses_exfat_g_target_if_present():\n"
        "    pass\n"
    )

    def test_env_var_lookup_in_a_decorator_is_not_a_route(self):
        for rel in ("tests/test_migrate.py", "system_cleaner/migrate.py"):
            with self.subTest(rel=rel):
                parsed = ffe._parse_python(rel, self.FAC_LIVE_SKIPIF)
                self.assertEqual(parsed["routes"], [])

    def test_real_route_decorators_are_still_found(self):
        src = ('@app.get("/health")\n'
               "def health():\n"
               "    pass\n"
               '@app.post("/items/{iid}")\n'
               "def create(iid):\n"
               "    pass\n"
               '@bp.route("/legacy")\n'
               "def legacy():\n"
               "    pass\n")
        routes = ffe._parse_python("api/server.py", src)["routes"]
        self.assertEqual([(r["method"], r["path"]) for r in routes],
                         [("GET", "/health"), ("POST", "/items/{iid}"),
                          ("GET", "/legacy")])

    def test_a_route_defined_in_a_test_file_is_not_the_products_route(self):
        src = '@app.get("/fixture")\ndef fixture():\n    pass\n'
        self.assertEqual(ffe._parse_python("tests/test_api.py", src)["routes"], [])

    def test_a_non_path_argument_is_not_a_route(self):
        src = '@registry.get("SOME_KEY")\ndef handler():\n    pass\n'
        self.assertEqual(ffe._parse_python("app/x.py", src)["routes"], [])


class BareRelativeImportsResolve(unittest.TestCase):
    """DEFECT 3: `from . import migrate` was indexed as module "." .

    `ast.ImportFrom` puts the target in `names`, not in `module` (which is
    None), so the indexer recorded only the dots and threw the target away.
    Module "." resolves to nothing, so `dependency_blast_radius` filed it in
    `unresolved_local_imports` and `quality_gates` failed the `blast-radius`
    gate on it. FreeAndClean has 7 of these. Every Python package using the
    ordinary relative-import form hit this.
    """

    def test_the_target_of_a_bare_relative_import_is_recorded(self):
        parsed = ffe._parse_python(
            "system_cleaner/storage_auto.py",
            "from . import migrate\nfrom . import storage_strategy as strategy\n")
        self.assertEqual([i["module"] for i in parsed["imports"]],
                         [".migrate", ".storage_strategy"])

    def test_dotted_and_absolute_forms_are_unchanged(self):
        parsed = ffe._parse_python(
            "pkg/a.py", "from .sib import thing\nfrom ..up import other\nimport os\n")
        self.assertEqual([i["module"] for i in parsed["imports"]],
                         [".sib", "..up", "os"])

    def test_star_import_is_not_recorded_as_a_name(self):
        parsed = ffe._parse_python("pkg/a.py", "from . import *\n")
        self.assertEqual(parsed["imports"], [])

    @staticmethod
    def _index(files, imports):
        return {"files": [{"path": p, "category": "source"} for p in files],
                "imports": imports}

    def test_the_gate_no_longer_fails_on_a_resolvable_relative_import(self):
        index = self._index(
            ["system_cleaner/migrate.py", "system_cleaner/storage_auto.py"],
            [{"file": "system_cleaner/storage_auto.py", "module": ".migrate", "line": 9}])
        blast = ffe.dependency_blast_radius(index, ["system_cleaner/migrate.py"])
        self.assertEqual(blast["unresolved_local_imports"], [])
        self.assertIn("system_cleaner/storage_auto.py", blast["affected"])

    def test_resolution_is_anchored_on_the_importer_not_the_basename(self):
        # Two migrate.py in different packages: the relative import must reach
        # its OWN sibling, never the other one.
        index = self._index(
            ["one/migrate.py", "two/migrate.py", "one/user.py"],
            [{"file": "one/user.py", "module": ".migrate", "line": 1}])
        blast = ffe.dependency_blast_radius(index, ["two/migrate.py"])
        self.assertNotIn("one/user.py", blast["affected"])
        blast = ffe.dependency_blast_radius(index, ["one/migrate.py"])
        self.assertIn("one/user.py", blast["affected"])

    def test_a_relative_import_that_leaves_the_tree_is_still_unresolved(self):
        index = self._index(["a.py"], [{"file": "a.py", "module": "..gone", "line": 1}])
        blast = ffe.dependency_blast_radius(index, ["a.py"])
        self.assertEqual([u["module"] for u in blast["unresolved_local_imports"]],
                         ["..gone"])


class CoverageGateSeparatesUnmeasurableFromUnproven(unittest.TestCase):
    """DEFECT 4: a .ps1 function counted as a coverage failure under `coverage`.

    `direct_function_gate` required `total == direct + blocked`, and
    `python -m coverage` cannot instrument PowerShell -- ever. FreeAndClean
    carries 57 such functions, so no amount of testing could clear
    `function-coverage`. "Outside what the configured tooling can measure" and
    "the project never exercised it" are different facts.
    """

    PY_COVERAGE = {"format": ffc.FORMAT_PY_JSON, "files": {
        "app/used.py": {"executed_lines": {1, 2}, "functions": {}}}}

    INDEX = {"symbols": [
        {"id": "app/used.py::run", "name": "run", "kind": "function",
         "file": "app/used.py", "line": 1, "end_line": 3},
        {"id": "app/never.py::cold", "name": "cold", "kind": "function",
         "file": "app/never.py", "line": 1, "end_line": 3},
        {"id": "run_cleaner.ps1::Invoke", "name": "Invoke", "kind": "function",
         "file": "run_cleaner.ps1", "line": 1, "end_line": 3},
    ]}

    def test_powershell_is_unmeasurable_and_python_absence_is_still_unproven(self):
        rows = ffc.direct_function_rows(self.INDEX, self.PY_COVERAGE)
        by_id = {r["id"]: r["status"] for r in rows}
        self.assertEqual(by_id["run_cleaner.ps1::Invoke"], "unmeasurable")
        self.assertEqual(by_id["app/never.py::cold"], "unproven")

    def test_the_unmeasurable_reason_names_the_tooling_gap(self):
        rows = ffc.direct_function_rows(self.INDEX, self.PY_COVERAGE)
        reason = next(r["reason"] for r in rows if r["id"] == "run_cleaner.ps1::Invoke")
        self.assertIn(".ps1", reason)

    def test_unmeasurable_closes_the_accounting_but_never_reads_as_covered(self):
        rows = ffc.direct_function_rows(self.INDEX, self.PY_COVERAGE)
        gate = ffc.direct_function_gate(rows, blocked={})
        self.assertEqual(gate["unmeasurable"], 1)
        self.assertNotIn("run_cleaner.ps1::Invoke", gate["unproven_ids"])
        self.assertIn("run_cleaner.ps1::Invoke", gate["unmeasurable_ids"])
        # Still incomplete: app/never.py::cold is a REAL, measurable gap.
        self.assertFalse(gate["complete"])

    def test_a_genuinely_unproven_function_still_blocks_the_gate(self):
        index = {"symbols": [s for s in self.INDEX["symbols"]
                             if s["file"] != "app/never.py"]}
        rows = ffc.direct_function_rows(index, self.PY_COVERAGE)
        gate = ffc.direct_function_gate(rows, blocked={})
        self.assertEqual(gate["unproven"], 0)
        self.assertTrue(gate["complete"])

    def test_with_no_parsed_artifact_nothing_is_excused(self):
        # No tool report means no basis on which to call anything unmeasurable.
        rows = ffc.direct_function_rows(self.INDEX, {"format": None, "files": {}})
        self.assertEqual({r["status"] for r in rows}, {"unproven"})


class ProdreadyInstallsItsOwnCoverageTool(unittest.TestCase):
    """DEFECT 5: `coverage` was missing and prodready only reported that.

    prodready's contract is detect -> install -> fix -> score. `coverage` is
    not importable in any interpreter on the owner's machine, so
    `coverage_commands` returned `available: False`, no artifact was produced,
    and all 596 FreeAndClean functions stayed "module-execution-only (NOT
    direct)" -- 0/596 on `function-coverage`. Detecting a missing free dev tool
    and then declining to install it IS the detect-only behaviour prodready
    exists to replace.
    """

    def test_the_install_step_is_wired_into_the_evidence_path(self):
        source = inspect.getsource(ff._direct_coverage_evidence)
        self.assertIn('"-m", "pip", "install"', source)
        self.assertIn("coverage", source)

    def test_coverage_commands_still_refuses_to_invent_an_absent_tool(self):
        # The install is an ATTEMPT; the availability report stays grounded.
        cmds = ffc.coverage_commands(".", {"ecosystem": "python", "test_cmd": []})
        for cmd in cmds:
            if not cmd.get("available"):
                self.assertTrue(cmd.get("reason"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
