"""Regression tests for Scout's target-vs-entered-program research path."""
from __future__ import annotations

import argparse
import email.message
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest import mock

import flexfactor as ff
import flexfactor_scout_research as research


def _resolver(_host, port, type=None):  # noqa: A002 - socket API parity
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class ScoutResearchTransportTests(unittest.TestCase):
    def test_ssrf_guard_rejects_private_and_credentialed_urls(self):
        for url in (
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data",
            "http://user:password@example.com/",
        ):
            allowed, reason = research.is_public_http_url(url, resolver=_resolver)
            self.assertFalse(allowed, (url, reason))

    def test_crawler_retrieves_feature_pages_and_assigns_stable_evidence_ids(self):
        pages = {
            "https://example.com": (
                "<html><title>Example Product</title><body>"
                "A real workflow platform for nonprofit teams. " * 5
                + '<a href="/features">Features</a>'
                + '<a href="https://other.example/ignore">Off site</a></body></html>'
            ),
            "https://example.com/features": (
                "<html><title>Features</title><body>Automated intake, evidence-backed "
                "matching, approval routing, status tracking, and exportable audit history. "
                "Each workflow has validation and recovery behavior.</body></html>"
            ),
        }

        def opener(url):
            return url, "text/html", pages[url]

        bundle = research.crawl_public_program(
            "https://example.com", prefix="S", max_pages=4,
            opener=opener, resolver=_resolver)
        self.assertTrue(bundle["coverage"]["complete"], bundle)
        self.assertEqual([row["id"] for row in bundle["evidence"]], ["S1", "S2"])
        self.assertIn("Automated intake", bundle["evidence"][1]["text"])
        self.assertNotIn("other.example", [row["url"] for row in bundle["evidence"]])

    def test_transport_connects_to_the_address_that_passed_the_guard(self):
        calls = []

        def resolver(_host, port, type=None):  # noqa: A002
            calls.append(port)
            if len(calls) > 1:
                return [(2, 1, 6, "", ("127.0.0.1", port))]
            return [(2, 1, 6, "", ("93.184.216.34", port))]

        headers = email.message.Message()
        headers["Content-Type"] = "text/html; charset=utf-8"

        class Response:
            status = 200
            reason = "OK"

            def __init__(self):
                self.headers = headers

            def read(self, _cap):
                return b"<html><body>Observable program behavior.</body></html>"

            def close(self):
                return None

        connected = []

        class Connection:
            def __init__(self, host, port, address, *, timeout):
                connected.append((host, port, address, timeout))

            def request(self, method, target, headers):
                self.request_data = (method, target, headers)

            def getresponse(self):
                return Response()

            def close(self):
                return None

        with mock.patch.object(research, "_PinnedHTTPConnection", Connection):
            final_url, content_type, body = research._default_open(
                "http://example.com/features", resolver=resolver)
        self.assertEqual(calls, [80], "the guarded hostname must be resolved only once")
        self.assertEqual(connected[0][2], "93.184.216.34")
        self.assertEqual(final_url, "http://example.com/features")
        self.assertEqual(content_type, "text/html")
        self.assertIn("Observable program behavior", body)

    def test_comparison_rejects_unknown_or_missing_evidence(self):
        target = {"evidence": [{"id": "T1"}]}
        source = {"evidence": [{"id": "S1"}]}
        valid = {
            "capability": "workflow recovery", "decision": "adapt",
            "source_behavior": "resumes interrupted work", "target_state": "starts over",
            "target_gap": "no checkpoint recovery", "purpose_alignment": "prevents lost work",
            "how_it_optimizes": "continues rather than repeats", "adaptation_plan": "add checkpoints",
            "target_touchpoints": ["src/run.ts"], "verification_plan": "kill and resume",
            "implementation_search_query": "durable workflow checkpoint library",
            "value_score": 90, "confidence": "high",
            "target_evidence_refs": ["T1"], "source_evidence_refs": ["S1"],
        }
        forged = dict(valid, capability="forged", source_evidence_refs=["S99"])
        result = research.validate_comparison(
            {"recommendations": [valid, forged]}, target, source)
        self.assertEqual([row["capability"] for row in result["recommendations"]],
                         ["workflow recovery"])
        self.assertEqual(len(result["rejected_rows"]), 1)
        self.assertFalse(result["validation"]["complete"])

    def test_comparison_accounts_for_every_profiled_source_capability(self):
        target = {"evidence": [{"id": "T1"}]}
        source = {"evidence": [{"id": "S1"}]}
        profile = {"capabilities": [
            {"name": "workflow recovery", "evidence_refs": ["S1"]},
            {"name": "audit export", "evidence_refs": ["S1"]},
        ]}
        row = {
            "capability": "workflow recovery", "decision": "adapt",
            "source_behavior": "resumes", "target_state": "restarts",
            "target_gap": "no recovery", "purpose_alignment": "reliability",
            "how_it_optimizes": "avoids repeat work", "adaptation_plan": "checkpoint",
            "target_touchpoints": ["run.py"], "verification_plan": "interrupt and resume",
            "implementation_search_query": "durable checkpoint workflow",
            "value_score": 90, "confidence": "high",
            "target_evidence_refs": ["T1"], "source_evidence_refs": ["S1"],
        }
        result = research.validate_comparison(
            {"recommendations": [row], "coverage_gaps": []},
            target, source, profile)
        self.assertEqual(result["validation"]["unaccounted_source_capabilities"],
                         ["audit export"])
        self.assertFalse(result["validation"]["complete"])

    def test_comparison_requires_exact_capability_name_and_its_own_evidence(self):
        target = {"evidence": [{"id": "T1"}]}
        source = {"evidence": [{"id": "S1"}, {"id": "S2"}]}
        profile = {"capabilities": [{
            "name": "Workflow Recovery", "evidence_refs": ["S2"],
        }]}
        row = {
            "capability": "workflow recovery", "decision": "adapt",
            "source_behavior": "resumes", "target_state": "restarts",
            "target_gap": "no recovery", "purpose_alignment": "reliability",
            "how_it_optimizes": "avoids repeat work", "adaptation_plan": "checkpoint",
            "target_touchpoints": ["run.py"], "verification_plan": "interrupt and resume",
            "implementation_search_query": "durable checkpoint workflow",
            "value_score": 90, "confidence": "high",
            "target_evidence_refs": ["T1"], "source_evidence_refs": ["S1"],
        }
        result = research.validate_comparison(
            {"recommendations": [row], "coverage_gaps": []},
            target, source, profile)
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["validation"]["unaccounted_source_capabilities"],
                         ["Workflow Recovery"])
        self.assertFalse(result["validation"]["complete"])

    def test_profile_rejects_blank_and_duplicate_capabilities(self):
        bundle = {"evidence": [{"id": "S1", "kind": "web-page"}]}
        profile = {
            "workflows": [], "optimization_needs": [], "coverage_gaps": [],
            "capabilities": [
                {"name": "Recovery", "behavior": "resumes", "user_value": "continuity",
                 "implementation_pattern": "checkpoint", "evidence_refs": ["S1"]},
                {"name": "recovery", "behavior": "duplicates", "user_value": "continuity",
                 "implementation_pattern": "checkpoint", "evidence_refs": ["S1"]},
                {"name": "", "behavior": "blank", "user_value": "none",
                 "implementation_pattern": "unknown", "evidence_refs": ["S1"]},
            ],
        }
        checked = research.validate_profile_references(profile, bundle, "S")
        self.assertEqual([row["name"] for row in checked["capabilities"]], ["Recovery"])
        self.assertEqual(len(checked["evidence_validation"]["rejected_rows"]), 2)
        self.assertFalse(checked["evidence_validation"]["complete"])

    def test_search_result_snippets_cannot_prove_program_behavior(self):
        bundle = {"evidence": [
            {"id": "S1", "kind": "search-ledger"},
            {"id": "S2", "kind": "web-page"},
        ]}
        profile = {
            "workflows": [], "optimization_needs": [], "coverage_gaps": [],
            "capabilities": [
                {"name": "snippet claim", "behavior": "claimed in a snippet",
                 "user_value": "unknown", "implementation_pattern": "unknown",
                 "evidence_refs": ["S1"]},
                {"name": "retrieved claim", "behavior": "shown on the page",
                 "user_value": "useful", "implementation_pattern": "observable workflow",
                 "evidence_refs": ["S2"]},
            ],
        }
        checked = research.validate_profile_references(profile, bundle, "S")
        self.assertEqual([row["name"] for row in checked["capabilities"]],
                         ["retrieved claim"])
        self.assertEqual(checked["evidence_validation"]["known_ids"], ["S2"])

    def test_scout_source_accepts_only_program_websites(self):
        for invalid in ("SourceSuite", "/tmp/source-suite"):
            with self.assertRaisesRegex(ValueError, "public product/program URL"):
                ff._research_program_reference(invalid, "S", "source")
        with self.assertRaisesRegex(ValueError, "not a source repository URL"):
            ff._research_program_reference(
                "https://github.com/example/source-suite", "S", "source")
        self.assertIsNone(ff._normalized_code_repository_url(
            "https://github.com/features/actions"))

        bundle = {
            "reference": "https://source.example", "kind": "website-url",
            "canonical_url": "https://source.example", "name_hint": "source.example",
            "evidence": [{"id": "S1", "url": "https://source.example",
                          "title": "Source", "text": "Observable workflow behavior."}],
            "coverage": {"complete": True, "retrieved": 1},
        }
        with mock.patch.object(research, "crawl_public_program", return_value=bundle):
            result = ff._research_program_reference(
                "https://source.example", "S", "source")
        self.assertEqual(result["kind"], "website-url")

    def test_behavioral_twin_identity_is_stable_per_canonical_url(self):
        first = research.behavioral_twin_identity(
            "HTTPS://Example.COM:443/product/?utm_source=x#demo", "Example Product")
        second = research.behavioral_twin_identity(
            "https://example.com/product", "Example Product")
        renamed = research.behavioral_twin_identity(
            "https://example.com/product", "Completely Different Display Name")
        other = research.behavioral_twin_identity(
            "https://example.com/another", "Example Product")
        self.assertEqual(first, second)
        self.assertEqual(second, renamed)
        self.assertNotEqual(first["branch"], other["branch"])
        self.assertTrue(first["branch"].startswith("scout/twin/"))
        self.assertTrue(first["subtree"].startswith("scout_twins/"))
        self.assertEqual(len(first["url_sha256"]), 64)

    def test_twin_spec_keeps_every_source_capability_even_when_target_rejects_it(self):
        bundle = {
            "canonical_url": "https://source.example", "name_hint": "Source",
            "evidence": [
                {"id": "S1", "kind": "web-page", "url": "https://source.example/a",
                 "title": "A", "text": "observable A"},
                {"id": "S2", "kind": "web-page", "url": "https://source.example/b",
                 "title": "B", "text": "marketing-only B"},
            ],
        }
        profile = {
            "name": "Source", "canonical_url": "https://source.example",
            "program_type": "service", "summary": "two features",
            "capabilities": [
                {"name": "Useful A", "behavior": "does A", "user_value": "A value",
                 "implementation_pattern": "pipeline", "evidence_refs": ["S1"],
                 "confidence": "high"},
                {"name": "Rejected B", "behavior": "claims B", "user_value": "B value",
                 "implementation_pattern": "unknown", "evidence_refs": ["S2"],
                 "confidence": "low"},
            ],
            "workflows": [{"name": "A flow", "behavior": "run A",
                           "evidence_refs": ["S1"]}],
            "limitations": [], "coverage_gaps": [],
        }
        comparison = {"recommendations": [
            {"capability": "Useful A", "decision": "adapt", "value_score": 90,
             "how_it_optimizes": "helps"},
            {"capability": "Rejected B", "decision": "reject", "value_score": 5,
             "how_it_optimizes": "does not help"},
        ]}
        spec = research.build_behavioral_twin_spec(
            bundle, profile, target_profile={"name": "Target"}, comparison=comparison)
        rows = spec["public_behavior_contract"]["capabilities"]
        self.assertEqual([row["name"] for row in rows], ["Useful A", "Rejected B"])
        self.assertEqual(rows[1]["target_fit"]["decision"], "reject")
        self.assertEqual(rows[1]["authoring_status"], "evidence-blocked")
        self.assertTrue(spec["validation"]["complete"])
        self.assertEqual(spec["completeness_contract"]["capability_total"], 2)
        self.assertNotIn("observable A", json.dumps(spec["evidence"]))


