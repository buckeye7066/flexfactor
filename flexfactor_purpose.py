"""Purpose awareness for FlexFactor.

FlexFactor's old question was "how many defects does this repo have?". That
question has the same answer for every repo, which is exactly what the owner
objected to (2026-08-11):

    "FlexFactor needs to make sure it understands the purpose each app or
     program I place in it was created for, and must bridge the gap between
     where it is and that purpose."

and, from the owner's own portfolio directive (`memory/doctrine/`):

    "The goal is not to make every program resemble the same generic
     application. The goal is to make every program successfully perform the
     particular job it was created to perform."

So this module answers a different question: *what was this program built to do,
and what is still between it and doing that?* It supplies three things:

1. `PurposeContract` - the per-program record, whose SHAPE is the owner's
   "Purpose and Acceptance Contract" (master prompt section 5), not ours. It is
   loaded from `memory/purpose_contracts.json` (26 programs seeded verbatim from
   the owner's master prompts) or from the audited repo itself, and only
   inferred from README/CLAUDE.md when nothing authored exists. `authored`
   records which, because an inferred purpose is a guess and must never be
   reported as the owner's requirement.
2. The owner's STATUS VOCABULARY (section 4) and DEFINITION OF PRODUCTION READY
   (section 6), as enforcement code. The owner ruled that "tests pass", "build
   passes", "merged", "deployed", "health endpoint returns 200" and friends are
   NOT production ready. `production_ready_status()` will not return
   PRODUCTION READY without evidence for every applicable condition, and
   `forbidden_claims()` catches a report that tries to say otherwise. This is
   the direct fix for FlexFactor's silent-overclaim bug class.
3. `acceptance_coverage()` / `gap_progress()` - the arithmetic that turns a run
   into "closed N gaps toward the app's purpose" instead of "scored X".

Stdlib only, and it never imports flexfactor - same discipline as
flexfactor_prodready.py, so the tests can load it in isolation.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict

SCHEMA = "flexfactor.purpose_contracts.v1"

# Where the seeded registry lives, relative to this file.
REGISTRY_REL = os.path.join("memory", "purpose_contracts.json")

# An audited repo may carry its own authored contract. Checked in this order.
IN_REPO_CONTRACT_FILES = (
    ".flexfactor-purpose.json",
    os.path.join("docs", "purpose-contract.md"),
    "PURPOSE.md",
)


# --------------------------------------------------------------------------- #
# The owner's status vocabulary (master prompt section 4).
# --------------------------------------------------------------------------- #

#: The ONLY release statuses FlexFactor may report. "DONE" is deliberately absent
#: - the owner banned it outright.
STATUS_VOCABULARY = (
    "QUEUED",
    "IN PROGRESS",
    "BLOCKED",
    "RELEASE CANDIDATE",
    "PRODUCTION READY",
)

#: Phase labels for a run in flight (portfolio directive section 12). These
#: describe WHERE a run is, never whether it succeeded.
PHASE_VOCABULARY = (
    "INVENTORY", "AUDITING", "IMPLEMENTING", "TESTING", "REVIEWING",
    "MERGING", "DEPLOYING", "LIVE VERIFYING",
)

#: Claims the owner explicitly ruled are NOT equivalent to PRODUCTION READY.
#: Verbatim from master prompt section 4 plus portfolio directive section 2.
NOT_PRODUCTION_READY_CLAIMS = (
    "code complete",
    "software complete",
    "tests pass",
    "build passes",
    "builds successfully",
    "merged",
    "deployed",
    "mock ready",
    "demo ready",
    "beta ready",
    "documentation complete",
    "pending owner action",
    "external release blocker",
    "ready except for",
    "should work",
    "works locally",
    "pr opened",
    "pr approved without substantive review",
    "health endpoint returns 200",
    "green deployment",
    "passes a small test suite",
    "has an attractive interface",
    "readme claiming that it works",
)

#: The owner's Definition of Production Ready (master prompt section 6 /
#: portfolio directive section 7), as machine-checkable condition ids. Each is
#: (id, prose, is_critical). A condition with no evidence is UNKNOWN, and an
#: unknown critical condition BLOCKS - "an unevaluated property is not evidence
#: of safety" (the same four-valued rule flexfactor_prodready already uses).
PRODUCTION_READY_CONDITIONS = (
    ("purpose_fulfilled", "The core purpose is fully implemented and the "
     "purpose-defining journey produces the outcome the program exists to "
     "produce.", True),
    ("journeys_end_to_end", "Primary user journeys work end to end.", True),
    ("modes_behave", "Major roles, modes, controls and configuration choices "
     "materially change behavior as intended.", True),
    ("data_paths", "Production data paths are functional and protected.", True),
    ("authz", "Authentication and authorization are correct.", True),
    ("privacy_security", "Privacy and security controls are appropriate.", True),
    ("defects_resolved", "Critical and high-severity defects are resolved.", True),
    ("tests_pass", "Applicable tests pass, on full rather than selectively "
     "narrowed gates.", False),
    ("reviewed", "The complete release candidate received substantive review.", True),
    ("merged", "Required changes are merged to the verified default branch.", False),
    ("ci_on_sha", "CI passes on the exact final default-branch SHA.", False),
    ("sha_deployed", "The exact merge SHA is deployed, packaged, or installed.", False),
    ("release_identity", "Live or installed release identity is independently "
     "verified.", False),
    ("output_inspected", "The actual purpose-defining production journey was "
     "executed and its final output inspected.", True),
    ("observability", "Monitoring, logging and error reporting are operational "
     "and do not expose secrets.", False),
    ("recovery_docs", "Backup, rollback, upgrade, uninstall and recovery "
     "documentation exists and was tested where applicable.", False),
    ("claims_match", "Product claims match verified capabilities.", True),
    ("no_abandoned_work", "No production-required work is abandoned in another "
     "PR, branch, worktree, or local artifact.", False),
    ("user_understandable", "The application is understandable to its intended "
     "users without developer assistance.", False),
    ("no_external_gap", "No required credential, certificate, legal review, "
     "payment validation, or external production proof remains incomplete.", False),
)

#: Evidence values a condition may carry. Mirrors prodready's four-valued gates.
EVIDENCE_STATES = ("pass", "fail", "na", "unknown")


def slugify(text: str) -> str:
    """Lowercase hyphen slug. Matches flexfactor._slugify's behavior so contract
    slugs and audit lock/report slugs line up."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# --------------------------------------------------------------------------- #
