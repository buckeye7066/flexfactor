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


if __name__ == "__main__":
    unittest.main()
