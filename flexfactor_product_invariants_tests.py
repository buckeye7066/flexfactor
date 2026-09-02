from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flexfactor_product_invariants import (
    collect_capability_test_evidence,
    competitor_capability_id,
    evaluate_product_invariants,
    stamp_competitor_implementation,
)


def contract():
    return {"authored": True, "purpose": "Help the user complete the core job."}


def purpose(gaps=None):
    gaps = list(gaps or [])
    return {
        "criteria_total": 2,
        "criteria_met": 2 if not gaps else 1,
        "criteria_unknown": 0,
        "gaps": gaps,
    }


def competitor(*, accept=True, applied=True, risk="low", mitigation=""):
    item = {
        "name": "Rival",
        "evidence_status": "verified",
        "evidence_urls": ["https://example.com/rival"],
        "reuse_mode": "clean-room-from-documented-behavior",
        "license": "proprietary",
        "license_source": "official-product-page",
        "bridge_status": "bridged" if applied else "rejected",
        "entered_fix_stream": applied,
        "implementation_status": (
            "applied-and-build-verified" if applied else "not-applied"
        ),
        "implementation_evidence": {
            "target_file": "src/recovery.py",
            "target_applied": applied,
            "target_verified": applied,
            "test_files": ["tests/test_recovery.py"] if applied else [],
            "all_generated_test_files": ["tests/test_recovery.py"] if applied else [],
            "verification_passed": applied,
        },
        "idea": {
            "idea_title": "Fast recovery",
            "what_it_does": "Resumes interrupted work.",
            "why_valuable": "Protects the user's progress.",
            "evidence_basis": "Official product documentation.",
            "accept": accept,
            "purpose_reason": "Directly improves the core workflow.",
            "wiring_plan": "Wire resume through the existing job boundary.",
            "verification_plan": "Interrupt and resume a real job in a regression test.",
            "risk_level": risk,
            "risk_reason": "Touches durable state.",
            "risk_mitigation": mitigation,
            "already_present": False,
            "code_fixable": True,
            "file": "src/recovery.py",
        },
    }
    return item


def research(item=None, *, verified=5, target=5):
    competitors = []
    for index in range(max(0, int(verified))):
        current = deepcopy(item) if index == 0 and item is not None else competitor(
            accept=index == 0,
            applied=index == 0,
        )
        current["name"] = f"Rival {index + 1}"
        current["evidence_urls"] = [f"https://example.com/rival-{index + 1}"]
        if index > 0:
            current["idea"]["purpose_reason"] = (
                "Rejected after fit review because it adds no material value to the core job."
            )
        capability_id = competitor_capability_id(current, index)
        current["capability_id"] = capability_id
        current["implementation_evidence"]["capability_id"] = capability_id
        competitors.append(current)
    return {
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "verified": verified,
        "coverage_note": f"{verified}/{target}",
        "implementation_evidence_version": "flexfactor.product-invariants.v1",
        "competitors": competitors,
    }


def evaluate(**over):
    args = {
        "purpose_enabled": True,
        "purpose_contract": contract(),
        "purpose_confidence": "owner-authored",
        "purpose_before": purpose([{"title": "old gap"}]),
        "purpose_after": purpose(),
        "purpose_errors": [],
        "competitors_enabled": True,
        "competitor_research": research(),
        "competitor_target": 5,
        "applied_files": ["src/recovery.py"],
        "test_files": ["tests/test_recovery.py"],
        "verification_passed": True,
        "license_compatible": lambda spdx: str(spdx).lower() in {
            "mit",
            "apache-2.0",
            "bsd-3-clause",
        },
    }
    args.update(over)
    return evaluate_product_invariants(**args)


