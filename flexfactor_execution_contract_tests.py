"""Offline tests for the one-queue FlexFactor execution contract."""

from __future__ import annotations

import unittest
import tempfile
import os

import flexfactor_execution as execution


class QueueContractTests(unittest.TestCase):
    def test_thirty_targets_are_accepted_in_input_order(self):
        targets = [f"program-{index}" for index in range(30)]
        self.assertEqual(execution.target_queue(targets), targets)

    def test_thirty_one_targets_are_refused(self):
        with self.assertRaisesRegex(execution.ExecutionContractError, "no more than 30"):
            execution.target_queue([f"program-{index}" for index in range(31)])

    def test_empty_and_blank_targets_are_refused(self):
        with self.assertRaises(execution.ExecutionContractError):
            execution.target_queue([])
        with self.assertRaises(execution.ExecutionContractError):
            execution.target_queue(["repo", "  "])


class PassContractTests(unittest.TestCase):
    def test_six_is_the_hard_pass_ceiling(self):
        self.assertEqual(execution.pass_count(6), 6)
        for invalid in (0, 7, "many"):
            with self.assertRaises(execution.ExecutionContractError):
                execution.pass_count(invalid)

    def test_follow_up_scope_contains_only_changed_files(self):
        self.assertEqual(
            execution.changed_file_scope(
                ["src\\a.py", "src/b.py", "src/a.py", "", "src/c.py"]
            ),
            ["src/a.py", "src/b.py", "src/c.py"],
        )

    def test_whole_repository_scope_is_also_normalized(self):
        root = tempfile.mkdtemp(prefix="ff-scope-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        coordinator = execution.SequentialOrchestrator(
            "audit", ["repo"], state_path=os.path.join(root, "queue.json"),
            queue_id="normalized-scope",
        )
        coordinator.start_target(0)
        self.assertEqual(
            coordinator.begin_pass(
                1, [" src\\a.py ", "src/a.py", "", "src/b.py"],
                whole_repository=True,
            ),
            ["src/a.py", "src/b.py"],
        )


class ProductDefaultsTests(unittest.TestCase):
    def test_fixed_product_constants(self):
        self.assertEqual(execution.MAX_TARGETS, 30)
        self.assertEqual(execution.MAX_PASSES, 6)
        self.assertEqual(execution.TOP_COMPETITORS, 3)
        self.assertEqual(execution.MODEL_POLICY, "best-available")