# The contract record.
# --------------------------------------------------------------------------- #

@dataclass
class PurposeContract:
    """One program's reason for existing, in the owner's own schema.

    `purpose` and `acceptance_criteria` are the load-bearing fields: everything
    downstream (the gap prompt, the gap-to-criterion mapping, the readiness
    verdict) is derived from them. `authored` is the honesty flag - True means a
    human wrote this contract, False means FlexFactor guessed it from the repo,
    and the two are never allowed to look alike in a report.
    """

    name: str
    slug: str = ""
    purpose: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    repo: str | None = None
    default_branch: str | None = None
    local_path: str | None = None
    locator: str | None = None
    required_design: list[str] | None = None
    false_substitutes: list[str] = field(default_factory=list)
    #: True when a human authored this (owner registry / in-repo contract file).
    #: False when FlexFactor inferred it from README/CLAUDE.md/metadata.
    authored: bool = False
    #: Where it came from, for the report's provenance line.
    source: dict | None = None

    def __post_init__(self):
        if not self.slug:
            self.slug = slugify(self.name)

    def to_dict(self) -> dict:
        return asdict(self)

    def prompt_block(self, max_chars: int = 6000) -> str:
        """Render the contract for a model prompt.

        Numbered acceptance criteria matter: the gap assessor is required to cite
        `acceptance_ref` by these numbers, which is what makes a gap list
        purpose-derived and auditable instead of a generic lint list.
        """
        origin = ("AUTHORED BY THE OWNER - this is a requirement, not a guess."
                  if self.authored else
                  "INFERRED by FlexFactor from the repository - treat as a "
                  "working hypothesis, not the owner's stated requirement.")
        lines = [
            f"PROGRAM: {self.name}",
            f"CONTRACT ORIGIN: {origin}",
            "",
            "PURPOSE (what this program was created to do):",
            self.purpose or "(not stated)",
        ]
        if self.acceptance_criteria:
            lines += ["", "ACCEPTANCE CRITERIA (the program is not finished until "
                          "every one of these is true):"]
            lines += [f"  {i}. {c}" for i, c in enumerate(self.acceptance_criteria, 1)]
        if self.required_design:
            lines += ["", "REQUIRED DESIGN:"]
            lines += [f"  - {d}" for d in self.required_design]
        if self.false_substitutes:
            lines += ["", "THESE DO NOT COUNT AS FULFILLING THE PURPOSE:"]
            lines += [f"  - {d}" for d in self.false_substitutes]
        return "\n".join(lines)[:max_chars]


