"""Behavioral proof for FlexFactor target-repository publication."""
from __future__ import annotations

import inspect
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import flexfactor as ff
import flexfactor_product_invariants as product_invariants


def _run(*argv: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _quality_rows(overrides: dict[str, str] | None = None) -> dict:
    states = {
        "tests": "pass",
        "secrets": "pass",
        "inventory": "pass",
        "rescan": "pass",
        "blast-radius": "pass",
        "independent-final-review": "pass",
        # Whole-product completeness gates intentionally remain open.
        "function-coverage": "fail",
        "behavior": "blocked",
        "build": "fail",  # a repaired red baseline; final build is rerun at push
    }
    states.update(overrides or {})
    return {
        "gates": [
            {"id": gate_id, "status": status}
            for gate_id, status in states.items()
        ]
    }


def _product_rows(overrides: dict[str, str] | None = None) -> dict:
    states = {
        "competitive-provenance": "pass",
        "competitive-fit-risk-reviewed": "pass",
        "no-blind-competitor-copying": "pass",
        "selected-capabilities-delivered": "pass",
        # Remaining purpose work must not strand a safe repair.
        "purpose-fulfilled": "fail",
        "competitive-coverage": "blocked",
    }
    states.update(overrides or {})
    return {
        "ready": False,
        "gates": [
            {"id": gate_id, "status": status}
            for gate_id, status in states.items()
        ],
    }


class IncrementalPublicationSafetyTests(unittest.TestCase):
    def test_completeness_gaps_do_not_strand_a_safe_verified_edit(self):
        ready, reason = ff._publication_safety_ready(
            _quality_rows(), _product_rows()
        )
        self.assertTrue(ready, reason)
        self.assertIn("completeness gaps may remain", reason)

    def test_runtime_product_gate_schema_uses_passed_boolean(self):
        product = {
            "gates": [
                product_invariants._gate(gate_id, True, "verified", "fix it")
                for gate_id in ff._PUBLICATION_PRODUCT_GATE_IDS
            ]
        }
        self.assertTrue(all("status" not in row for row in product["gates"]))
        ready, reason = ff._publication_safety_ready(_quality_rows(), product)
        self.assertTrue(ready, reason)

    def test_provenance_and_risk_review_are_publication_requirements(self):
        for gate_id in ("competitive-provenance", "competitive-fit-risk-reviewed"):
            with self.subTest(gate_id=gate_id):
                product = _product_rows({gate_id: "fail"})
                ready, reason = ff._publication_safety_ready(_quality_rows(), product)
                self.assertFalse(ready)
                self.assertIn(gate_id, reason)

    def test_independent_review_receives_only_publication_scoped_gates(self):
        full = _quality_rows()
        scoped = ff._publication_review_quality_gates(full, full)
        self.assertEqual("candidate-publication-safety", scoped["scope"])
        self.assertTrue(scoped["passed"])
        ids = {row["id"] for row in scoped["gates"]}
        self.assertNotIn("function-coverage", ids)
        self.assertNotIn("behavior", ids)
        self.assertNotIn("build", ids)
        self.assertNotIn("independent-final-review", ids)
        self.assertEqual("fail", next(
            row["status"] for row in full["gates"]
            if row["id"] == "function-coverage"
        ))

    def test_a_retained_PASSING_completeness_row_does_not_fail_the_scope(self):
        # Reported on PR #153: a green candidate-safety snapshot read as a
        # FAILURE because the scoped check demanded the surviving gate ids be
        # EQUAL to the candidate-safety set. `build` passes in both snapshots,
        # so the baseline filter (which only drops rows that were ALREADY
        # fail/blocked) rightly keeps it - and the equality then turned that
        # retained green row into a failure, `_verification_passed` stayed
        # false, and `selected-capabilities-delivered` blocked publication
        # after a successful suite.
        final = _quality_rows({"build": "pass"})
        baseline = _quality_rows({"build": "pass"})
        scoped = ff._publication_review_quality_gates(final, baseline)
        self.assertIn("build", {row["id"] for row in scoped["gates"]})
        self.assertTrue(scoped["passed"])

    def test_a_retained_FAILING_completeness_row_still_fails_the_scope(self):
        # The other half, and the reason non-candidate rows are retained at
        # all: a candidate that turns `behavior` red must not read green just
        # because every candidate-safety gate passes.
        final = _quality_rows({"behavior": "fail"})
        baseline = _quality_rows({"behavior": "pass"})
        scoped = ff._publication_review_quality_gates(final, baseline)
        self.assertIn("behavior", {row["id"] for row in scoped["gates"]})
        self.assertFalse(scoped["passed"])

    def test_a_missing_candidate_safety_gate_is_never_a_pass(self):
        final = _quality_rows()
        final["gates"] = [
            row for row in final["gates"] if row["id"] != "tests"
        ]
        scoped = ff._publication_review_quality_gates(final, final)
        self.assertNotIn("tests", {row["id"] for row in scoped["gates"]})
        self.assertFalse(scoped["passed"])

    def test_new_or_unproven_completeness_failure_stays_in_review(self):
        final = _quality_rows({"function-coverage": "fail"})
        baseline = _quality_rows({"function-coverage": "pass"})
        scoped = ff._publication_review_quality_gates(final, baseline)
        row = next(g for g in scoped["gates"] if g["id"] == "function-coverage")
        self.assertEqual("fail", row["status"])
        self.assertFalse(scoped["passed"])

        unknown_baseline = ff._publication_review_quality_gates(final)
        self.assertIn(
            "function-coverage",
            {g["id"] for g in unknown_baseline["gates"]},
        )

    def test_candidate_verification_does_not_require_whole_product_pass(self):
        quality = _quality_rows()
        scoped = ff._publication_review_quality_gates(quality, quality)
        self.assertTrue(scoped["passed"])

    def test_empty_competitor_result_vacuously_passes_fit_risk_review(self):
        result = product_invariants.evaluate_product_invariants(
            purpose_enabled=False,
            purpose_contract={},
            purpose_confidence=None,
            purpose_before=None,
            purpose_after=None,
            purpose_errors=[],
            competitors_enabled=True,
            competitor_research={"competitors": []},
            competitor_target=5,
            applied_files=[],
            test_files=[],
            verification_passed=True,
            license_compatible=lambda _license: True,
        )
        fit = next(
            gate for gate in result["gates"]
            if gate["id"] == "competitive-fit-risk-reviewed"
        )
        self.assertTrue(fit["passed"])

    def test_final_review_can_cover_every_unpublished_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            _run("git", "init", "-q", "-b", "main", str(repo))
            _run("git", "config", "user.name", "FlexFactor Test", cwd=repo)
            _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
            (repo / "first.txt").write_text("baseline\n", encoding="utf-8")
            _run("git", "add", ".", cwd=repo)
            _run("git", "commit", "-qm", "baseline", cwd=repo)
            baseline = _run("git", "rev-parse", "HEAD", cwd=repo)
            (repo / "first.txt").write_text("checkpoint one\n", encoding="utf-8")
            _run("git", "commit", "-qam", "checkpoint one", cwd=repo)
            (repo / "second.txt").write_text("checkpoint two\n", encoding="utf-8")
            _run("git", "add", ".", cwd=repo)
            _run("git", "commit", "-qm", "checkpoint two", cwd=repo)
            final = _run("git", "rev-parse", "HEAD", cwd=repo)
            prompts = []

            def approve(_provider, _system, prompt, _schema, max_tokens=8000):
                prompts.append(prompt)
                return {"verdict": "approve", "commit": final, "findings": [],
                        "evidence_consistent": True, "reason": "complete"}

            reviewer = types.SimpleNamespace(model="test-reviewer")
            with mock.patch.object(ff, "_judge", side_effect=approve):
                review = ff._independent_final_review(
                    reviewer, str(repo), baseline, final,
                    # Current main refuses an exact-head review that is not
                    # bound to a complete authorizing purpose contract. This
                    # test is about the review RANGE, so supply the minimum
                    # authority the reviewer now requires and keep asserting
                    # the thing it was written to assert.
                    {"purpose_contract": {
                        "name": "Fixture", "confidence": "owner-authored"},
                     "purpose_confidence": "owner-authored"},
                )
            self.assertEqual("approve", review["verdict"])
            reviewed = "\n".join(prompts)
            self.assertIn("checkpoint one", reviewed)
            self.assertIn("checkpoint two", reviewed)

    def test_every_candidate_safety_gate_is_fail_closed(self):
        cases = [
            (_quality_rows({"tests": "fail"}), _product_rows(), "tests"),
            (_quality_rows({"secrets": "blocked"}), _product_rows(), "secrets"),
            (_quality_rows(), _product_rows({
                "no-blind-competitor-copying": "fail"
            }), "no-blind-competitor-copying"),
            (_quality_rows(), {"gates": []}, "selected-capabilities-delivered"),
        ]
        for quality, product, expected in cases:
            with self.subTest(expected=expected):
                ready, reason = ff._publication_safety_ready(quality, product)
                self.assertFalse(ready)
                self.assertIn(expected, reason)

    def test_audit_finalization_uses_candidate_safety_not_full_convergence(self):
        source = inspect.getsource(ff.audit_one_program)
        finalization = source.split("# FINAL PUBLICATION GATE.", 1)[1]
        finalization = finalization.split("if evidence is not None:", 1)[0]
        self.assertIn("_publication_safety_ready(", finalization)
        self.assertIn("prepublication_complete = publication_safe", finalization)
        self.assertNotIn("prepublication_complete = bool(\n            converged",
                         finalization)

    def test_exact_verified_commit_is_really_pushed_to_remote_main(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin.git"
            target = root / "target"
            _run("git", "init", "--bare", "-q", "-b", "main", str(origin))
            _run("git", "init", "-q", "-b", "main", str(target))
            _run("git", "config", "user.name", "FlexFactor Test", cwd=target)
            _run("git", "config", "user.email", "test@example.invalid", cwd=target)
            (target / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            _run("git", "add", "app.py", cwd=target)
            _run("git", "commit", "-qm", "baseline", cwd=target)
            _run("git", "remote", "add", "origin", str(origin), cwd=target)
            _run("git", "push", "-q", "-u", "origin", "main", cwd=target)

            (target / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            _run("git", "add", "app.py", cwd=target)
            _run("git", "commit", "-qm", "verified repair", cwd=target)
            candidate = _run("git", "rev-parse", "HEAD", cwd=target)

            args = types.SimpleNamespace(push=True, merge=True)
            with mock.patch.object(
                ff, "_publication_gate", return_value=(True, "green")
            ), mock.patch.object(
                ff, "_wip_publish_guard", return_value=(True, "")
            ):
                result = ff._publish_verified_head(
                    str(target), "main", args, {}, candidate
                )

            self.assertTrue(result["complete"], result)
            self.assertEqual("main", result["default_branch"])
            self.assertEqual(
                candidate,
                _run("git", "--git-dir", str(origin), "rev-parse", "main"),
            )



def _fake_evidence_module(gate_states: dict[str, str]) -> types.SimpleNamespace:
    """A deterministic stand-in for `flexfactor_evidence` on the boundary tree.

    The boundary evaluation is what is under test, not the evidence runtime, so
    the module returns the exact gate states the boundary is supposed to have.
    """
    seen: dict[str, object] = {}

    def build_repository_index(root, run_id, progress=None):
        seen["index_root"] = root
        return {"root": root, "run_id": run_id, "totals": {}}

    def coverage_ledger(index, **kwargs):
        return {"functions": [], "function_total": 0}

    def changed_file_rescan(index, changed):
        return {"complete": True, "changed": list(changed)}

    def dependency_blast_radius(index, changed):
        return {"ran": True, "unresolved_local_imports": []}

    def secret_findings(root, index):
        return []

    def quality_gates(**kwargs):
        rows = [{"id": gate_id, "name": gate_id, "category": "quality",
                 "ran": True, "passed": status == "pass", "status": status,
                 "evidence": {}}
                for gate_id, status in gate_states.items()]
        return {"gates": rows}

    return types.SimpleNamespace(
        build_repository_index=build_repository_index,
        coverage_ledger=coverage_ledger,
        changed_file_rescan=changed_file_rescan,
        dependency_blast_radius=dependency_blast_radius,
        secret_findings=secret_findings,
        quality_gates=quality_gates,
        seen=seen,
    )


def _coverage_run(*, parsed: bool) -> dict:
    return {
        "rows": [],
        "blocked": {},
        "blocked_rejected": [],
        "meta": {"available": parsed,
                 "artifacts": [{"path": "coverage.xml", "parsed": parsed}]},
    }


class PublicationBoundaryBaselineTests(unittest.TestCase):
    """#156: the review baseline is MEASURED on origin, not synthesised.

    `audit_one_program` used to build the comparison baseline as a single
    synthetic `build` row, so `function-coverage` and `behavior` had no baseline
    state, could never be proven legacy, and the certifier was told to reject a
    gate that was already red on the default branch before the run started.
    """

    def _repo(self, root: Path) -> tuple[Path, str]:
        origin = root / "origin.git"
        target = root / "target"
        _run("git", "init", "--bare", "-q", "-b", "main", str(origin))
        _run("git", "init", "-q", "-b", "main", str(target))
        _run("git", "config", "user.name", "FlexFactor Test", cwd=target)
        _run("git", "config", "user.email", "test@example.invalid", cwd=target)
        (target / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _run("git", "add", "app.py", cwd=target)
        _run("git", "commit", "-qm", "boundary", cwd=target)
        _run("git", "remote", "add", "origin", str(origin), cwd=target)
        _run("git", "push", "-q", "-u", "origin", "main", cwd=target)
        boundary = _run("git", "rev-parse", "HEAD", cwd=target)
        (target / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        _run("git", "add", "app.py", cwd=target)
        _run("git", "commit", "-qm", "candidate", cwd=target)
        return target, boundary

    def _boundary(self, target: Path, boundary: str, *, boundary_states: dict,
                  candidate_gates: dict, parsed_coverage: bool = True,
                  candidate_e2e_ran: bool = False) -> dict:
        module = _fake_evidence_module(boundary_states)
        with mock.patch.object(
            ff, "_direct_coverage_evidence",
            return_value=_coverage_run(parsed=parsed_coverage),
        ):
            return ff._boundary_completeness_gates(
                str(target), boundary, {}, module, "run-1", candidate_gates,
                baseline_ok=None, baseline_measured_boundary=False,
                candidate_e2e_ran=candidate_e2e_ran)

    def test_a_gate_already_red_on_the_boundary_does_not_block(self):
        # THE BUG. `function-coverage` was red on origin/main before this run
        # existed. Publication review must be able to prove that and scope it
        # out, instead of rejecting an otherwise safe candidate for it.
        with tempfile.TemporaryDirectory() as temporary:
            target, boundary = self._repo(Path(temporary))
            # Only function-coverage is red; every other row is green so the
            # scope reflects exactly the gate under test.
            candidate = _quality_rows({"function-coverage": "fail",
                                       "behavior": "pass", "build": "pass"})
            baseline = self._boundary(
                target, boundary,
                boundary_states={"function-coverage": "fail"},
                candidate_gates=candidate)
            self.assertEqual(
                {"function-coverage"},
                {row["id"] for row in baseline["gates"]},
                baseline,
            )
            scoped = ff._publication_review_quality_gates(candidate, baseline)
            self.assertNotIn(
                "function-coverage", {row["id"] for row in scoped["gates"]})
            self.assertTrue(scoped["passed"], scoped)
            # And the shape this replaced: a synthetic one-row `build`
            # baseline could not exonerate ANY other completeness gate, so the
            # certifier was told to reject the same safe candidate.
            synthetic = ff._publication_review_quality_gates(
                candidate, {"gates": [{"id": "build", "status": "pass"}]})
            self.assertIn(
                "function-coverage", {row["id"] for row in synthetic["gates"]})
            self.assertFalse(synthetic["passed"], synthetic)

    def test_a_gate_this_run_turned_red_still_blocks(self):
        # THE OTHER DIRECTION, and the one an earlier autonomous "fix" broke:
        # a candidate that deletes coverage must not be exonerated.
        with tempfile.TemporaryDirectory() as temporary:
            target, boundary = self._repo(Path(temporary))
            candidate = _quality_rows({"function-coverage": "fail",
                                       "behavior": "pass", "build": "pass"})
            baseline = self._boundary(
                target, boundary,
                boundary_states={"function-coverage": "pass"},
                candidate_gates=candidate)
            scoped = ff._publication_review_quality_gates(candidate, baseline)
            row = next(g for g in scoped["gates"]
                       if g["id"] == "function-coverage")
            self.assertEqual("fail", row["status"])
            self.assertFalse(scoped["passed"], scoped)

    def test_an_unmeasurable_boundary_keeps_the_row_in_review(self):
        # Fail-closed. A boundary that cannot be checked out yields NO rows,
        # and no row means the candidate's row is retained - never dropped for
        # want of evidence.
        with tempfile.TemporaryDirectory() as temporary:
            target, _boundary = self._repo(Path(temporary))
            candidate = _quality_rows({"function-coverage": "fail"})
            baseline = self._boundary(
                target, "0" * 40,
                boundary_states={"function-coverage": "fail"},
                candidate_gates=candidate)
            self.assertEqual([], baseline["gates"])
            scoped = ff._publication_review_quality_gates(candidate, baseline)
            self.assertIn(
                "function-coverage", {row["id"] for row in scoped["gates"]})
            self.assertFalse(scoped["passed"], scoped)

    def test_boundary_coverage_without_a_parsed_artifact_is_not_evidence(self):
        # A fresh worktree has no installed dependencies, so a coverage tool
        # that could not run reports "nothing is covered". Trusting that would
        # fabricate a red baseline and exonerate a real regression.
        with tempfile.TemporaryDirectory() as temporary:
            target, boundary = self._repo(Path(temporary))
            candidate = _quality_rows({"function-coverage": "fail"})
            baseline = self._boundary(
                target, boundary,
                boundary_states={"function-coverage": "fail"},
                candidate_gates=candidate, parsed_coverage=False)
            self.assertEqual([], baseline["gates"])
            scoped = ff._publication_review_quality_gates(candidate, baseline)
            self.assertFalse(scoped["passed"], scoped)

    def test_behavior_is_only_compared_when_no_journeys_ran(self):
        # `behavior` depends on a browser run that cannot be replayed against
        # the boundary. Comparing it is faithful only when this run executed no
        # journeys either, so the run-level input is identical on both sides.
        with tempfile.TemporaryDirectory() as temporary:
            target, boundary = self._repo(Path(temporary))
            candidate = _quality_rows({"behavior": "blocked"})
            offline = self._boundary(
                target, boundary, boundary_states={"behavior": "blocked"},
                candidate_gates=candidate)
            self.assertEqual(
                {"behavior"}, {row["id"] for row in offline["gates"]})
            live = self._boundary(
                target, boundary, boundary_states={"behavior": "blocked"},
                candidate_gates=candidate, candidate_e2e_ran=True)
            self.assertEqual([], live["gates"])

    def test_a_green_candidate_row_costs_no_boundary_checkout(self):
        # The extra evaluation is spent only when it can change the outcome.
        with tempfile.TemporaryDirectory() as temporary:
            target, boundary = self._repo(Path(temporary))
            module = _fake_evidence_module({"function-coverage": "pass"})
            with mock.patch.object(
                ff, "_direct_coverage_evidence",
                side_effect=AssertionError("boundary coverage must not run"),
            ):
                baseline = ff._boundary_completeness_gates(
                    str(target), boundary, {}, module, "run-1",
                    _quality_rows({"function-coverage": "pass",
                                   "behavior": "pass"}),
                    baseline_ok=None, baseline_measured_boundary=False,
                    candidate_e2e_ran=False)
            self.assertEqual([], baseline["gates"])
            self.assertNotIn("index_root", module.seen)

    def test_boundary_run_trust_mirrors_the_target_and_is_revoked(self):
        # The boundary tree is the SAME repository at an ancestor commit, in a
        # temporary path no `trusted_repos` rule names. Without propagating the
        # target's own authorization the boundary coverage run is refused as
        # untrusted third-party code and this gate can never produce evidence.
        # The grant must MIRROR the target - never create trust - and must not
        # outlive the evaluation.
        with tempfile.TemporaryDirectory() as temporary:
            target, boundary = self._repo(Path(temporary))
            candidate = _quality_rows({"function-coverage": "fail"})
            seen: dict[str, object] = {}

            def record(project_dir, stack, index, pfx=""):
                seen["allowed"] = ff._run_trust_allowed(project_dir)
                seen["cwd"] = project_dir
                return _coverage_run(parsed=True)

            module = _fake_evidence_module({"function-coverage": "fail"})
            with mock.patch.object(
                ff, "_execution_authorization",
                return_value=({"basis": "trusted-repo"}, ""),
            ), mock.patch.object(ff, "_direct_coverage_evidence", record):
                ff._boundary_completeness_gates(
                    str(target), boundary, {}, module, "run-1", candidate,
                    baseline_ok=None, baseline_measured_boundary=False,
                    candidate_e2e_ran=False)
            self.assertIs(True, seen.get("allowed"))
            self.assertFalse(ff._run_trust_allowed(str(seen["cwd"])))

            # An UNAUTHORIZED target grants nothing.
            seen.clear()
            with mock.patch.object(
                ff, "_execution_authorization",
                return_value=(None, "not a trusted repository"),
            ), mock.patch.object(ff, "_direct_coverage_evidence", record):
                ff._boundary_completeness_gates(
                    str(target), boundary, {}, module, "run-1", candidate,
                    baseline_ok=None, baseline_measured_boundary=False,
                    candidate_e2e_ran=False)
            self.assertIs(False, seen.get("allowed"))

    def test_the_build_row_is_only_claimed_for_the_boundary_commit(self):
        # `baseline_ok` measures the PRE-MUTATION head. It describes origin's
        # tree only when the run started there; otherwise it says nothing about
        # the boundary and must not be presented as its state.
        module = _fake_evidence_module({})
        at_boundary = ff._boundary_completeness_gates(
            "/nonexistent", "abc123", {}, module, "run-1",
            _quality_rows({"function-coverage": "pass", "behavior": "pass"}),
            baseline_ok=False, baseline_measured_boundary=True,
            candidate_e2e_ran=False)
        self.assertEqual(
            [("build", "fail")],
            [(row["id"], row["status"]) for row in at_boundary["gates"]])
        ahead = ff._boundary_completeness_gates(
            "/nonexistent", "abc123", {}, module, "run-1",
            _quality_rows({"function-coverage": "pass", "behavior": "pass"}),
            baseline_ok=False, baseline_measured_boundary=False,
            candidate_e2e_ran=False)
        self.assertEqual([], ahead["gates"])

    def test_review_baseline_is_measured_not_synthesised(self):
        source = inspect.getsource(ff.audit_one_program)
        summary = source.split(
            '"review_scope": "candidate-publication-safety"', 1)[1]
        summary = summary.split("changed_file_rescan", 1)[0]
        self.assertIn("_boundary_completeness_gates(", source)
        self.assertIn("publication_baseline_gates", summary)
        # The synthetic single-row baseline is gone.
        self.assertNotIn('{"gates": [{', summary)



if __name__ == "__main__":
    unittest.main()