class ProductInvariantTests(unittest.TestCase):
    def test_generated_test_markers_bind_evidence_per_capability(self):
        required = [
            {"capability_id": "competitor-1-fast"},
            {"capability_id": "competitor-2-safe"},
        ]
        evidence = collect_capability_test_evidence(
            test_path="tests\\test_recovery.py",
            contents=(
                "# FLEXFACTOR_CAPABILITY:competitor-1-fast\n"
                "def test_fast_recovery():\n    assert recover() == 'fast'\n"
            ),
            required_capabilities=required,
        )
        self.assertEqual(
            evidence,
            {"competitor-1-fast": ["tests/test_recovery.py"]},
        )

    def test_real_build_ledger_is_the_only_source_of_implementation_status(self):
        ledger = research(competitor(applied=False))
        ledger["competitors"][0]["bridge_status"] = "bridged"
        ledger["competitors"][0]["entered_fix_stream"] = True
        capability_id = competitor_capability_id(ledger["competitors"][0], 0)
        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=[],
            test_files=["tests/test_recovery.py"],
            tests_by_source={"src/recovery.py": ["tests/test_recovery.py"]},
            tests_by_capability={capability_id: ["tests/test_recovery.py"]},
            verification_passed=True,
        )
        item = ledger["competitors"][0]
        self.assertEqual(item["implementation_status"], "applied-and-build-verified")
        self.assertEqual(
            item["implementation_evidence"],
            {
                "capability_id": capability_id,
                "target_file": "src/recovery.py",
                "target_applied": True,
                "target_verified": True,
                "test_files": ["tests/test_recovery.py"],
                "all_generated_test_files": ["tests/test_recovery.py"],
                "verification_passed": True,
            },
        )

        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=["src/recovery.py"],
            test_files=["tests/test_recovery.py"],
            tests_by_source={"src/recovery.py": ["tests/test_recovery.py"]},
            tests_by_capability={capability_id: ["tests/test_recovery.py"]},
            verification_passed=True,
        )
        self.assertEqual(
            item["implementation_status"], "applied-verification-incomplete"
        )

    def test_capability_test_must_exist_in_global_and_target_test_ledgers(self):
        ledger = research(competitor(applied=False))
        ledger["competitors"][0]["bridge_status"] = "bridged"
        ledger["competitors"][0]["entered_fix_stream"] = True
        capability_id = competitor_capability_id(ledger["competitors"][0], 0)
        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=[],
            test_files=["tests/test_real.py"],
            tests_by_source={"src/other.py": ["tests/test_real.py"]},
            tests_by_capability={capability_id: ["tests/test_forged.py"]},
            verification_passed=True,
        )
        item = ledger["competitors"][0]
        self.assertEqual(
            item["implementation_status"], "applied-verification-incomplete"
        )
        self.assertEqual(item["implementation_evidence"]["test_files"], [])

    def test_unrelated_test_never_certifies_a_selected_capability(self):
        ledger = research(competitor(applied=False))
        ledger["competitors"][0]["bridge_status"] = "bridged"
        ledger["competitors"][0]["entered_fix_stream"] = True
        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=[],
            test_files=["tests/test_other.py"],
            tests_by_source={"src/other.py": ["tests/test_other.py"]},
            tests_by_capability={"competitor-elsewhere": ["tests/test_other.py"]},
            verification_passed=True,
        )
        item = ledger["competitors"][0]
        self.assertEqual(
            item["implementation_status"], "applied-verification-incomplete"
        )
        self.assertEqual(item["implementation_evidence"]["test_files"], [])
        result = evaluate(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            test_files=["tests/test_other.py"],
            verification_passed=True,
        )
        self.assertFalse(result["ready"])
        self.assertIn(
            "no executable test artifact",
            " ".join(gate["evidence"] for gate in result["blockers"]),
        )

    def test_capabilities_sharing_a_target_need_distinct_capability_evidence(self):
        ledger = research()
        second = ledger["competitors"][1]
        second["idea"].update({
            "accept": True,
            "purpose_reason": "Adds a separate recovery-safety guarantee.",
            "code_fixable": True,
            "file": "src/recovery.py",
        })
        second["bridge_status"] = "bridged"
        second["entered_fix_stream"] = True
        first_id = competitor_capability_id(ledger["competitors"][0], 0)
        second_id = competitor_capability_id(second, 1)

        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=[],
            test_files=["tests/test_fast_recovery.py"],
            tests_by_source={
                "src/recovery.py": ["tests/test_fast_recovery.py"],
            },
            tests_by_capability={
                first_id: ["tests/test_fast_recovery.py"],
            },
            verification_passed=True,
        )
        self.assertEqual(
            ledger["competitors"][0]["implementation_status"],
            "applied-and-build-verified",
        )
        self.assertEqual(
            ledger["competitors"][1]["implementation_status"],
            "applied-verification-incomplete",
        )
        blocked = evaluate(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            test_files=["tests/test_fast_recovery.py"],
        )
        self.assertFalse(blocked["ready"])

        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=[],
            test_files=[
                "tests/test_fast_recovery.py",
                "tests/test_safe_recovery.py",
            ],
            tests_by_source={
                "src/recovery.py": [
                    "tests/test_fast_recovery.py",
                    "tests/test_safe_recovery.py",
                ],
            },
            tests_by_capability={
                first_id: ["tests/test_fast_recovery.py"],
                second_id: ["tests/test_safe_recovery.py"],
            },
            verification_passed=True,
        )
        passed = evaluate(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            test_files=[
                "tests/test_fast_recovery.py",
                "tests/test_safe_recovery.py",
            ],
        )
        self.assertTrue(passed["ready"], passed["blockers"])

    def test_complete_purpose_and_verified_selected_capability_pass(self):
        result = evaluate()
        self.assertTrue(result["ready"], result["blockers"])
        self.assertEqual(result["selected_capabilities"][0]["capability"], "Fast recovery")

    def test_open_or_unknown_purpose_never_converges(self):
        result = evaluate(purpose_after=purpose([{"title": "still open"}]))
        self.assertFalse(result["ready"])
        self.assertIn("purpose-fulfilled", {g["id"] for g in result["blockers"]})

    def test_owner_authored_label_cannot_replace_actual_authored_provenance(self):
        forged = evaluate(
            purpose_contract={"authored": False},
            purpose_confidence="owner-authored",
        )
        self.assertIn(
            "purpose-authority",
            {gate["id"] for gate in forged["blockers"]},
        )
        inferred = evaluate(
            purpose_contract={"authored": False},
            purpose_confidence="strongly-inferred",
        )
        self.assertNotIn(
            "purpose-authority",
            {gate["id"] for gate in inferred["blockers"]},
        )

    def test_disabled_or_incomplete_research_is_a_blocker(self):
        disabled = evaluate(competitors_enabled=False)
        self.assertFalse(disabled["ready"])
        thin = evaluate(competitor_research=research(verified=2, target=5))
        self.assertIn("competitive-coverage", {g["id"] for g in thin["blockers"]})

    def test_reported_count_and_lowered_target_cannot_pad_thin_research(self):
        padded = research(verified=1, target=1)
        padded["verified"] = 5
        result = evaluate(
            competitor_research=padded,
            competitor_target=5,
        )
        self.assertFalse(result["ready"])
        coverage = next(
            gate for gate in result["blockers"] if gate["id"] == "competitive-coverage"
        )
        self.assertIn("corroborated=1/5", coverage["evidence"])
        self.assertIn("reported=5", coverage["evidence"])

    def test_duplicate_records_and_sources_cannot_pad_competitor_coverage(self):
        duplicated = research()
        for item in duplicated["competitors"]:
            item["name"] = "Same rival"
            item["evidence_urls"] = ["https://example.com/same-rival"]
        result = evaluate(competitor_research=duplicated)
        self.assertIn(
            "competitive-coverage",
            {gate["id"] for gate in result["blockers"]},
        )

    def test_research_older_than_exactly_thirty_days_is_stale(self):
        stale = research()
        stale["researched_at"] = (
            datetime.now(timezone.utc) - timedelta(days=30, seconds=1)
        ).isoformat()
        result = evaluate(competitor_research=stale)
        self.assertIn(
            "competitive-research-current",
            {gate["id"] for gate in result["blockers"]},
        )

    def test_selected_capability_must_be_changed_wired_and_tested(self):
        result = evaluate(
            competitor_research=research(competitor(applied=False)),
            applied_files=[],
            test_files=[],
            verification_passed=False,
        )
        self.assertFalse(result["ready"])
        evidence = " ".join(g["evidence"] for g in result["blockers"])
        self.assertIn("never entered", evidence)
        self.assertIn("not changed", evidence)
        self.assertIn("verification", evidence)

    def test_fit_risk_and_duplication_review_is_required(self):
        item = competitor()
        item["idea"].pop("risk_reason")
        item["idea"].pop("already_present")
        result = evaluate(competitor_research=research(item))
        self.assertIn(
            "competitive-fit-risk-reviewed",
            {g["id"] for g in result["blockers"]},
        )

    def test_medium_or_high_risk_selection_needs_a_mitigation(self):
        result = evaluate(competitor_research=research(competitor(risk="high")))
        self.assertIn(
            "competitive-fit-risk-reviewed",
            {g["id"] for g in result["blockers"]},
        )

    def test_reference_only_capability_is_never_implemented_blindly(self):
        item = competitor()
        item["reuse_mode"] = "reference-only"
        result = evaluate(competitor_research=research(item))
        self.assertFalse(result["ready"])
        self.assertIn("reference-only", " ".join(g["evidence"] for g in result["blockers"]))

    def test_direct_reuse_requires_attributable_license_provenance(self):
        valid = competitor()
        valid["reuse_mode"] = "direct-code-reuse"
        valid["license"] = "MIT"
        valid["license_source"] = "github-api"
        self.assertTrue(evaluate(competitor_research=research(valid))["ready"])

        for forged_license in ("UNKNOWN", "GPL-3.0"):
            item = deepcopy(valid)
            item["license"] = forged_license
            result = evaluate(competitor_research=research(item))
            self.assertIn(
                "no-blind-competitor-copying",
                {gate["id"] for gate in result["blockers"]},
            )
        for forged_source in ("UNKNOWN", "unverified"):
            item = deepcopy(valid)
            item["license_source"] = forged_source
            result = evaluate(competitor_research=research(item))
            self.assertIn(
                "no-blind-competitor-copying",
                {gate["id"] for gate in result["blockers"]},
            )

    def test_all_candidates_may_be_rejected_when_the_rejection_is_reviewed(self):
        item = competitor(accept=False, applied=False)
        item["bridge_status"] = "rejected"
        result = evaluate(
            competitor_research=research(item),
            applied_files=[],
            test_files=[],
            verification_passed=True,
        )
        self.assertTrue(result["ready"], result["blockers"])
        self.assertEqual(len(result["rejected_capabilities"]), 5)
        self.assertEqual(result["selected_capabilities"], [])

    def test_runtime_hard_wires_the_invariant_into_completion_and_release(self):
        runtime = Path(__file__).with_name("flexfactor.py")
        competitors = Path(__file__).with_name("flexfactor_competitors.py")
        if not runtime.exists() or not competitors.exists():
            self.skipTest("integration sources are not materialized in this isolated test copy")
        source = runtime.read_text(encoding="utf-8")
        market = competitors.read_text(encoding="utf-8")
        self.assertIn("evaluate_product_invariants(", source)
        self.assertIn("stamp_competitor_implementation(", source)
        self.assertIn("tests_by_source=tests_by_source", source)
        self.assertIn("tests_by_capability=tests_by_capability", source)
        self.assertIn("FLEXFACTOR_CAPABILITY:", source)
        self.assertIn("license_compatible=_license_compatible", source)
        self.assertIn('getattr(purpose_contract, "authored", False)', source)
        self.assertIn('research["unverified_files"] = outcome["unverified"]', source)
        self.assertIn("applied, unverified, notes = _fix_files(", source)
        self.assertIn('research["applied_files"] = outcome["applied"]', source)
        self.assertIn("execution_orchestrator.record_competitor_gate(", source)
        self.assertIn("target=_ff_execution.TOP_COMPETITORS", source)
        self.assertIn('and product_invariants.get("ready") is True', source)
        self.assertIn('a.get("product_invariants") or {}', source)
        self.assertIn('"product_invariants": product_invariants', source)
        for field in (
            "already_present",
            "risk_level",
            "risk_reason",
            "risk_mitigation",
            "wiring_plan",
            "verification_plan",
        ):
            self.assertIn(f'"{field}"', market)


if __name__ == "__main__":
    unittest.main()
