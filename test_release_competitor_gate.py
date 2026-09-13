"""Offline regressions for mandatory inter-pass research failure."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import flexfactor_tests as fixtures
import flexfactor_execution as execution
import flexfactor_competitors as competitors

ff = fixtures.ff


class CompetitorGateFailureTests(unittest.TestCase):
    def _gate(self, failure=None, *, real_module=False):
        research = {"competitors": [], "sources_skipped": {}, "verified": 0,
                    "coverage_note": "SHORTFALL: no corroborated competitors"}
        module = SimpleNamespace(
            _ascii=str,
            research_competitors=mock.Mock(return_value=research),
            competitor_findings=lambda *_args, **_kwargs: [],
        )
        if real_module:
            module = competitors
        if failure == "missing":
            module = None
        elif failure is not None:
            module.research_competitors.side_effect = failure
        with mock.patch.object(ff, "_competitors_module", return_value=module), \
             mock.patch.object(ff, "resolve_repo_rewards_url", return_value=(None, "offline")), \
             mock.patch.object(ff, "_scout_program_profile", return_value=({}, "")), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            outcome = ff._run_top_competitor_gate(
                args=SimpleNamespace(competitor_count=7, competitor_fixes=7),
                pfx="", report=lambda **_kw: None, checkpoint=None,
                display_name="fixture", purpose_blob="fixture purpose", stack={},
                purpose_reviewer=object(), author=object(), cross=object(),
                project_dir=tempfile.gettempdir(), all_files=[], meter=None,
                baseline_ok=True, oversized=[], noop_stats={}, errors_total=0,
                done_set=set(), total_to_review=0, git=False, branch="main",
                prev_branch="main", purpose_contract=None,
            )
        return outcome, module

    def _record(self, tmp, outcome, changed):
        coordinator = execution.SequentialOrchestrator(
            "audit", ["fixture"], state_path=str(Path(tmp) / "queue.json"),
            queue_id="competitor-failure",
        )
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["app.py"], whole_repository=True)
        coordinator.finish_pass(1, changed, reviewed_files=["app.py"])
        coordinator.record_competitor_gate(
            attempted=outcome["attempted"], implemented_files=outcome["applied"],
            verified=int((outcome["research"] or {}).get("verified") or 0),
            note="; ".join(outcome["notes"]),
        )
        return coordinator

    def test_missing_module_budget_and_crash_cannot_authorize_pass_two(self):
        for failure in ("missing", ff.BudgetExceededError("offline cost cap"),
                        RuntimeError("offline research fault")):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as tmp:
                outcome, _ = self._gate(failure)
                coordinator = self._record(tmp, outcome, ["app.py"])
                with self.assertRaises(execution.OrchestrationOrderError):
                    coordinator.begin_pass(2, ["app.py"])

    def test_missing_module_budget_and_crash_cannot_be_success(self):
        for failure in ("missing", ff.BudgetExceededError("offline cost cap"),
                        RuntimeError("offline research fault")):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as tmp:
                outcome, _ = self._gate(failure)
                coordinator = self._record(tmp, outcome, [])
                coordinator.record_finalization(
                    changed_files=[], final_commit=None, quality_gates_passed=True,
                    publication_required=False, publication_complete=True,
                )
                self.assertNotEqual(0, coordinator.finish_target(0, 0))

    def test_completed_research_preserves_configured_count_and_honest_shortfall(self):
        outcome, module = self._gate()
        self.assertTrue(outcome["attempted"])
        self.assertEqual(7, module.research_competitors.call_args.kwargs["target"])
        self.assertEqual(0, outcome["research"]["verified"])
        self.assertIn("SHORTFALL", outcome["research"]["coverage_note"])


    def test_real_research_swallowed_budget_fault_stays_incomplete(self):
        with mock.patch.object(ff, "_judge", side_effect=ff.BudgetExceededError("offline cost cap")), \
             mock.patch.object(competitors, "web_search", return_value=([], "", {})):
            outcome, _ = self._gate(real_module=True)
        self.assertIn("model-discovery", outcome["research"]["sources_skipped"])
        self.assertFalse(outcome["attempted"])

    def test_refactor_missing_module_budget_and_crash_are_incomplete(self):
        for failure in ("missing", ff.BudgetExceededError("offline cost cap"),
                        RuntimeError("offline research fault")):
            with self.subTest(failure=str(failure)):
                module = None if failure == "missing" else SimpleNamespace(
                    research_competitors=mock.Mock(side_effect=failure))
                with mock.patch.object(ff, "_competitors_module", return_value=module), \
                     mock.patch.object(ff, "resolve_repo_rewards_url", return_value=(None, "offline")), \
                     mock.patch.object(ff, "_scout_program_profile", return_value=({}, "")), \
                     mock.patch.object(ff, "_tracked_repository_scope", return_value=[]):
                    outcome = ff._refactor_top_three_gate(
                        SimpleNamespace(goal="keep correct", competitor_count=7),
                        object(), tempfile.gettempdir(), "app.py", "VALUE = 1\n", {}, "main")
                self.assertFalse(outcome["attempted"])

    def test_noop_refactor_cannot_succeed_when_required_research_is_unavailable(self):
        original = "VALUE = 1\n"
        provider = SimpleNamespace(
            complete=lambda *_a: original,
            grade_independent=lambda *_a: json.dumps({
                "grade": 100, "meets_goal": True, "rationale": "valid", "issues": []}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            source = repo / "app.py"
            source.write_text(original, encoding="utf-8")
            fixtures._init_test_origin(str(repo), str(Path(tmp) / "origin.git"))
            args = SimpleNamespace(file=str(source), goal="keep correct", threshold=90,
                                   max_iterations=1, max_cost=1, push=True, merge=True)
            with mock.patch.object(ff, "_ensure_program_understanding",
                                   return_value=fixtures._unit_purpose_understanding()), \
                 mock.patch.object(ff, "_best_available_provider", return_value=provider), \
                 mock.patch.object(ff, "_publication_gate", return_value=(True, "ok")), \
                 mock.patch.object(ff, "_competitors_module", return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(0, ff.run(args))
            self.assertEqual(original, source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