# --------------------------------------------------------------------------- #
# Loading.
# --------------------------------------------------------------------------- #

def registry_path(base_dir: str | None = None) -> str:
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, REGISTRY_REL)


def load_registry(path: str | None = None) -> dict:
    """Load the seeded contract registry. Returns {} when absent or unreadable -
    purpose awareness degrades to inference, it never breaks a run."""
    p = path or registry_path()
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("programs"), dict):
        return {}
    return doc["programs"]


def _match_keys(program_name: str, project_dir: str | None) -> list[str]:
    """Every slug worth trying for this program, most specific first."""
    keys = []
    for raw in (program_name, os.path.basename(project_dir.rstrip("\\/"))
                if project_dir else None):
        if not raw:
            continue
        s = slugify(raw)
        if s and s not in keys:
            keys.append(s)
        squashed = s.replace("-", "")
        if squashed and squashed not in keys:
            keys.append(squashed)
    return keys


def find_contract(program_name: str, project_dir: str | None = None,
                  registry: dict | None = None) -> PurposeContract | None:
    """Look up an AUTHORED contract for this program.

    Resolution order, most authoritative first:
      1. a contract file inside the audited repo (the program speaks for itself)
      2. the owner's seeded registry, by slug, then by alias, then by local_path
    Returns None when nothing authored is found; the caller then infers.
    """
    if project_dir:
        in_repo = contract_from_repo(project_dir, program_name)
        if in_repo is not None:
            return in_repo

    reg = load_registry() if registry is None else registry
    if not reg:
        return None
    keys = _match_keys(program_name, project_dir)

    # Exact slug.
    for k in keys:
        if k in reg:
            return _contract_from_registry(reg[k])
    # Alias.
    for entry in reg.values():
        alias_slugs = {slugify(a) for a in (entry.get("aliases") or [])}
        alias_slugs |= {s.replace("-", "") for s in list(alias_slugs)}
        if alias_slugs & set(keys):
            return _contract_from_registry(entry)
    # Same checkout on disk.
    if project_dir:
        want = os.path.normcase(os.path.abspath(project_dir))
        for entry in reg.values():
            lp = entry.get("local_path")
            if lp and os.path.normcase(os.path.abspath(lp)) == want:
                return _contract_from_registry(entry)
    return None


def _contract_from_registry(entry: dict) -> PurposeContract:
    return PurposeContract(
        name=entry.get("name") or entry.get("slug") or "(unnamed)",
        slug=entry.get("slug") or "",
        purpose=entry.get("purpose") or "",
        acceptance_criteria=list(entry.get("acceptance_criteria") or []),
        aliases=list(entry.get("aliases") or []),
        repo=entry.get("repo"),
        default_branch=entry.get("default_branch"),
        local_path=entry.get("local_path"),
        locator=entry.get("locator"),
        required_design=entry.get("required_design") or None,
        false_substitutes=list(entry.get("false_substitutes") or []),
        authored=True,
        source=entry.get("source") or {"doc": REGISTRY_REL, "authored_by": "owner"},
    )