class ScoutPersistentTwinBranchTests(unittest.TestCase):
    class Provider:
        judge_model = "reviewer"
        model = "author"

        def structured(self, _system, prompt, schema, max_tokens=8000, **_kwargs):
            if schema is ff.BEHAVIORAL_TWIN_PLAN_SCHEMA:
                return {
                    "can_build": True, "reason": "", "architecture": "stdlib service",
                    "runtime": "Python 3.12", "files": ["app.py", "tests/test_app.py"],
                    "capability_plan": [
                        {"name": "Render greeting", "status": "full",
                         "implementation": "pure function", "files": ["app.py"],
                         "acceptance_tests": ["tests/test_app.py"], "blockers": []},
                        {"name": "Unproven magic", "status": "blocked",
                         "implementation": "not invented", "files": [],
                         "acceptance_tests": [], "blockers": ["low-confidence evidence"]},
                    ],
                    "workflow_plan": [
                        {"name": "Greeting flow", "status": "full",
                         "implementation": "call render", "acceptance_tests": ["tests/test_app.py"],
                         "blockers": []},
                    ],
                    "dependencies": [],
                }
            if schema is ff.BEHAVIORAL_TWIN_PATCH_SCHEMA:
                return {
                    "files": [
                        {"path": "app.py",
                         "contents": "def render(name: str) -> str:\n    return f'Hello, {name}!'\n"},
                        {"path": "tests/test_app.py", "contents": (
                            "import pathlib\nimport unittest\nfrom app import render\n\n"
                            "class GreetingTests(unittest.TestCase):\n"
                            "    def test_public_outcome(self):\n"
                            "        pathlib.Path('runtime-only.txt').write_text('side effect')\n"
                            "        self.assertEqual(render('Scout'), 'Hello, Scout!')\n\n"
                            "if __name__ == '__main__':\n    unittest.main()\n")},
                    ],
                    "delete_files": [],
                    "capability_accounting": [
                        {"name": "Render greeting", "status": "implemented",
                         "files": ["app.py"], "tests": ["tests/test_app.py"],
                         "limitations": [], "blockers": []},
                        {"name": "Unproven magic", "status": "blocked",
                         "files": [], "tests": [], "limitations": [],
                         "blockers": ["low-confidence evidence"]},
                    ],
                    "workflow_accounting": [
                        {"name": "Greeting flow", "status": "implemented",
                         "tests": ["tests/test_app.py"], "limitations": [], "blockers": []},
                    ],
                    "runtime_contract": {
                        "entrypoints": [{
                            "name": "Greeting CLI", "kind": "cli",
                            "command": ["python", "app.py"],
                            "implementation_files": ["app.py"],
                            "readiness_check": "Import app and call render with a name",
                        }],
                        "capability_routes": [{
                            "name": "Render greeting",
                            "execution_files": ["app.py"],
                            "acceptance_tests": ["tests/test_app.py"],
                            "success_artifacts": ["rendered greeting string"],
                            "validation": ["assert exact greeting output"],
                            "runtime_dependencies": [],
                        }],
                        "failure_policy": "Raise an error for invalid input rather than report fake success.",
                    },
                    "reuse_contract": {
                        "modules": [{
                            "purpose": "Render greetings in another program",
                            "module": "app", "symbols": ["render"],
                            "files": ["app.py"],
                            "consumer_contract": "render(name) returns the completed greeting string",
                        }],
                        "integration_example": "from app import render; output = render('Scout')",
                    },
                    "dependencies": [], "summary": "working greeting twin",
                    "commit_message": "Build source greeting behavioral twin",
                }
            if schema is ff.FINAL_REVIEW_SCHEMA:
                commit = re.search(r"EXPECTED FINAL COMMIT: ([0-9a-f]{40})", prompt).group(1)
                return {"verdict": "approve", "commit": commit, "findings": [],
                        "evidence_consistent": True, "reason": "exact commit verified"}
            raise AssertionError("unexpected schema")

    class RepairingProvider(Provider):
        def __init__(self):
            self.repair_calls = 0

        def structured(self, system, prompt, schema, max_tokens=8000, **kwargs):
            payload = super().structured(system, prompt, schema, max_tokens, **kwargs)
            if schema is ff.BEHAVIORAL_TWIN_PATCH_SCHEMA:
                if system == ff.BEHAVIORAL_TWIN_REPAIR_SYSTEM:
                    self.repair_calls += 1
                elif system == ff.BEHAVIORAL_TWIN_PATCH_SYSTEM:
                    for row in payload["files"]:
                        if row["path"] == "tests/test_app.py":
                            row["contents"] = row["contents"].replace(
                                "'Hello, Scout!'", "'WRONG OUTPUT'")
            return payload

    @staticmethod
    def _git(root, *args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def _fixture(self):
        root = tempfile.mkdtemp(prefix="ff-twin-target-")
        remote = tempfile.mkdtemp(prefix="ff-twin-origin-")
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, remote, True)
        self.assertEqual(self._git(root, "init", "-q", "-b", "main").returncode, 0)
        self._git(root, "config", "user.name", "Test")
        self._git(root, "config", "user.email", "test@example.com")
        with open(os.path.join(root, "target.txt"), "w", encoding="utf-8") as fh:
            fh.write("target stays untouched\n")
        self._git(root, "add", "-A")
        self.assertEqual(self._git(root, "commit", "-qm", "base").returncode, 0)
        self.assertEqual(subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", remote],
            capture_output=True).returncode, 0)
        self._git(root, "remote", "add", "origin", remote)
        self.assertEqual(self._git(root, "push", "-q", "origin", "main").returncode, 0)
        return root, remote

    @staticmethod
    def _spec():
        bundle = {
            "canonical_url": "https://source.example", "name_hint": "Source",
            "evidence": [{"id": "S1", "kind": "web-page",
                          "url": "https://source.example/features", "title": "Features",
                          "text": "Greeting behavior and an unproven marketing claim."}],
        }
        profile = {
            "name": "Source", "canonical_url": "https://source.example",
            "program_type": "service", "summary": "greets users",
            "capabilities": [
                {"name": "Render greeting", "behavior": "renders a greeting",
                 "user_value": "friendly output", "implementation_pattern": "function",
                 "evidence_refs": ["S1"], "confidence": "high"},
                {"name": "Unproven magic", "behavior": "claims magic",
                 "user_value": "unknown", "implementation_pattern": "unknown",
                 "evidence_refs": ["S1"], "confidence": "low"},
            ],
            "workflows": [{"name": "Greeting flow", "behavior": "enter name, get greeting",
                           "evidence_refs": ["S1"]}],
            "limitations": [], "coverage_gaps": [],
        }
        return bundle, research.build_behavioral_twin_spec(bundle, profile)

    def test_branch_is_persistent_remote_and_target_worktree_never_switches(self):
        root, _remote = self._fixture()
        bundle, spec = self._spec()
        before_branch = self._git(root, "branch", "--show-current").stdout.strip()
        before_head = self._git(root, "rev-parse", "HEAD").stdout.strip()
        before_status = self._git(root, "status", "--porcelain").stdout
        args = argparse.Namespace(verify=True, isolate_verify=True,
                                  trust_repo=True, push=True,
                                  scout_twin_repo=root)
        result = ff.build_behavioral_twin_branch(
            args, root, spec, bundle, self.Provider())
        self.assertEqual(result.status, "published", result.detail)
        self.assertEqual(result.commit, result.remote_commit)
        self.assertEqual(result.verification[-1]["network_controls"], "deny-requested")
        self.assertEqual(self._git(root, "branch", "--show-current").stdout.strip(),
                         before_branch)
        self.assertEqual(self._git(root, "rev-parse", "HEAD").stdout.strip(), before_head)
        self.assertEqual(self._git(root, "status", "--porcelain").stdout, before_status)
        branch = spec["identity"]["branch"]
        subtree = spec["identity"]["subtree"]
        self.assertEqual(self._git(root, "show", f"{branch}:{subtree}/app.py").returncode, 0)
        self.assertNotEqual(
            self._git(root, "show", f"{branch}:{subtree}/runtime-only.txt").returncode, 0,
            "test side effects from the Git-free verification copy must not be committed")
        self.assertNotEqual(self._git(root, "show", f"main:{subtree}/app.py").returncode, 0)
        remote = self._git(root, "ls-remote", "--heads", "origin",
                           f"refs/heads/{branch}")
        self.assertIn(result.commit, remote.stdout)
        self.assertIn(branch, self._git(root, "branch", "--list").stdout)
        second = ff.build_behavioral_twin_branch(
            args, root, spec, bundle, self.Provider())
        self.assertEqual(second.status, "up-to-date", second.detail)
        self.assertEqual(second.branch, branch)
        self.assertEqual(second.independent_review.get("verdict"), "approve")

    def test_failed_generated_app_is_repaired_and_reverified_before_push(self):
        root, _remote = self._fixture()
        bundle, spec = self._spec()
        provider = self.RepairingProvider()
        args = argparse.Namespace(verify=True, isolate_verify=True,
                                  trust_repo=True, push=True,
                                  scout_twin_repo=root)
        result = ff.build_behavioral_twin_branch(
            args, root, spec, bundle, provider)
        self.assertEqual(result.status, "published", result.detail)
        self.assertGreaterEqual(provider.repair_calls, 1)
        branch = spec["identity"]["branch"]
        subtree = spec["identity"]["subtree"]
        receipt = self._git(root, "show", f"{branch}:{subtree}/SUPERVISION.json")
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        supervision = json.loads(receipt.stdout)
        self.assertTrue(supervision["completed"])
        self.assertEqual(
            [row["status"] for row in supervision["cycles"]
             if row["stage"] == "verification"],
            ["rejected", "passed"])

    def test_patch_validator_refuses_path_escape_and_missing_feature(self):
        _bundle, spec = self._spec()
        patch = {
            "files": [{"path": "../../outside.py", "contents": "print('x')\n"}],
            "delete_files": [], "capability_accounting": [],
            "workflow_accounting": [], "dependencies": [],
            "summary": "bad", "commit_message": "bad",
        }
        ok, reason, _accounting = ff._validate_behavioral_twin_patch(spec, patch)
        self.assertFalse(ok)
        self.assertIn("unsafe", reason)
        self.assertIn("missing capability accounting", reason)

        blocked_ready = self.Provider().structured(
            "", "", ff.BEHAVIORAL_TWIN_PATCH_SCHEMA)
        blocked_ready["capability_accounting"][0] = {
            "name": "Render greeting", "status": "blocked", "files": [], "tests": [],
            "limitations": [], "blockers": ["would take engineering effort"],
        }
        ok, reason, _accounting = ff._validate_behavioral_twin_patch(
            spec, blocked_ready)
        self.assertFalse(ok)
        self.assertIn("sufficient public evidence", reason)

    def test_verification_refuses_a_suite_that_discovers_zero_tests(self):
        with tempfile.TemporaryDirectory(prefix="ff-empty-twin-") as root:
            os.makedirs(os.path.join(root, "tests"))
            with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as fh:
                fh.write("VALUE = 1\n")
            with open(os.path.join(root, "tests", "test_empty.py"),
                      "w", encoding="utf-8") as fh:
                fh.write("VALUE = 1\n")
            args = argparse.Namespace(verify=True, isolate_verify=True, trust_repo=True)
            ok, receipts, reason, inventory = ff._verify_behavioral_twin(root, args)
        self.assertFalse(ok)
        self.assertIn("zero executable acceptance tests", reason)
        self.assertEqual(receipts[-1].get("tests_run"), 0)
        self.assertEqual(inventory.get("file_count"), 2)


