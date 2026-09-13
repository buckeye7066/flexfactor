"""Offline final-review call-site and publication independence regressions."""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flexfactor_tests as support  # installs isolated state and HTTP guards
import flexfactor_rotation as rotation
import flexfactor_runstate as runstate

ff = support.ff


class FinalReviewIndependenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        support._init_test_origin(str(self.project), str(self.root / "remote.git"))
        self.baseline = self.git("rev-parse", "HEAD")
        self.calls = []
        self.selections = []
        self.counter = 0
        contract, confidence, *_ = support._unit_purpose_understanding()
        self.evidence = {"purpose_contract": contract.to_dict(),
                         "purpose_confidence": confidence}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.project), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def provider(self, models, coordinator=None):
        self.counter += 1
        routes = [rotation.Route(
            id=f"fixture/{model}", backend="fixture", backend_label="fixture",
            model=model, wire_model=model, api="openai", base_url="https://example.invalid",
            pool=model, cost_class=rotation.LOCAL_UNLIMITED, tier=rotation.FRONTIER,
        ) for model in models]
        outer = self

        class Transport:
            def __init__(self, route):
                self.model = route.model
                self.meter = None

            def complete(self, *_args, **_kwargs):
                outer.calls.append((self.model, "author"))
                return "VALUE = 2\n"

            def structured(self, system, prompt, schema, **kwargs):
                if schema is ff.FINAL_REVIEW_SCHEMA:
                    outer.calls.append((self.model, "final-review"))
                    return {"commit": re.search(r"EXPECTED FINAL COMMIT: (\w+)", prompt)[1],
                            "verdict": "approve", "findings": [],
                            "evidence_consistent": True, "reason": "reviewed exact patch"}
                outer.calls.append((self.model, "author"))
                return {"changed": True, "edits": [{"search": "VALUE = 1", "replace": "VALUE = 2"}],
                        "fixed_titles": ["incorrect value"], "notes": ""}

        rotator = rotation.Rotator(
            catalog=rotation.Catalog(routes, "fixture", 0, "fixture"),
            store=rotation.StateStore(str(self.root / f"rotation-{self.counter}.json")),
            app="flexfactor",
        )
        original_next = rotator.next_route

        def select(*args, **kwargs):
            result = original_next(*args, **kwargs)
            self.selections.append((result.route.model, kwargs.get("intent")))
            return result

        rotator.next_route = select
        return rotation.RotatingProvider(
            rotator, Transport, tier=rotation.FRONTIER, judge_tier=rotation.FRONTIER,
            role_coordinator=coordinator,
        )

    def commit_candidate(self, source="VALUE = 2\n"):
        (self.project / "app.py").write_text(source, encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-qm", "candidate")
        return self.git("rev-parse", "HEAD")

    def review(self, provider, final):
        return ff._independent_final_review(provider, str(self.project),
                                            self.baseline, final, self.evidence)

    def checkpoint(self, provider, source, checkpoint=None):
        ff._bind_candidate_authorship(provider, str(self.project), checkpoint)
        (self.project / "app.py").write_text(source, encoding="utf-8")
        args = SimpleNamespace(_candidate_author_provider=provider, push=True)
        with mock.patch.object(ff, "_publication_gate", return_value=(True, "fixture passed")):
            status = ff._commit_and_sync(str(self.project), "main", "main", args,
                                         "independence fixture", {})
        self.assertIn("committed", status)
        return self.git("rev-parse", "HEAD")

    def test_final_schema_excludes_all_authors_across_provider_instances(self):
        coordinator = rotation.RoleCoordinator()
        first = self.provider(["gpt-5.6"], coordinator)
        second = self.provider(["claude-opus-4.6"], coordinator)
        first.complete("author the first part")
        ff.generate_file_fix_edits(second, "app.py", "VALUE = 1\n", [])
        reviewer = self.provider(["gpt-5.6", "claude-opus-4.6", "qwen3-coder"], coordinator)
        ff._judge(reviewer, ff.FINAL_REVIEW_SYSTEM,
                  "EXPECTED FINAL COMMIT: abc123", ff.FINAL_REVIEW_SCHEMA)
        model, intent = self.selections[-1]
        self.assertEqual("qwen3-coder", model)
        self.assertTrue({"openai", "anthropic"}.issubset(intent.avoid_families))

    def test_same_family_final_review_is_unavailable(self):
        provider = self.provider(["gpt-5.6"])
        final = self.checkpoint(provider, provider.complete("author"))
        result = self.review(provider, final)
        self.assertEqual("reject", result["verdict"])
        self.assertIn("ledger incomplete", result["reason"])
        self.assertNotIn(("gpt-5.6", "final-review"), self.calls)

    def test_resumed_candidate_without_author_receipt_cannot_be_approved(self):
        prior = self.provider(["gpt-5.6"])
        final = self.commit_candidate(prior.complete("author before process exit"))
        resumed = self.provider(["gpt-5.6"])
        result = self.review(resumed, final)
        self.assertEqual("reject", result["verdict"])
        self.assertNotIn(("gpt-5.6", "final-review"), self.calls)

    def test_new_author_does_not_hide_untracked_prior_candidate_bytes(self):
        prior = self.provider(["gpt-5.6"])
        self.commit_candidate(prior.complete("author before process exit"))
        resumed = self.provider(["claude-opus-4.6"])
        resumed.complete("author a second part")
        final = self.commit_candidate("VALUE = 3\n")
        reviewer = self.provider(["gpt-5.6"], resumed.role_coordinator)
        self.assertEqual("reject", self.review(reviewer, final)["verdict"])

    def test_restart_restores_all_committed_authors_before_selecting_review(self):
        checkpoint = runstate.new_run(str(self.root / "runs"), program="fixture",
                                     project_dir=str(self.project), mode="audit",
                                     policy="fixture", tool="fixture")
        first = self.provider(["gpt-5.6"])
        self.checkpoint(first, first.complete("first part"), checkpoint)
        second = self.provider(["claude-opus-4.6"], first.role_coordinator)
        second.complete("second part")
        final = self.checkpoint(second, "VALUE = 3\n")
        saved = json.loads(Path(checkpoint.path).read_text(encoding="utf-8"))
        restored = runstate.RunCheckpoint(str(self.root / "runs"), saved)
        reviewer = self.provider(["gpt-5.6", "claude-opus-4.6", "qwen3-coder"])
        ff._bind_candidate_authorship(reviewer, str(self.project), restored)
        result = self.review(reviewer, final)
        self.assertEqual("approve", result["verdict"], result["reason"])
        self.assertEqual([("qwen3-coder", "final-review")],
                         [call for call in self.calls if call[1] == "final-review"])
        self.assertTrue({"openai", "anthropic"}.issubset(
            self.selections[-1][1].avoid_families))

    def test_checkpoint_receipt_cannot_cover_an_unrecorded_earlier_commit(self):
        self.commit_candidate("VALUE = 2\n")
        author = self.provider(["claude-opus-4.6"])
        author.complete("last part")
        final = self.checkpoint(author, "VALUE = 3\n")
        reviewer = self.provider(["qwen3-coder"], author.role_coordinator)
        result = self.review(reviewer, final)
        self.assertEqual("reject", result["verdict"])
        self.assertIn("missing for commit", result["reason"])

    def test_restart_preserves_opaque_authors_alongside_known_families(self):
        checkpoint = runstate.new_run(str(self.root / "runs"), program="fixture",
                                     project_dir=str(self.project), mode="audit",
                                     policy="fixture", tool="fixture")
        known = self.provider(["gpt-5.6"])
        self.checkpoint(known, known.complete("known author"), checkpoint)
        opaque = self.provider(["auto"], known.role_coordinator)
        opaque.complete("opaque author")
        final = self.checkpoint(opaque, "VALUE = 3\n")
        saved = json.loads(Path(checkpoint.path).read_text(encoding="utf-8"))
        self.assertEqual({"openai", "unknown"},
                         set(saved["candidate_author_families"][final]))
        restored = runstate.RunCheckpoint(str(self.root / "runs"), saved)
        reviewer = self.provider(["qwen3-coder"])
        ff._bind_candidate_authorship(reviewer, str(self.project), restored)
        result = self.review(reviewer, final)
        self.assertEqual("reject", result["verdict"])
        self.assertIn("unknown", reviewer.role_coordinator.author_families)
        self.assertEqual([], [call for call in self.calls if call[1] == "final-review"])

    def test_changed_sha_cannot_reuse_author_receipt(self):
        author = self.provider(["gpt-5.6"])
        self.checkpoint(author, author.complete("candidate"))
        final = self.commit_candidate("VALUE = 3\n")
        reviewer = self.provider(["qwen3-coder"], author.role_coordinator)
        result = self.review(reviewer, final)
        self.assertEqual("reject", result["verdict"])
        self.assertIn(final, result["reason"])

    def test_unpublished_start_commit_cannot_hide_earlier_authors(self):
        self.baseline = self.commit_candidate("VALUE = 2\n")
        author = self.provider(["claude-opus-4.6"])
        author.complete("last part")
        final = self.checkpoint(author, "VALUE = 3\n")
        reviewer = self.provider(["gpt-5.6"], author.role_coordinator)
        result = self.review(reviewer, final)
        self.assertEqual("reject", result["verdict"])
        self.assertIn("unpublished", result["reason"])

    def test_failed_receipt_write_blocks_later_final_review(self):
        checkpoint = runstate.new_run(str(self.root / "runs"), program="fixture",
                                     project_dir=str(self.project), mode="audit",
                                     policy="fixture", tool="fixture")
        author = self.provider(["gpt-5.6"])
        source = author.complete("candidate")
        with mock.patch.object(checkpoint, "save", return_value=False):
            with self.assertRaisesRegex(ff.BranchStateError, "author history"):
                self.checkpoint(author, source, checkpoint)
        reviewer = self.provider(["qwen3-coder"], author.role_coordinator)
        self.assertEqual("reject", self.review(reviewer, self.git("rev-parse", "HEAD"))["verdict"])

    def test_scout_rolls_back_before_publication_when_author_is_untracked(self):
        reviewer = self.provider(["gpt-5.6"])
        opts = SimpleNamespace(
            allow_dirty=False, verify=True, push=True, merge=True,
            final_reviewer=reviewer, isolate_verify=True,
            purpose_contract_for_review=self.evidence["purpose_contract"],
            purpose_confidence_for_review=self.evidence["purpose_confidence"],
        )
        patch = {"files": [{"path": "app.py", "contents": "VALUE = 2\n"}],
                 "packages": [], "commit_message": "candidate"}
        original_run = ff._run

        def run(command, *args, **kwargs):
            if command == ["fixture-test"]:
                return subprocess.CompletedProcess(command, 0, "passed", "")
            return original_run(command, *args, **kwargs)

        with mock.patch.object(ff, "_detect_verify", return_value=(False, [["fixture-test"]])), \
                mock.patch.object(ff, "_run", side_effect=run), \
                mock.patch.object(ff, "_publish_verified_head", return_value={"complete": True}) as publish, \
                contextlib.redirect_stdout(io.StringIO()):
            result = ff.apply_integration(str(self.project), "fixture/library", patch, opts)
        self.assertEqual("review-rejected", result.status, result.detail)
        publish.assert_not_called()
        self.assertEqual(self.baseline, self.git("rev-parse", "HEAD"))
        self.assertEqual("VALUE = 1\n", (self.project / "app.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