def contract_from_repo(project_dir: str, program_name: str = "") -> PurposeContract | None:
    """Read an authored contract that lives inside the audited repo."""
    for rel in IN_REPO_CONTRACT_FILES:
        path = os.path.join(project_dir, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            continue
        if rel.endswith(".json"):
            try:
                data = json.loads(body)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            c = _contract_from_registry(data)
            if not c.name or c.name == "(unnamed)":
                c.name = program_name or os.path.basename(project_dir)
                c.slug = slugify(c.name)
            c.source = {"doc": rel, "authored_by": "repo"}
            return c
        parsed = parse_markdown_contract(body)
        if parsed and parsed.get("purpose"):
            return PurposeContract(
                name=parsed.get("name") or program_name or os.path.basename(project_dir),
                purpose=parsed["purpose"],
                acceptance_criteria=parsed.get("acceptance_criteria", []),
                false_substitutes=parsed.get("false_substitutes", []),
                authored=True,
                source={"doc": rel, "authored_by": "repo"},
            )
    return None


_MD_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_MD_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")


def parse_markdown_contract(text: str) -> dict:
    """Parse a `docs/purpose-contract.md`-style document.

    The owner's own FlexFactor contract uses `## Purpose`, `## Acceptance ...`,
    `## Forbidden substitutes`, so those are the sections understood here. Prose
    under Purpose is joined; list items under the other two become entries.
    """
    out: dict = {"acceptance_criteria": [], "false_substitutes": []}
    section = None
    purpose_lines: list[str] = []
    for raw in (text or "").splitlines():
        h = _MD_HEADING.match(raw)
        if h:
            title = h.group(1).lower()
            if title.startswith("purpose") and "contract" not in title:
                section = "purpose"
            elif title.startswith("acceptance"):
                section = "acceptance"
            elif "forbidden" in title or "substitute" in title:
                section = "forbidden"
            else:
                section = None
                # "# Purpose & Acceptance Contract - <Name>" carries the name.
                m = re.search(r"contract\s*[-—:]\s*(.+)$", h.group(1), re.I)
                if m and not out.get("name"):
                    out["name"] = m.group(1).strip()
            continue
        line = raw.strip()
        if not line or section is None:
            continue
        item = _MD_ITEM.match(raw)
        if section == "purpose":
            purpose_lines.append(re.sub(r"\*\*", "", line))
        elif section == "acceptance" and item:
            out["acceptance_criteria"].append(item.group(1))
        elif section == "forbidden":
            text_ = item.group(1) if item else line
            out["false_substitutes"].extend(
                s.strip().rstrip(".") for s in text_.split(",") if s.strip())
    out["purpose"] = " ".join(purpose_lines).strip()
    return out


def inferred_contract(program_name: str, purpose_text: str,
                      acceptance: list[str] | None = None) -> PurposeContract:
    """Wrap a model-inferred purpose. `authored` stays False so no report can
    present a guess as the owner's requirement."""
    return PurposeContract(
        name=program_name,
        purpose=purpose_text or "",
        acceptance_criteria=list(acceptance or []),
        authored=False,
        source={"doc": "(inferred from repository metadata)", "authored_by": "flexfactor"},
    )


# --------------------------------------------------------------------------- #
# Gap arithmetic: "closed N gaps toward the app's purpose".
# --------------------------------------------------------------------------- #

def normalize_gap(gap: dict, n_criteria: int) -> dict:
    """Clamp a model-produced gap into the shape the rest of the pipeline trusts.

    `acceptance_ref` is 1-based into the contract's criteria; anything out of
    range becomes None (an unattributed gap), never a wrong attribution.
    """
    g = dict(gap or {})
    ref = g.get("acceptance_ref")
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        ref = None
    if ref is not None and not (1 <= ref <= n_criteria):
        ref = None
    g["acceptance_ref"] = ref
    sev = str(g.get("severity", "")).lower()
    g["severity"] = sev if sev in ("critical", "high", "medium", "low") else "medium"
    g["code_fixable"] = bool(g.get("code_fixable"))
    g["file"] = str(g.get("file") or "")
    g["title"] = str(g.get("title") or "purpose gap")
    return g


def acceptance_coverage(contract: PurposeContract, gaps: list[dict]) -> list[dict]:
    """Per acceptance criterion: is it blocked, and by which gaps?

    This is the thing that makes the output purpose-derived. A generic linter
    cannot produce this table because it has no idea what the criteria are.
    """
    # UNATTRIBUTED gaps (acceptance_ref None - the model was told to emit 0
    # when a gap blocks the purpose but no single numbered criterion, and
    # normalize_gap maps 0/out-of-range to None) block the PURPOSE as a whole.
    # A criterion with no attributed gap is therefore only provably met when
    # there are also no unattributed gaps in play; otherwise it is UNKNOWN
    # (met=None). "Unevaluated is not evidence of safety" - without this, six
    # critical whole-purpose gaps used to score as 100% of criteria met.
    unattributed = [g for g in gaps if g.get("acceptance_ref") is None]
    rows = []
    for i, crit in enumerate(contract.acceptance_criteria, 1):
        blocking = [g for g in gaps if g.get("acceptance_ref") == i]
        worst = None
        for g in blocking:
            rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(
                str(g.get("severity", "")).lower(), 2)
            if worst is None or rank > worst[0]:
                worst = (rank, g.get("severity"))
        rows.append({
            "index": i,
            "criterion": crit,
            "met": (False if blocking else (None if unattributed else True)),
            "blocking_gaps": len(blocking),
            "unattributed_gaps": len(unattributed),
            "worst_severity": worst[1] if worst else None,
            "gap_titles": [g.get("title") for g in blocking],
        })
    return rows


def gap_progress(before: list[dict], closed_titles: list[str]) -> dict:
    """Summarize a run as movement toward the purpose, not as a score.

    Returns the numbers the report leads with: how many gaps existed, how many
    this run actually closed, and how many acceptance criteria became unblocked
    as a result.
    """
    closed = {str(t) for t in (closed_titles or [])}
    remaining = [g for g in before if str(g.get("title")) not in closed]
    refs_before = {g.get("acceptance_ref") for g in before
                   if g.get("acceptance_ref") is not None}
    refs_after = {g.get("acceptance_ref") for g in remaining
                  if g.get("acceptance_ref") is not None}
    return {
        "gaps_before": len(before),
        "gaps_closed": len(before) - len(remaining),
        "gaps_remaining": len(remaining),
        "criteria_blocked_before": len(refs_before),
        "criteria_unblocked": len(refs_before - refs_after),
        "criteria_blocked_after": len(refs_after),
    }


# --------------------------------------------------------------------------- #
# Status: the owner's vocabulary, enforced.
# --------------------------------------------------------------------------- #

def production_ready_status(evidence: dict | None,
                            *, has_open_gaps: bool = False,
                            blocked_reason: str | None = None) -> tuple[str, list[str]]:
    """Map condition evidence onto the owner's five-value status vocabulary.

    Rules, straight from the master prompt:
      * PRODUCTION READY requires every applicable condition to be `pass`. A
        critical condition that is `unknown` BLOCKS - unevaluated is not safe.
      * Any `fail`, or an open purpose gap, means BLOCKED / IN PROGRESS. There
        is no "ready except for".
      * RELEASE CANDIDATE is the strongest claim available when the software
        conditions pass but release-side proof (deploy, live verification) is
        still unknown - it is NOT a synonym for done.

    Returns (status, unmet) where `unmet` names every condition standing between
    the program and PRODUCTION READY, so a report can never assert readiness
    without also showing what it is missing.
    """
    ev = dict(evidence or {})
    unmet: list[str] = []
    failed = False
    critical_unknown = False
    release_unknown = False
    release_ids = {"merged", "ci_on_sha", "sha_deployed", "release_identity"}

    for cid, prose, is_critical in PRODUCTION_READY_CONDITIONS:
        state = str(ev.get(cid, "unknown")).lower()
        if state not in EVIDENCE_STATES:
            state = "unknown"
        if state in ("pass", "na"):
            continue
        unmet.append(cid)
        if state == "fail":
            failed = True
        elif is_critical:
            critical_unknown = True
        elif cid in release_ids:
            release_unknown = True

    if blocked_reason:
        return "BLOCKED", unmet
    if failed:
        return "BLOCKED", unmet
    if has_open_gaps:
        return "IN PROGRESS", unmet
    if critical_unknown:
        return "IN PROGRESS", unmet
    if not unmet:
        return "PRODUCTION READY", []
    if release_unknown:
        return "RELEASE CANDIDATE", unmet
    return "IN PROGRESS", unmet


def forbidden_claims(text: str) -> list[str]:
    """Find phrases a report used that the owner ruled are not production ready.

    Used as a tripwire on FlexFactor's own output: if a summary says a program is
    production ready *because* the build passed, that is the overclaim bug the
    owner is trying to kill, and this returns the offending phrases.
    """
    low = (text or "").lower()
    hits = []
    for claim in NOT_PRODUCTION_READY_CLAIMS:
        if claim in low and claim not in hits:
            hits.append(claim)
    return hits


def assert_status_vocabulary(status: str) -> str:
    """Raise on any status outside the owner's vocabulary. `DONE` is banned by
    name; so is anything invented."""
    s = (status or "").strip().upper()
    if s not in STATUS_VOCABULARY:
        raise ValueError(
            f"status {status!r} is not in the owner's vocabulary "
            f"{list(STATUS_VOCABULARY)} (master prompt section 4; 'DONE' is "
            "explicitly banned as a release status)")
    return s