class HeyGenStandaloneReplayTests(unittest.TestCase):
    """Named replay of the owner's required standalone URL acceptance case.

    The evidence rows are short factual paraphrases of the official public
    HeyGen pages inspected on 2026-09-02. No target-program evidence, vendor
    source, or private behavior is present in the fixture.
    """

    def test_full_heygen_twin_is_created_without_any_target_program(self):
        source_bundle = {
            "canonical_url": "https://www.heygen.com/", "name_hint": "HeyGen",
            "evidence": [
                {"id": "S1", "kind": "web-page", "title": "HeyGen homepage",
                 "url": "https://www.heygen.com/",
                 "text": "Publicly describes text, image, and audio inputs producing videos "
                         "with avatars, voiceovers, captions, visuals, and animations."},
                {"id": "S2", "kind": "web-page", "title": "Avatar IV",
                 "url": "https://www.heygen.com/avatars/avatar-iv",
                 "text": "Publicly describes photo-to-talking-video, lip sync, gestures, "
                         "and full-body formats."},
                {"id": "S3", "kind": "web-page", "title": "Avatar IV API",
                 "url": "https://www.heygen.com/blog/announcing-the-avatar-iv-api",
                 "text": "Publicly describes programmatic photo plus script video creation."},
                {"id": "S4", "kind": "web-page", "title": "Video Agent",
                 "url": "https://www.heygen.com/academy/video-agent",
                 "text": "Publicly describes prompt-to-plan, user review and feedback, then "
                         "full generation and editing."},
                {"id": "S5", "kind": "web-page", "title": "Motion prompts",
                 "url": ("https://help.heygen.com/en/articles/12805098-fine-tune-avatar-"
                         "gestures-and-movements-with-custom-motion-prompts-avatar-iv-v"),
                 "text": "Publicly describes free-text and preset control of expression, "
                         "gesture, gaze, and per-scene motion."},
                {"id": "S6", "kind": "web-page", "title": "Avatar V",
                 "url": "https://www.heygen.com/avatars/avatar-v",
                 "text": "Publicly claims long-form identity and motion consistency learned "
                         "from an input video."},
            ],
        }
        source_profile = {
            "name": "HeyGen", "summary": "AI avatar video creation platform",
            "program_type": "service", "canonical_url": "https://www.heygen.com/",
            "workflows": [
                {"name": "Prompt, review, then generate",
                 "behavior": "Turns a prompt into a reviewable plan before generation",
                 "evidence_refs": ["S4"]},
                {"name": "Photo and script to avatar video",
                 "behavior": "Accepts a photo and script and produces a talking avatar video",
                 "evidence_refs": ["S2", "S3"]},
            ],
            "capabilities": [
                {"name": "Prompt-to-video planning and approval",
                 "behavior": "Creates a plan, accepts feedback, then proceeds to generation",
                 "user_value": "Users can correct intent before expensive generation",
                 "implementation_pattern": "plan/review/generate state machine",
                 "evidence_refs": ["S4"], "confidence": "high"},
                {"name": "Photo-to-talking-avatar video",
                 "behavior": "Animates a supplied photo into a talking avatar video",
                 "user_value": "Creates presenter video without recording footage",
                 "implementation_pattern": "image-conditioned avatar renderer",
                 "evidence_refs": ["S2"], "confidence": "high"},
                {"name": "Script lip sync and voiceover",
                 "behavior": "Synchronizes avatar speech motion with scripted audio",
                 "user_value": "Produces understandable presenter speech",
                 "implementation_pattern": "speech and viseme timeline",
                 "evidence_refs": ["S1", "S2"], "confidence": "high"},
                {"name": "Directed gesture, expression, gaze, and motion",
                 "behavior": "Applies prompts or presets to avatar motion per scene",
                 "user_value": "Gives creators control over delivery",
                 "implementation_pattern": "scene-scoped motion controls",
                 "evidence_refs": ["S5"], "confidence": "high"},
                {"name": "Full-body and multiple output formats",
                 "behavior": "Supports full-body avatars and multiple presentation formats",
                 "user_value": "Fits varied publishing layouts",
                 "implementation_pattern": "layout-aware composition pipeline",
                 "evidence_refs": ["S2"], "confidence": "medium"},
                {"name": "Programmatic avatar video API",
                 "behavior": "Accepts photo and script inputs programmatically",
                 "user_value": "Automates repeated video generation",
                 "implementation_pattern": "asynchronous job API",
                 "evidence_refs": ["S3"], "confidence": "high"},
                {"name": "Captions, visuals, and animation composition",
                 "behavior": "Composes captions and supporting visuals into generated video",
                 "user_value": "Produces a more complete edited result",
                 "implementation_pattern": "timeline composition",
                 "evidence_refs": ["S1"], "confidence": "medium"},
                {"name": "Long-form avatar identity consistency",
                 "behavior": "Keeps avatar identity stable across longer and varied scenes",
                 "user_value": "Avoids distracting identity drift",
                 "implementation_pattern": "identity-conditioned rendering and QA",
                 "evidence_refs": ["S6"], "confidence": "medium"},
            ],
            "stack_signals": [], "limitations": ["Private model internals are not public"],
            "coverage_gaps": ["Public evidence does not establish model architecture"],
        }
        twin = research.build_behavioral_twin_spec(source_bundle, source_profile)
        twin_names = [row["name"] for row in
                      twin["public_behavior_contract"]["capabilities"]]
        author_evidence = json.loads(ff._twin_public_evidence_context(
            source_bundle, twin))
        self.assertEqual(len(twin_names), 8)
        self.assertEqual(
            set(twin_names),
            {row["name"] for row in source_profile["capabilities"]})
        self.assertEqual({row["id"] for row in author_evidence},
                         {"S1", "S2", "S3", "S4", "S5", "S6"})
        self.assertEqual(twin["identity"]["branch"],
                         "scout/twin/heygen-c0fadff54acb")
        self.assertNotIn("target_profile", twin)
        self.assertNotIn("target_fit", twin)


