from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from flexfactor_product_invariants import (
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
    return {
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "verified": verified,
        "coverage_note": f"{verified}/{target}",
        "competitors": [item or competitor()],
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
    }
    args.update(over)
    return evaluate_product_invariants(**args)


class ProductInvariantTests(unittest.TestCase):
    def test_real_build_ledger_is_the_only_source_of_implementation_status(self):
        ledger = research(competitor(applied=False))
        ledger["competitors"][0]["bridge_status"] = "bridged"
        ledger["competitors"][0]["entered_fix_stream"] = True
        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=[],
            test_files=["tests/test_recovery.py"],
            verification_passed=True,
        )
        item = ledger["competitors"][0]
        self.assertEqual(item["implementation_status"], "applied-and-build-verified")
        self.assertEqual(
            item["implementation_evidence"],
            {
                "target_file": "src/recovery.py",
                "target_applied": True,
                "target_verified": True,
                "test_files": ["tests/test_recovery.py"],
                "verification_passed": True,
            },
        )

        stamp_competitor_implementation(
            competitor_research=ledger,
            applied_files=["src/recovery.py"],
            unverified_files=["src/recovery.py"],
            test_files=["tests/test_recovery.py"],
            verification_passed=True,
        )
        self.assertEqual(
            item["implementation_status"], "applied-verification-incomplete"
        )

    def test_complete_purpose_and_verified_selected_capability_pass(self):
        result = evaluate()
        self.assertTrue(result["ready"], result["blockers"])
        self.assertEqual(result["selected_capabilities"][0]["capability"], "Fast recovery")

    def test_open_or_unknown_purpose_never_converges(self):
        result = evaluate(purpose_after=purpose([{"title": "still open"}]))
        self.assertFalse(result["ready"])
        self.assertIn("purpose-fulfilled", {g["id"] for g in result["blockers"]})

    def test_disabled_or_incomplete_research_is_a_blocker(self):
        disabled = evaluate(competitors_enabled=False)
        self.assertFalse(disabled["ready"])
        thin = evaluate(competitor_research=research(verified=2, target=5))
        self.assertIn("competitive-coverage", {g["id"] for g in thin["blockers"]})

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
        self.assertEqual(len(result["rejected_capabilities"]), 1)

    def test_runtime_hard_wires_the_invariant_into_completion_and_release(self):
        runtime = Path(__file__).with_name("flexfactor.py")
        competitors = Path(__file__).with_name("flexfactor_competitors.py")
        if not runtime.exists() or not competitors.exists():
            self.skipTest("integration sources are not materialized in this isolated test copy")
        source = runtime.read_text(encoding="utf-8")
        market = competitors.read_text(encoding="utf-8")
        self.assertIn("evaluate_product_invariants(", source)
        self.assertIn("stamp_competitor_implementation(", source)
        self.assertIn("applied_c, unver_c, notes_c = _fix_files(", source)
        self.assertIn('competitor_research["applied_files"]', source)
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
