"""Regression tests for Scout's target-vs-entered-program research path."""
from __future__ import annotations

import argparse
import email.message
import json
import os
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