class ScoutStandaloneUrlEndToEndTests(unittest.TestCase):
    def test_url_only_run_builds_scout_branch_without_target_or_repo_rewards(self):
        bundle = {
            "reference": "https://source.example",
            "kind": "website-url", "name_hint": "SourceSuite",
            "canonical_url": "https://source.example/",
            "retrieved_at": "2026-09-02T00:00:00+00:00",
            "evidence": [{
                "id": "S1", "url": "https://source.example/features",
                "title": "Source features", "text": "Creates a finished video.",
            }],
            "coverage": {"complete": True, "pages_retrieved": 1},
        }
        profile = {
            "name": "SourceSuite", "summary": "Creates videos.",
            "program_type": "service", "canonical_url": "https://source.example/",
            "workflows": [{"name": "Create video", "behavior": "Create then export",
                           "evidence_refs": ["S1"]}],
            "capabilities": [{
                "name": "Video export", "behavior": "Produces a finished video",
                "user_value": "Delivers the requested media",
                "implementation_pattern": "validated render pipeline",
                "evidence_refs": ["S1"], "confidence": "high",
            }],
            "stack_signals": [], "limitations": [], "coverage_gaps": [],
            "evidence_validation": {"complete": True},
        }
        args = argparse.Namespace(
            target=None, program="https://source.example", apply=True,
            assume_yes=True, apply_tier="adopt", max_cost=25.0,
            model_mode="best", provider="auto", model=None, economy=False,
            judge_model=None, trust_repo=True,
        )
        published = ff.TwinResult(
            "published", "verified", "scout/twin/source-example-123456789abc",
            "scout_twins/source-example-123456789abc")
        with tempfile.TemporaryDirectory(prefix="ff-url-only-queue-") as root:
            coordinator = ff._ff_execution.SequentialOrchestrator(
                "scout", [args.program],
                state_path=os.path.join(root, "queue.json"),
                queue_id="url-only",
            )
            coordinator.start_target(0)
            args.execution_orchestrator = coordinator
            with mock.patch.object(ff, "_research_program_reference",
                                   return_value=bundle), \
                 mock.patch.object(ff, "_best_available_provider",
                                   return_value=object()), \
                 mock.patch.object(ff, "_profile_research_bundle",
                                   return_value=profile), \
                 mock.patch.object(ff, "build_behavioral_twin_branch",
                                   return_value=published) as build, \
                 mock.patch.object(ff, "repo_rewards_search") as repo_rewards:
                rc = ff._run_scout_impl(args)
            rc = coordinator.finish_target(0, rc)
            receipt = coordinator.snapshot()["items"][0]
        self.assertEqual(rc, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["standalone_scout"]["branch"], published.branch)
        self.assertEqual(receipt["standalone_scout"]["status"], "published")
        self.assertEqual(build.call_count, 1)
        self.assertIsNone(build.call_args.args[1])
        repo_rewards.assert_not_called()


