"""Fail-closed purpose and competitive-fit completion contract.

FlexFactor may report partial progress when research or verification is
unavailable, but it must not report convergence or production readiness.  This
module is deliberately pure and dependency-free so the same evidence can gate
the audit, the readiness card, and tests without another model opinion.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA = "flexfactor.product-invariants.v1"
KNOWN_REUSE_MODES = {
    "direct-code-reuse",
    "clean-room-from-documented-behavior",
    "reference-only",
}
MUTATING_REUSE_MODES = {
    "direct-code-reuse",
    "clean-room-from-documented-behavior",
}


def stamp_competitor_implementation(*, competitor_research: dict | None,
                                    applied_files: list[str] | set[str] | None,
                                    unverified_files: list[str] | set[str] | None,
                                    test_files: list[str] | set[str] | None,
                                    verification_passed: bool) -> dict | None:
    """Attach deterministic implementation evidence to every market decision.

    This is the hand-off between the real build/test loop and the policy gate.
    The model may propose a capability and a verification plan, but it cannot
    grant itself ``applied-and-build-verified``.  That status is derived only
    from the paths the transactional fixer changed, its unverified-path ledger,
    the executable tests the run produced, and the final quality-gate result.
    """
    if competitor_research is None:
        return None
    applied = {str(path).replace("\\", "/") for path in (applied_files or [])}
    unverified = {
        str(path).replace("\\", "/") for path in (unverified_files or [])
    }
    tests = sorted({
        str(path).replace("\\", "/") for path in (test_files or []) if str(path).strip()
    })
    for competitor in competitor_research.get("competitors") or []:
        idea = competitor.get("idea") or {}
        target = str(idea.get("file") or "").replace("\\", "/")
        target_applied = bool(target and target in applied)
        target_verified = bool(target_applied and target not in unverified)
        implementation_evidence = {
            "target_file": target,
            "target_applied": target_applied,
            "target_verified": target_verified,
            "test_files": tests,
            "verification_passed": bool(verification_passed),
        }
        competitor["implementation_evidence"] = implementation_evidence
        if not idea.get("accept"):
            status = "rejected-purpose-fit"
        elif idea.get("already_present") is True:
            status = "rejected-duplicate"
        elif not competitor.get("entered_fix_stream"):
            status = "not-selected"
        elif target_verified and tests and verification_passed:
            status = "applied-and-build-verified"
        elif target_applied:
            status = "applied-verification-incomplete"
        else:
            status = "not-applied"
        competitor["implementation_status"] = status
    competitor_research["implementation_evidence_version"] = SCHEMA
    return competitor_research


def _value(item: Any, key: str, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _utc_timestamp_is_current(value: Any, max_age_days: int = 30) -> bool:
    raw = _text(value)
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return age.total_seconds() >= -300 and age.days <= max_age_days


def _gate(gate_id: str, passed: bool, evidence: str, remediation: str) -> dict:
    return {
        "id": gate_id,
        "passed": bool(passed),
        "evidence": _text(evidence),
        "remediation": _text(remediation),
    }


def evaluate_product_invariants(*, purpose_enabled: bool,
                                purpose_contract: Any,
                                purpose_confidence: str,
                                purpose_before: dict | None,
                                purpose_after: dict | None,
                                purpose_errors: list[str] | None,
                                competitors_enabled: bool,
                                competitor_research: dict | None,
                                competitor_target: int = 5,
                                applied_files: list[str] | set[str] | None = None,
                                test_files: list[str] | set[str] | None = None,
                                verification_passed: bool | None = None) -> dict:
    """Return immutable, JSON-safe evidence and every blocking invariant.

    A competitor capability is selected only when its evidence is current and
    corroborated, its purpose fit and adoption risks were reviewed, it is not a
    duplicate, its reuse mode permits implementation, its target file entered
    the gated fix stream, the file actually changed, and verification passed.
    Rejected capabilities remain in the ledger with their reason; copying a
    product roadmap is never a completion condition.
    """
    gates: list[dict] = []
    confidence = _text(purpose_confidence).lower()
    authored = bool(_value(purpose_contract, "authored", False))
    authoritative = authored or confidence in {"owner-authored", "strongly-inferred"}
    gates.append(_gate(
        "purpose-authority",
        purpose_enabled and authoritative,
        f"enabled={bool(purpose_enabled)}; authored={authored}; confidence={confidence or 'missing'}",
        "Enable purpose analysis and supply an owner-authored contract or at least three independent, agreeing evidence families.",
    ))

    purpose_errors = [_text(item) for item in (purpose_errors or []) if _text(item)]
    assessments_complete = (
        purpose_enabled and purpose_before is not None and purpose_after is not None
        and not purpose_errors
    )
    gates.append(_gate(
        "purpose-assessment-complete",
        assessments_complete,
        f"baseline={purpose_before is not None}; final={purpose_after is not None}; errors={len(purpose_errors)}",
        "Resume with a responsive purpose reviewer until both baseline and final assessments complete without sampling errors.",
    ))

    final_gaps = list((purpose_after or {}).get("gaps") or [])
    criteria_total = int((purpose_after or {}).get("criteria_total") or 0)
    criteria_met = int((purpose_after or {}).get("criteria_met") or 0)
    criteria_unknown = int((purpose_after or {}).get("criteria_unknown") or 0)
    purpose_fulfilled = (
        assessments_complete and criteria_total > 0 and criteria_met == criteria_total
        and criteria_unknown == 0 and not final_gaps
    )
    gates.append(_gate(
        "purpose-fulfilled",
        purpose_fulfilled,
        f"criteria={criteria_met}/{criteria_total}; unknown={criteria_unknown}; open_gaps={len(final_gaps)}",
        "Implement and directly verify every remaining purpose criterion; unknown or whole-purpose gaps are not complete.",
    ))

    research = competitor_research or {}
    target = max(1, int(research.get("target") or competitor_target or 5))
    verified = int(research.get("verified") or 0)
    current = _utc_timestamp_is_current(research.get("researched_at"))
    gates.append(_gate(
        "competitive-research-current",
        competitors_enabled and bool(competitor_research) and current,
        f"enabled={bool(competitors_enabled)}; researched_at={_text(research.get('researched_at')) or 'missing'}",
        "Run live competitor research now; a disabled, missing, or stale market snapshot cannot certify competitive fit.",
    ))
    gates.append(_gate(
        "competitive-coverage",
        current and verified >= target,
        f"corroborated={verified}/{target}; {research.get('coverage_note') or ''}",
        "Corroborate the target number of real alternatives from reachable sources; never pad a shortfall with invented products.",
    ))

    competitors = list(research.get("competitors") or [])
    provenance_failures: list[str] = []
    review_failures: list[str] = []
    copying_failures: list[str] = []
    selected: list[dict] = []
    rejected: list[dict] = []
    applied = {str(path).replace("\\", "/") for path in (applied_files or [])}
    tests = {str(path).replace("\\", "/") for path in (test_files or [])}

    for competitor in competitors:
        name = _text(competitor.get("name")) or "(unnamed competitor)"
        evidence_urls = [
            _text(url) for url in (competitor.get("evidence_urls") or [])
            if _text(url).startswith(("https://", "http://"))
        ]
        if competitor.get("evidence_status") == "verified" and not evidence_urls:
            provenance_failures.append(f"{name}: verified without a source URL")
        reuse_mode = _text(competitor.get("reuse_mode"))
        if reuse_mode not in KNOWN_REUSE_MODES:
            copying_failures.append(f"{name}: unknown reuse mode {reuse_mode or '(missing)'}")
        if reuse_mode == "direct-code-reuse" and not _text(competitor.get("license")):
            copying_failures.append(f"{name}: direct reuse without a recorded license")

        idea = dict(competitor.get("idea") or {})
        accepted = bool(idea.get("accept"))
        risk_level = _text(idea.get("risk_level")).lower()
        risk_reason = _text(idea.get("risk_reason"))
        mitigation = _text(idea.get("risk_mitigation"))
        already_present = idea.get("already_present")
        required_review = {
            "idea_title": _text(idea.get("idea_title")),
            "what_it_does": _text(idea.get("what_it_does")),
            "why_valuable": _text(idea.get("why_valuable")),
            "purpose_reason": _text(idea.get("purpose_reason")),
            "evidence_basis": _text(idea.get("evidence_basis")),
            "wiring_plan": _text(idea.get("wiring_plan")),
            "verification_plan": _text(idea.get("verification_plan")),
            "risk_level": risk_level,
            "risk_reason": risk_reason,
        }
        missing = [key for key, value in required_review.items() if not value]
        if risk_level not in {"low", "medium", "high"}:
            missing.append("valid risk_level")
        if already_present not in {True, False}:
            missing.append("already_present decision")
        if accepted and risk_level in {"medium", "high"} and not mitigation:
            missing.append("risk_mitigation")
        if missing:
            review_failures.append(f"{name}: missing {', '.join(sorted(set(missing)))}")

        record = {
            "competitor": name,
            "capability": required_review["idea_title"],
            "source_urls": evidence_urls,
            "purpose_fit": required_review["purpose_reason"],
            "wiring_plan": required_review["wiring_plan"],
            "verification_plan": required_review["verification_plan"],
            "risk_level": risk_level,
            "risk_reason": risk_reason,
            "risk_mitigation": mitigation,
            "reuse_mode": reuse_mode,
            "file": str(idea.get("file") or "").replace("\\", "/"),
        }
        if accepted:
            selected.append(record)
        else:
            rejected.append({**record, "reason": required_review["purpose_reason"]})

    gates.append(_gate(
        "competitive-provenance",
        not provenance_failures,
        "; ".join(provenance_failures) or f"{verified} corroborated competitor record(s) carry source URLs",
        "Attach reachable source URLs to every corroborated competitor; model recollection is not provenance.",
    ))
    gates.append(_gate(
        "competitive-fit-risk-reviewed",
        bool(competitors) and not review_failures,
        "; ".join(review_failures) or f"{len(competitors)} capability decision(s) include fit, duplication, provenance, and risk review",
        "Complete the purpose-fit, duplication, evidence, and adoption-risk decision for every candidate before selecting features.",
    ))
    gates.append(_gate(
        "no-blind-competitor-copying",
        not copying_failures,
        "; ".join(copying_failures) or "Every competitor uses a provenance-bound direct, clean-room, or reference-only mode",
        "Resolve source ownership and license provenance; copy nothing from unknown/reference-only sources.",
    ))

    delivery_failures: list[str] = []
    for record in selected:
        competitor = next(
            (item for item in competitors if _text(item.get("name")) == record["competitor"]),
            {},
        )
        idea = dict(competitor.get("idea") or {})
        path = record["file"]
        if idea.get("already_present") is True:
            delivery_failures.append(f"{record['competitor']}: selected a duplicate capability")
        if competitor.get("evidence_status") != "verified":
            delivery_failures.append(f"{record['competitor']}: selected without corroboration")
        if record["reuse_mode"] not in MUTATING_REUSE_MODES:
            delivery_failures.append(f"{record['competitor']}: selected from reference-only evidence")
        if not bool(idea.get("code_fixable")) or not path:
            delivery_failures.append(f"{record['competitor']}: selected without an implementable target")
        if competitor.get("bridge_status") != "bridged" or not competitor.get("entered_fix_stream"):
            delivery_failures.append(f"{record['competitor']}: never entered the gated fix stream")
        if competitor.get("implementation_status") != "applied-and-build-verified":
            delivery_failures.append(
                f"{record['competitor']}: implementation was not applied and build-verified"
            )
        implementation = dict(competitor.get("implementation_evidence") or {})
        if implementation.get("target_file") != path:
            delivery_failures.append(
                f"{record['competitor']}: implementation evidence does not name {path}"
            )
        if implementation.get("target_applied") is not True:
            delivery_failures.append(
                f"{record['competitor']}: implementation evidence does not prove the target changed"
            )
        if implementation.get("target_verified") is not True:
            delivery_failures.append(
                f"{record['competitor']}: changed target remains unverified"
            )
        if not list(implementation.get("test_files") or []):
            delivery_failures.append(
                f"{record['competitor']}: no executable test artifact is attached"
            )
        if implementation.get("verification_passed") is not True:
            delivery_failures.append(
                f"{record['competitor']}: final executable verification did not pass"
            )
        if path and path not in applied:
            delivery_failures.append(f"{record['competitor']}: target {path} was not changed")
    if selected and not tests:
        delivery_failures.append(
            "selected competitor capabilities lack focused executable regression tests"
        )
    if selected and verification_passed is not True:
        delivery_failures.append("selected competitor capabilities lack a passing executable verification suite")
    gates.append(_gate(
        "selected-capabilities-delivered",
        not delivery_failures,
        "; ".join(delivery_failures) or (
            f"{len(selected)} selected capability/capabilities implemented and verified"
            if selected else
            f"No capability selected; {len(rejected)} candidate(s) explicitly rejected for fit/risk"
        ),
        "Implement, wire, and directly test each selected capability, or explicitly reject it for purpose fit, duplication, provenance, or risk.",
    ))

    blockers = [gate for gate in gates if not gate["passed"]]
    return {
        "schema": SCHEMA,
        "ready": not blockers,
        "gates": gates,
        "blockers": blockers,
        "selected_capabilities": selected,
        "rejected_capabilities": rejected,
    }


__all__ = [
    "SCHEMA",
    "evaluate_product_invariants",
    "stamp_competitor_implementation",
]