class OrchestratorTests(unittest.TestCase):
    def _coordinator(self, targets=("a", "b")):
        root = tempfile.mkdtemp(prefix="ff-orchestrator-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        return execution.SequentialOrchestrator(
            "audit", targets, state_path=os.path.join(root, "queue.json"),
            queue_id="test-queue"
        )

    @staticmethod
    def _finalize(coordinator, changed_files=()):
        coordinator.record_finalization(
            changed_files=changed_files,
            final_commit="verified-sha" if changed_files else None,
            quality_gates_passed=True,
            publication_required=bool(changed_files),
            publication_complete=True,
        )

    def test_only_one_target_can_be_active(self):
        coordinator = self._coordinator()
        coordinator.start_target(0)
        with self.assertRaises(execution.OrchestrationOrderError):
            coordinator.start_target(1)

    def test_worker_outcome_note_survives_the_central_queue_runner(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, [], reviewed_files=["a.py"])
        coordinator.record_competitor_gate(
            attempted=False, implemented_files=[], verified=0,
            not_applicable=True,
        )
        self._finalize(coordinator)
        coordinator.note_active_target("verified no-op")
        self.assertEqual(coordinator.finish_target(0, 0), 0)
        self.assertEqual(
            coordinator.snapshot()["items"][0]["note"], "verified no-op")

    def test_refactor_receipt_cannot_claim_a_whole_repository_pass(self):
        root = tempfile.mkdtemp(prefix="ff-refactor-scope-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        coordinator = execution.SequentialOrchestrator(
            "refactor", ["src/a.py"],
            state_path=os.path.join(root, "queue.json"), queue_id="refactor-scope")
        coordinator.start_target(0)
        with self.assertRaisesRegex(execution.OrchestrationOrderError, "selected-file"):
            coordinator.begin_pass(1, ["src/a.py"], whole_repository=True)
        coordinator.begin_pass(
            1, ["src/a.py"], scope_kind="selected-file", exhaustive=False)
        record = coordinator.snapshot()["items"][0]["passes"][0]
        self.assertEqual(record["scope"], "selected-file")
        self.assertFalse(record["exhaustive"])

    def test_scout_receipt_records_understanding_not_a_semantic_audit(self):
        root = tempfile.mkdtemp(prefix="ff-scout-scope-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        coordinator = execution.SequentialOrchestrator(
            "scout", ["repo"], state_path=os.path.join(root, "queue.json"),
            queue_id="scout-scope")
        coordinator.start_target(0)
        coordinator.begin_pass(
            1, ["README.md"], scope_kind="repository-understanding",
            exhaustive=False)
        record = coordinator.snapshot()["items"][0]["passes"][0]
        self.assertEqual(record["scope"], "repository-understanding")
        self.assertFalse(record["exhaustive"])

    def test_pass_two_requires_the_competitor_gate(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py", "b.py"], whole_repository=True)
        coordinator.finish_pass(1, ["a.py"], reviewed_files=["a.py", "b.py"])
        with self.assertRaisesRegex(execution.OrchestrationOrderError, "competitor"):
            coordinator.begin_pass(2, ["a.py"])
        coordinator.record_competitor_gate(
            attempted=True, implemented_files=["b.py"], verified=3
        )
        self.assertEqual(
            coordinator.begin_pass(2, ["a.py", "b.py"]), ["a.py", "b.py"]
        )

    def test_follow_up_pass_cannot_widen_or_drop_the_verified_delta(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py", "b.py"], whole_repository=True)
        coordinator.finish_pass(1, ["a.py"], reviewed_files=["a.py", "b.py"])
        coordinator.record_competitor_gate(
            attempted=True, implemented_files=["b.py"], verified=3
        )
        for wrong in (["a.py"], ["a.py", "b.py", "c.py"], ["b.py", "a.py"]):
            with self.assertRaisesRegex(execution.OrchestrationOrderError, "exactly"):
                coordinator.begin_pass(2, wrong)

    def test_exhaustive_pass_refuses_a_missing_file_review(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py", "README.md"], whole_repository=True)
        with self.assertRaisesRegex(
                execution.OrchestrationOrderError, "lack a completed review"):
            coordinator.finish_pass(
                1, [], reviewed_files=["a.py"],
                incomplete_files=["README.md"])
        record = coordinator.snapshot()["items"][0]["passes"][0]
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["reconciliation"]["missing_review_files"],
                         ["README.md"])

    def test_exhaustive_pass_refuses_an_unattempted_repair(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        with self.assertRaisesRegex(
                execution.OrchestrationOrderError, "not attempted"):
            coordinator.finish_pass(
                1, [], reviewed_files=["a.py"],
                repair_candidate_files=["a.py"], repair_attempted_files=[])

    def test_exhaustive_pass_accepts_reviewed_and_attempted_population(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py", "README.md"], whole_repository=True)
        changed = coordinator.finish_pass(
            1, ["a.py"], reviewed_files=["a.py", "README.md"],
            repair_candidate_files=["a.py"], repair_attempted_files=["a.py"])
        self.assertEqual(changed, ["a.py"])
        record = coordinator.snapshot()["items"][0]["passes"][0]
        self.assertTrue(record["exhaustive"])
        self.assertEqual(record["status"], "completed")

    def test_running_first_pass_can_reconcile_a_post_repair_manifest(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        scope = coordinator.reconcile_first_pass_scope(
            ["a.py", "generated/bridge.py"])
        self.assertEqual(scope, ["a.py", "generated/bridge.py"])
        record = coordinator.snapshot()["items"][0]["passes"][0]
        self.assertEqual(record["files"], scope)
        self.assertIn("scope_reconciled_at", record)
        self.assertEqual(record["scope_revisions"][0]["added_files"],
                         ["generated/bridge.py"])

    def test_finalization_accounts_for_post_pass_mutations(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, ["a.py"], reviewed_files=["a.py"])
        coordinator.record_competitor_gate(
            attempted=True, implemented_files=[], verified=3)
        coordinator.begin_pass(2, ["a.py"])
        coordinator.finish_pass(2, [], reviewed_files=["a.py"])
        self._finalize(coordinator, ["a.py", "requirements.txt"])
        finalization = coordinator.snapshot()["items"][0]["finalization"]
        self.assertEqual(finalization["pass_changed_files"], ["a.py"])
        self.assertEqual(finalization["post_pass_changed_files"],
                         ["requirements.txt"])
        self.assertEqual(coordinator.finish_target(0, 0), 0)

    def test_audit_success_requires_final_tree_reconciliation(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, [], reviewed_files=["a.py"])
        coordinator.record_competitor_gate(
            attempted=False, implemented_files=[], verified=0,
            not_applicable=True)
        coordinator.finish_target(0, 0)
        item = coordinator.snapshot()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("final-tree reconciliation", item["note"])

    def test_scope_reconciliation_is_refused_after_pass_one(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, [], reviewed_files=["a.py"])
        with self.assertRaisesRegex(
                execution.OrchestrationOrderError, "running exhaustive"):
            coordinator.reconcile_first_pass_scope(["a.py", "late.py"])

    def test_success_is_refused_while_a_pass_is_active(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_target(0, 0)
        item = coordinator.snapshot()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertNotEqual(item["exit_code"], 0)

    def test_success_is_refused_when_a_mode_bypasses_the_contract(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.finish_target(0, 0)
        item = coordinator.snapshot()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("no repository pass", item["note"])
        self.assertIn("competitor gate", item["note"])

    def test_queue_exit_code_propagates_contract_refusal(self):
        root = tempfile.mkdtemp(prefix="ff-queue-refusal-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        code, coordinator = execution.run_sequential_queue(
            "scout", ["repo"], lambda *_args: 0,
            state_path=os.path.join(root, "receipt.json"), queue_id="refusal",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(coordinator.snapshot()["status"], "failed")

    def test_success_requires_an_exact_delta_follow_up_after_edits(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, ["a.py"], reviewed_files=["a.py"])
        coordinator.record_competitor_gate(
            attempted=True, implemented_files=[], verified=3
        )
        coordinator.finish_target(0, 0)
        item = coordinator.snapshot()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("exact-delta", item["note"])

    def test_no_delta_can_mark_the_competitor_gate_not_applicable(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, [], reviewed_files=["a.py"])
        coordinator.record_competitor_gate(
            attempted=False, implemented_files=[], verified=0,
            not_applicable=True,
        )
        self._finalize(coordinator)
        self.assertEqual(coordinator.finish_target(0, 0), 0)
        item = coordinator.snapshot()["items"][0]
        self.assertEqual(item["status"], "completed")
        self.assertFalse(item["competitor_gate"]["attempted"])
        self.assertTrue(item["competitor_gate"]["not_applicable"])

    def test_competitor_gate_cannot_be_not_applicable_after_an_edit(self):
        coordinator = self._coordinator(("repo",))
        coordinator.start_target(0)
        coordinator.begin_pass(1, ["a.py"], whole_repository=True)
        coordinator.finish_pass(1, ["a.py"], reviewed_files=["a.py"])
        with self.assertRaisesRegex(
                execution.OrchestrationOrderError, "not applicable"):
            coordinator.record_competitor_gate(
                attempted=False, implemented_files=[], verified=0,
                not_applicable=True,
            )

    def test_interrupted_target_resumes_without_replaying_prior_targets(self):
        root = tempfile.mkdtemp(prefix="ff-queue-resume-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        path = os.path.join(root, "receipt.json")
        first = execution.SequentialOrchestrator(
            "audit", ["one", "two"], state_path=path, queue_id="resume"
        )
        first.start_target(0)
        first.begin_pass(1, ["a.py"], whole_repository=True)

        resumed = execution.SequentialOrchestrator(
            "audit", ["one", "two"], state_path=path, queue_id="resume"
        )
        self.assertEqual(resumed.next_index, 0)
        item = resumed.snapshot()["items"][0]
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["attempts"][0]["status"], "interrupted")
        resumed.start_target(0)
        resumed.begin_pass(1, ["a.py"], whole_repository=True)

    def test_queue_runner_never_overlaps_targets_and_persists_receipt(self):
        root = tempfile.mkdtemp(prefix="ff-queue-run-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        path = os.path.join(root, "receipt.json")
        active = []
        observed = []

        def runner(target, index, total, coordinator):
            self.assertEqual(active, [])
            active.append(target)
            observed.append((target, index, total))
            coordinator.begin_pass(
                1, [target], whole_repository=False,
                scope_kind="repository-understanding", exhaustive=False,
            )
            coordinator.finish_pass(1, [], reviewed_files=[target])
            coordinator.record_competitor_gate(
                attempted=True, implemented_files=[], verified=3
            )
            self._finalize(coordinator)
            active.pop()
            return 0

        code, coordinator = execution.run_sequential_queue(
            "scout", ["one", "two", "three"], runner,
            state_path=path, queue_id="serial"
        )
        self.assertEqual(code, 0)
        self.assertEqual(observed, [("one", 1, 3), ("two", 2, 3), ("three", 3, 3)])
        self.assertEqual(coordinator.snapshot()["status"], "completed")
        self.assertTrue(os.path.isfile(path))

    def test_ordinary_runner_exception_fails_one_target_and_continues(self):
        root = tempfile.mkdtemp(prefix="ff-queue-exception-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        observed = []

        def runner(target, _index, _total, coordinator):
            observed.append(target)
            if target == "one":
                raise RuntimeError("first target broke")
            coordinator.begin_pass(1, ["two.py"], whole_repository=True)
            coordinator.finish_pass(1, [], reviewed_files=["two.py"])
            coordinator.record_competitor_gate(
                attempted=True, implemented_files=[], verified=3
            )
            self._finalize(coordinator)
            return 0

        code, coordinator = execution.run_sequential_queue(
            "audit", ["one", "two"], runner,
            state_path=os.path.join(root, "queue.json"), queue_id="continue",
        )
        snapshot = coordinator.snapshot()
        self.assertNotEqual(code, 0)
        self.assertEqual(observed, ["one", "two"])
        self.assertEqual([item["status"] for item in snapshot["items"]],
                         ["failed", "completed"])
        self.assertIn("RuntimeError: first target broke",
                      snapshot["items"][0]["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