class ScoutProgramComparisonEndToEndTests(unittest.TestCase):
    TARGET_PROFILE = {
        "name": "TargetApp", "summary": "Processes applications but restarts failed jobs.",
        "users": ["operators"], "goals": ["complete applications reliably"],
        "stack": ["Python"],
        "workflows": [{"name": "application processing", "current_behavior": "one shot",
                       "status": "partial", "evidence_refs": ["T1"]}],
        "capabilities": [{"name": "recovery", "current_behavior": "none",
                          "status": "missing", "evidence_refs": ["T1"]}],
        "constraints": [],
        "optimization_needs": [{"need": "durable recovery", "why": "jobs repeat",
                                "priority": "high", "evidence_refs": ["T1"]}],
        "coverage_gaps": [],
    }
    SOURCE_PROFILE = {
        "name": "SourceSuite", "summary": "Resumes multi-step workflows.",
        "program_type": "service", "canonical_url": "https://source.example",
        "workflows": [{"name": "durable execution", "behavior": "resumes at checkpoints",
                       "evidence_refs": ["S1"]}],
        "capabilities": [{"name": "checkpoint recovery",
                          "behavior": "persists each completed stage",
                          "user_value": "does not repeat work",
                          "implementation_pattern": "durable state machine",
                          "evidence_refs": ["S1"], "confidence": "high"}],
        "stack_signals": [], "limitations": [], "coverage_gaps": [],
    }
    COMPARISON = {
        "target_name": "TargetApp", "scouted_name": "SourceSuite",
        "summary": "Checkpoint recovery is a material target improvement.",
        "coverage_gaps": [],
        "recommendations": [{
            "capability": "checkpoint recovery", "decision": "adapt",
            "source_behavior": "persists and resumes each stage",
            "target_state": "the target reruns the entire job",
            "target_gap": "no durable checkpoint", "purpose_alignment": "improves completion",
            "how_it_optimizes": "removes repeated work after interruption",
            "adaptation_plan": "persist stage state and resume the first incomplete stage",
            "target_touchpoints": ["app.py"],
            "verification_plan": "interrupt after stage one and prove stage two resumes",
            "implementation_search_query": "python durable workflow checkpoint state machine",
            "value_score": 92, "confidence": "high",
            "target_evidence_refs": ["T1"], "source_evidence_refs": ["S1"],
        }],
    }
    RESULT = {
        "repo": {"fullName": "example/durable-flow",
                 "htmlUrl": "https://github.com/example/durable-flow",
                 "primaryLanguage": "Python", "stars": 400,
                 "licenseSpdx": "MIT", "pushedAt": "2026-08-30",
                 "description": "Durable workflow checkpoints."},
        "ai": {"purposeSummary": "Checkpoint and resume workflows.",
               "suggestedUses": ["durable background jobs"]},
        "finalScore": 88, "safety": {"verdict": "allow"},
    }

    def test_named_scout_profiles_both_programs_then_searches_exact_delta(self):
        with tempfile.TemporaryDirectory() as target:
            with open(os.path.join(target, "app.py"), "w", encoding="utf-8") as fh:
                fh.write("def run():\n    return 'one-shot'\n")
            bundles = {
                "target": {
                    "reference": target, "kind": "local-repository", "name_hint": "TargetApp",
                    "canonical_url": "", "retrieved_at": "2026-09-02T00:00:00+00:00",
                    "evidence": [{"id": "T1", "path": "app.py", "title": "Target source",
                                  "text": "def run(): return 'one-shot'"}],
                    "coverage": {"complete": True, "source_files": 1},
                },
                "source": {
                    "reference": "https://source.example", "kind": "website-url",
                    "name_hint": "SourceSuite", "canonical_url": "https://source.example",
                    "retrieved_at": "2026-09-02T00:00:00+00:00",
                    "evidence": [{"id": "S1", "url": "https://source.example/features",
                                  "title": "Source features",
                                  "text": "Stages are checkpointed and resume after interruption."}],
                    "coverage": {"complete": True, "pages_retrieved": 1},
                },
            }
            queries = []

            def acquire(_ref, prefix, role, args=None):
                return bundles["target" if role == "target" else "source"]

            def judge(_provider, _system, _prompt, schema, max_tokens=8000):
                if schema is research.TARGET_PROFILE_SCHEMA:
                    return dict(self.TARGET_PROFILE)
                if schema is research.SCOUTED_PROGRAM_PROFILE_SCHEMA:
                    return dict(self.SOURCE_PROFILE)
                if schema is research.PROGRAM_COMPARISON_SCHEMA:
                    return json.loads(json.dumps(self.COMPARISON))
                if schema is ff.BENEFIT_SCHEMA:
                    return {"benefit_score": 90, "verdict": "adopt",
                            "how_it_helps": "adds exact checkpoint recovery",
                            "integration_note": "wire into app.py", "risks": []}
                raise AssertionError("unexpected schema")

            def rr(_base, query, lens=None, attempts=3):
                queries.append(query)
                return [self.RESULT]

            args = argparse.Namespace(
                target=target, program="https://source.example",
                allow_remote_program_context=True,
                repo_rewards_url="http://localhost:3000", auto_start=False,
                max_cost=25.0, top=3, apply=False, apply_tier="adopt",
                clone_inspect=False, execution_orchestrator=None, trust_repo=False,
                model_mode="best", provider="auto", model=None, economy=False,
                judge_model=None, no_remote_repo_rewards=False,
            )
            with mock.patch.object(ff, "_research_program_reference", acquire), \
                 mock.patch.object(ff, "_best_available_provider", lambda *a, **k: object()), \
                 mock.patch.object(ff, "_judge", judge), \
                 mock.patch.object(ff, "resolve_repo_rewards_url",
                                   lambda *a, **k: ("http://localhost:3000", "fixture RR")), \
                 mock.patch.object(ff, "repo_rewards_search", rr):
                rc = ff._run_named_scout_impl(args)

            self.assertEqual(rc, 0)
            self.assertEqual(queries, ["python durable workflow checkpoint state machine"])
            reports = [name for name in os.listdir(target)
                       if name.endswith("_repo_rewards_report.md")]
            self.assertEqual(len(reports), 1)
            with open(os.path.join(target, reports[0]), encoding="utf-8") as fh:
                report = fh.read()
            self.assertIn("Target-versus-scouted capability decisions", report)
            self.assertIn("checkpoint recovery", report)
            self.assertIn("T1", report)
            self.assertIn("S1", report)
            structured = [name for name in os.listdir(target)
                          if name.startswith("_scout_report.")]
            self.assertEqual(len(structured), 1)
            with open(os.path.join(target, structured[0]), encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(payload["program_comparison"]["scouted_profile"]["name"],
                             "SourceSuite")
            # Raw source/program page content is hashed in artifacts, not copied.
            evidence = payload["program_comparison"]["scouted_evidence"]["evidence"][0]
            self.assertNotIn("text", evidence)
            self.assertEqual(len(evidence["text_sha256"]), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
