"""FlexFactor Scout production bridge (items 94-100).

Scout stays a FlexFactor *mode* with a separate command surface, risk model,
and report schema. Repo Rewards results are metadata-screened candidates only
— never "safe to install". Evaluation pins immutable commit SHAs, runs only
inside disposable sandboxes (credential-stripped, egress-controlled), and
emits integration proposals that require a separate FlexFactor apply approval
before the target app is mutated.

This module is stdlib-only and does not import flexfactor.py (avoids cycles).
Callers inject helpers (run, hermetic env, no-network env, rmtree) when needed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from typing import Any, Callable, Iterator

# --------------------------------------------------------------------------- #
# Bridge 94 — separate command / config / risk model / report schema
# --------------------------------------------------------------------------- #

SCOUT_MODE = "scout"
SCOUT_POLICY_FILE = ".flexfactor-scout-policy.json"
SCOUT_PROPOSAL_FILE = ".flexfactor-scout-proposals.json"
SCOUT_REPORT_JSON = "_scout_report.json"
FLEXFACTOR_APPLY_APPROVAL_FILE = ".flexfactor-apply-approval.json"

# Risk model: three independent verdicts. README/metadata never grants execute.
SCOUT_RISK_MODEL = {
    "name": "scout-three-verdict-v1",
    "verdicts": ("safe_to_inspect", "safe_to_integrate", "safe_to_execute"),
    "rules": (
        "Repo Rewards results are metadata-screened candidates only.",
        "Never label a candidate safe-to-install or safe-to-run from metadata.",
        "safe_to_execute is never granted automatically.",
        "Unknown license / missing safety / injection indicators fail closed.",
        "Target mutation requires a separate FlexFactor apply approval.",
    ),
    "false_substitutes": (
        "safe-to-install labels",
        "host credential access during candidate eval",
        "auto-merge into target",
    ),
}

METADATA_SCREENED_ONLY = True
FORBIDDEN_SAFETY_LABELS = (
    "safe-to-install",
    "safe to install",
    "safe-to-run",
    "safe to run",
    "safe_to_install",
    "safe_to_run",
)

# Structured report schema (bridge 94 + 99 fields).
SCOUT_REPORT_SCHEMA_VERSION = "scout-report-v1"
SCOUT_RECOMMENDATION_REQUIRED_FIELDS = (
    "commit_sha",
    "license",
    "security",
    "maintenance",
    "compatibility",
    "benefit",
    "integration_cost",
    "rejection_reason",
)

SCOUT_REPORT_SCHEMA = {
    "type": "object",
    "required": ["schema", "mode", "risk_model", "metadata_screened_only",
                 "program", "recommendations"],
    "properties": {
        "schema": {"const": SCOUT_REPORT_SCHEMA_VERSION},
        "mode": {"const": SCOUT_MODE},
        "risk_model": {"type": "object"},
        "metadata_screened_only": {"const": True},
        "program": {"type": "object"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": list(SCOUT_RECOMMENDATION_REQUIRED_FIELDS),
            },
        },
        "proposals": {"type": "array"},
        "sandbox": {"type": "object"},
    },
}

# Env var names stripped from candidate-eval sandboxes (no user credentials).
_CREDENTIAL_ENV_PREFIXES = (
    "AWS_", "AZURE_", "GCP_", "GOOGLE_", "GH_", "GITHUB_", "GITLAB_",
    "OPENAI_", "ANTHROPIC_", "HUGGING", "HF_", "NPM_", "NODE_AUTH",
    "TWILIO_", "STRIPE_", "SLACK_", "DISCORD_", "TELEGRAM_",
    "DATABASE_", "POSTGRES_", "MYSQL_", "MONGO_", "REDIS_",
    "DOCKER_", "KUBE", "HEROKU_", "RAILWAY_", "VERCEL_", "NETLIFY_",
    "SSH_", "PGPASSWORD", "PGUSER",
)
_CREDENTIAL_ENV_EXACT = {
    "API_KEY", "APIKEY", "ACCESS_TOKEN", "AUTH_TOKEN", "AUTHORIZATION",
    "TOKEN", "SECRET", "PASSWORD", "CREDENTIALS", "PRIVATE_KEY",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
}


def scout_config_banner() -> str:
    """Human-readable identity for Scout mode (separate from audit/refactor)."""
    return (
        f"FlexFactor Scout mode={SCOUT_MODE} "
        f"risk_model={SCOUT_RISK_MODEL['name']} "
        f"report_schema={SCOUT_REPORT_SCHEMA_VERSION} "
        f"metadata_screened_only={METADATA_SCREENED_ONLY}"
    )


def assert_no_safe_to_install_claim(text: str) -> list[str]:
    """Return forbidden phrases found in text (empty => clean)."""
    low = (text or "").lower()
    return [p for p in FORBIDDEN_SAFETY_LABELS if p in low]


# --------------------------------------------------------------------------- #
# Bridge 95 — metadata-only + commit pin + evidence fields
# --------------------------------------------------------------------------- #

_SHA_RX = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def is_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RX.match(value.strip()))


def extract_metadata_commit_sha(result: dict) -> str | None:
    """Best-effort SHA from Repo Rewards metadata (still must be verified by clone)."""
    if not isinstance(result, dict):
        return None
    repo = result.get("repo") or {}
    for key in ("commitSha", "commit_sha", "sha", "headSha", "defaultBranchSha",
                "pinnedSha", "revision"):
        for src in (result, repo, result.get("provenance") or {}):
            if isinstance(src, dict):
                val = src.get(key)
                if is_commit_sha(val):
                    return str(val).strip().lower()
    return None


def pin_fields_from_evidence(evaluation: dict) -> dict:
    """Normalize bridge-95 evidence fields onto an evaluation (in place + return).

    Always stamps metadata_screened_only=True and never invents a safe-to-install
    label. Commit SHA starts as metadata hint or 'unpinned' until clone confirms.
    """
    ev = evaluation.setdefault("evidence", {})
    if not isinstance(ev, dict):
        evaluation["evidence"] = {}
        ev = evaluation["evidence"]

    result = evaluation.get("result") or {}
    meta_sha = extract_metadata_commit_sha(result if isinstance(result, dict) else {})
    pinned = ev.get("commit_sha") or ev.get("pinned_commit_sha") or meta_sha
    if pinned and is_commit_sha(pinned):
        ev["commit_sha"] = str(pinned).strip().lower()
        ev["commit_pin_source"] = ev.get("commit_pin_source") or (
            "clone" if ev.get("clone_inspection_ok") else "metadata")
    else:
        ev["commit_sha"] = "unpinned"
        ev["commit_pin_source"] = "none"

    ev["metadata_screened_only"] = True
    ev["safe_to_install"] = False  # explicit denial — metadata is not install proof
    ev["provenance"] = ev.get("provenance") or (
        (result.get("repo") or {}).get("htmlUrl") if isinstance(result, dict) else None
    ) or "unknown"
    ev["last_activity"] = ev.get("last_activity") or "unknown"
    ev["advisories"] = ev.get("advisories", "unknown")
    if "transitive_risk" not in ev:
        ev["transitive_risk"] = _estimate_transitive_risk(ev)
    if "compatibility" not in ev:
        ev["compatibility"] = _estimate_compatibility(evaluation, ev)
    evaluation["evidence"] = ev
    return ev


def _estimate_transitive_risk(ev: dict) -> str:
    burden = str(ev.get("dependency_burden") or "").lower()
    scripts = str(ev.get("install_scripts") or "").lower()
    native = str(ev.get("native_build") or "").lower()
    flags = ev.get("execution_flags") or []
    if "unknown" in burden or "unknown" in scripts:
        return "unknown-until-sandbox-inspect"
    if flags or "present" in scripts or ("none" not in native and native):
        return "elevated"
    if "0 direct" in burden or burden.startswith("0 "):
        return "low"
    return "moderate"


def _estimate_compatibility(evaluation: dict, ev: dict) -> str:
    benefit = evaluation.get("benefit") or {}
    note = str(benefit.get("integration_note") or "").strip()
    if ev.get("license_compatible") is not True:
        return "incompatible-or-unverified-license"
    if note:
        return f"noted:{note[:120]}"
    if evaluation.get("recommendation") == "ADOPT":
        return "likely-compatible"
    if evaluation.get("recommendation") == "CONSIDER":
        return "situational"
    return "not-recommended"


def confirm_pin_from_clone(checkout_dir: str, evaluation: dict,
                           run: Callable | None = None) -> str | None:
    """Read immutable SHA from a hermetic checkout and stamp evidence."""
    ev = evaluation.setdefault("evidence", {})
    sha = None
    git_dir = os.path.join(checkout_dir, ".git")
    if os.path.isdir(git_dir) or os.path.isfile(git_dir):
        if run is not None:
            cp = run(["git", "-C", checkout_dir, "rev-parse", "HEAD"],
                     cwd=checkout_dir, timeout=60)
            out = (getattr(cp, "stdout", "") or "").strip()
            if getattr(cp, "returncode", 1) == 0 and is_commit_sha(out):
                sha = out.lower()
        else:
            # Fallback without injected runner: read .git/HEAD / loose ref.
            try:
                head = open(os.path.join(checkout_dir, ".git", "HEAD"),
                            encoding="utf-8").read().strip()
                if head.startswith("ref:"):
                    ref = head.split(" ", 1)[1].strip()
                    ref_path = os.path.join(checkout_dir, ".git", *ref.split("/"))
                    if os.path.isfile(ref_path):
                        sha = open(ref_path, encoding="utf-8").read().strip().lower()
                elif is_commit_sha(head):
                    sha = head.lower()
            except OSError:
                sha = None
    if sha and is_commit_sha(sha):
        ev["commit_sha"] = sha
        ev["pinned_commit_sha"] = sha
        ev["commit_pin_source"] = "clone"
        ev["clone_inspection_ok"] = ev.get("clone_inspection_ok", True)
        return sha
    # Fail closed: do not trust metadata alone as a pin after a clone attempt.
    if ev.get("commit_sha") and ev.get("commit_pin_source") == "metadata":
        ev["commit_sha"] = "unpinned"
        ev["commit_pin_source"] = "clone-failed"
    return None


# --------------------------------------------------------------------------- #
# Bridge 96 — disposable sandbox eval (credentials stripped, egress, teardown)
# --------------------------------------------------------------------------- #

def strip_credential_env(base: dict | None = None) -> dict:
    """Copy env with secrets / home / cloud tokens removed."""
    src = dict(base if base is not None else os.environ)
    out = {}
    for k, v in src.items():
        ku = k.upper()
        if ku in _CREDENTIAL_ENV_EXACT:
            continue
        if any(ku.startswith(p) for p in _CREDENTIAL_ENV_PREFIXES):
            continue
        if "SECRET" in ku or "PASSWORD" in ku or "TOKEN" in ku or "API_KEY" in ku:
            continue
        out[k] = v
    # Force a disposable HOME so tools do not read ~/.npmrc / ~/.gitconfig secrets.
    out["HOME"] = tempfile.gettempdir()
    out["USERPROFILE"] = tempfile.gettempdir()
    out["GIT_TERMINAL_PROMPT"] = "0"
    out["GIT_CONFIG_NOSYSTEM"] = "1"
    out["GIT_CONFIG_GLOBAL"] = os.devnull
    out["GIT_CONFIG_COUNT"] = "0"
    return out


def sandbox_eval_env(no_network_env: Callable[[], dict] | None = None) -> dict:
    """Credential-stripped + best-effort no-network env for candidate execution."""
    if no_network_env is not None:
        env = no_network_env()
    else:
        dead = "http://127.0.0.1:9"
        env = dict(os.environ)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env[k] = dead
        env["NO_PROXY"] = ""
        env["no_proxy"] = ""
        env.update({"npm_config_offline": "true", "npm_config_registry": dead,
                    "npm_config_fund": "false", "npm_config_audit": "false"})
    return strip_credential_env(env)


@contextmanager
def disposable_sandbox(prefix: str = "ffscout-sandbox-") -> Iterator[str]:
    """Create a temp dir and always tear it down (bridge 96 clean teardown)."""
    root = tempfile.mkdtemp(prefix=prefix)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_sandboxed_candidate_eval(
    clone_url: str,
    *,
    commit_sha: str | None = None,
    run: Callable,
    hermetic_git_env: Callable[[], dict],
    no_network_env: Callable[[], dict] | None = None,
    eval_argv: list[str] | None = None,
    timeout: int = 120,
    resource_limits: dict | None = None,
) -> dict:
    """Clone+optionally execute a candidate ONLY inside a disposable sandbox.

    - Network egress: poisoned proxy / npm offline (via sandbox_eval_env)
    - No user credentials: strip_credential_env
    - Bounded resources: timeout + optional max output size marker
    - Clean teardown: disposable_sandbox
    - Never touches the host project directory
    """
    limits = resource_limits or {}
    max_output = int(limits.get("max_output_bytes", 200_000))
    report: dict[str, Any] = {
        "ok": False,
        "commit_sha": commit_sha or "unpinned",
        "sandbox": True,
        "credentials_stripped": True,
        "egress_controlled": True,
        "teardown": "pending",
        "stdout": "",
        "stderr": "",
        "error": None,
    }
    if not (clone_url or "").lower().startswith("https://"):
        report["error"] = "refused: non-https clone url"
        report["teardown"] = "n/a"
        return report

    # Bound: refuse obviously host-escaping argv BEFORE any clone/exec.
    if eval_argv:
        joined = " ".join(str(a) for a in eval_argv).lower()
        if ".." in joined or ":\\" in joined or "/etc/" in joined or ":/" in joined.replace("https://", ""):
            # Allow the https clone url separately; argv itself must stay relative.
            bad = False
            for a in eval_argv:
                s = str(a)
                sl = s.lower()
                if ".." in s or re.match(r"^[a-z]:\\", sl) or sl.startswith("/etc/"):
                    bad = True
                    break
            if bad:
                report["error"] = "refused: eval argv looks host-escaping"
                report["teardown"] = "n/a"
                return report

    with disposable_sandbox() as root:
        dest = os.path.join(root, "candidate")
        try:
            env = hermetic_git_env()
            env = strip_credential_env(env)
            cp = run(
                ["git", "-c", "protocol.allow=never",
                 "-c", "protocol.https.allow=always",
                 "clone", "--depth", "1", "--no-tags", "--no-checkout",
                 "--", clone_url, dest],
                cwd=root, timeout=timeout, env=env,
            )
            if getattr(cp, "returncode", 1) != 0:
                report["error"] = f"clone failed rc={getattr(cp, 'returncode', '?')}"
                return report
            # Materialize HEAD (still no smudge filters beyond checkout of blobs).
            cp2 = run(["git", "-C", dest, "checkout", "--force", "HEAD"],
                      cwd=dest, timeout=60, env=env)
            if getattr(cp2, "returncode", 1) != 0:
                report["error"] = "checkout failed"
                return report
            pinned = confirm_pin_from_clone(dest, {"evidence": {}}, run=run)
            if pinned:
                report["commit_sha"] = pinned
            if commit_sha and pinned and not pinned.startswith(commit_sha[:7]):
                # Requested pin mismatch — fail closed.
                report["error"] = (
                    f"pin mismatch: requested {commit_sha} got {pinned}")
                return report
            if eval_argv:
                senv = sandbox_eval_env(no_network_env)
                cp3 = run(eval_argv, cwd=dest, timeout=timeout, env=senv)
                report["stdout"] = (getattr(cp3, "stdout", "") or "")[:max_output]
                report["stderr"] = (getattr(cp3, "stderr", "") or "")[:max_output]
                report["ok"] = getattr(cp3, "returncode", 1) == 0
                if not report["ok"]:
                    report["error"] = f"eval rc={getattr(cp3, 'returncode', '?')}"
            else:
                report["ok"] = True  # clone+pin only
        finally:
            report["teardown"] = "complete"
    report["sandbox_residual"] = False  # disposed by context manager
    report["host_project_untouched"] = True
    return report


# --------------------------------------------------------------------------- #
# Bridge 97 — integration proposal (delta / conflict / rollback) + apply gate
# --------------------------------------------------------------------------- #

def build_integration_proposal(
    evaluation: dict,
    *,
    project_dir: str | None = None,
    packages: list | None = None,
    files_planned: list | None = None,
) -> dict:
    """Structured proposal — never mutates the target by itself."""
    pin_fields_from_evidence(evaluation)
    ev = evaluation.get("evidence") or {}
    v = evaluation.get("verdicts") or {}
    b = evaluation.get("benefit") or {}
    repo = evaluation.get("repo") or (evaluation.get("result") or {}).get("repo") or {}

    packages = list(packages or [])
    files_planned = list(files_planned or [])
    deps_before, deps_after_estimate = _dependency_delta_estimate(
        project_dir, packages)

    conflicts = analyze_conflicts(project_dir, files_planned, packages)
    rejection = _rejection_reason(evaluation, ev, v)

    benefit_score = b.get("benefit_score")
    integration_cost = _integration_cost(packages, files_planned, conflicts, ev)

    proposal = {
        "schema": "scout-proposal-v1",
        "repo": repo.get("fullName") or ev.get("repo") or "(unknown)",
        "url": repo.get("htmlUrl") or ev.get("provenance"),
        "need": evaluation.get("need"),
        "recommendation": evaluation.get("recommendation"),
        "commit_sha": ev.get("commit_sha") or "unpinned",
        "commit_pin_source": ev.get("commit_pin_source"),
        "metadata_screened_only": True,
        "safe_to_install": False,
        "license": ev.get("license"),
        "license_compatible": ev.get("license_compatible"),
        "security": {
            "safety_verdict": ev.get("safety_verdict"),
            "advisories": ev.get("advisories"),
            "injection_flags": ev.get("injection_flags") or [],
            "execution_flags": ev.get("execution_flags") or [],
            "verdicts": v,
        },
        "maintenance": {
            "last_activity": ev.get("last_activity"),
            "stars": ev.get("stars"),
            "language": ev.get("language"),
        },
        "compatibility": ev.get("compatibility"),
        "transitive_risk": ev.get("transitive_risk"),
        "benefit": {
            "score": benefit_score,
            "how_it_helps": b.get("how_it_helps") or b.get("rationale"),
            "integration_note": b.get("integration_note"),
            "risks": b.get("risks") or [],
        },
        "integration_cost": integration_cost,
        "dependency_delta": {
            "packages_requested": packages,
            "deps_before_count": len(deps_before),
            "deps_added_estimate": [p for p in packages if p.split("@")[0] not in deps_before],
            "deps_already_present": [p for p in packages if p.split("@")[0] in deps_before],
            "deps_after_estimate_count": len(deps_after_estimate),
        },
        "conflict_analysis": conflicts,
        "rollback_plan": (
            ev.get("rollback_plan")
            or "dedicated flexfactor/adopt-* branch; build-gated; hard reset on failure; "
               "proposal-only until FlexFactor apply approval"
        ),
        "rejection_reason": rejection,
        "requires_flexfactor_apply_approval": True,
        "target_mutation_allowed": False,
    }
    evaluation["proposal"] = proposal
    return proposal


def _dependency_delta_estimate(project_dir: str | None, packages: list) -> tuple[set, set]:
    before: set[str] = set()
    if project_dir and os.path.isdir(project_dir):
        pkg_path = os.path.join(project_dir, "package.json")
        try:
            with open(pkg_path, encoding="utf-8") as f:
                data = json.load(f)
            for section in ("dependencies", "devDependencies", "optionalDependencies",
                            "peerDependencies"):
                block = data.get(section) or {}
                if isinstance(block, dict):
                    before.update(block.keys())
        except (OSError, ValueError):
            pass
    names = {_npm_name(str(p)) for p in packages}
    after = set(before) | {n for n in names if n}
    return before, after


def _npm_name(spec: str) -> str:
    s = spec.strip()
    if s.startswith("@"):
        # @scope/name or @scope/name@version -> @scope/name
        body = s[1:]
        if "@" in body:
            body = body.rsplit("@", 1)[0]
        return "@" + body
    return s.split("@", 1)[0]


def analyze_conflicts(project_dir: str | None, files_planned: list,
                      packages: list) -> dict:
    overlapping_files = []
    missing_project = project_dir is None or not os.path.isdir(project_dir or "")
    if project_dir and os.path.isdir(project_dir):
        for rel in files_planned:
            if not isinstance(rel, str) or not rel.strip():
                continue
            # Normalize crude path; do not follow symlinks for existence probe.
            full = os.path.normpath(os.path.join(project_dir, rel))
            try:
                if os.path.commonpath([os.path.abspath(project_dir),
                                       os.path.abspath(full)]) != os.path.abspath(project_dir):
                    overlapping_files.append({"path": rel, "issue": "escapes-project"})
                    continue
            except ValueError:
                overlapping_files.append({"path": rel, "issue": "invalid-path"})
                continue
            if os.path.lexists(full):
                overlapping_files.append({"path": rel, "issue": "would-overwrite"})
    return {
        "missing_project": missing_project,
        "overlapping_files": overlapping_files,
        "package_count": len(packages),
        "conflict_likely": bool(overlapping_files),
        "notes": (
            ["no local project dir — proposal only"] if missing_project else
            (["file overlaps require owner review"] if overlapping_files else
             ["no file overlaps detected"])
        ),
    }


def _integration_cost(packages, files_planned, conflicts, ev) -> str:
    score = 1
    score += min(3, len(packages))
    score += min(3, len(files_planned))
    if conflicts.get("conflict_likely"):
        score += 2
    if str(ev.get("transitive_risk") or "").startswith("elevated"):
        score += 2
    if str(ev.get("native_build") or "").lower() not in ("", "none detected", "unknown until inspected"):
        if "none" not in str(ev.get("native_build") or "").lower():
            score += 2
    if score <= 2:
        return "low"
    if score <= 5:
        return "moderate"
    return "high"


def _rejection_reason(evaluation, ev, v) -> str | None:
    if evaluation.get("recommendation") == "SKIP":
        return (evaluation.get("benefit") or {}).get("rationale") or "judged unnecessary"
    if v.get("safe_to_integrate") is not True:
        reasons = v.get("reasons") or []
        return "; ".join(reasons) if reasons else "safe_to_integrate is not True"
    if ev.get("commit_sha") in (None, "", "unpinned"):
        return "commit SHA not pinned — refuse auto-apply"
    return None


def flexfactor_apply_approved(project_dir: str | None,
                            proposal: dict | None = None) -> bool:
    """Bridge 100 gate: target mutation only with a separate FlexFactor approval file.

    Approval file schema (owner / FlexFactor apply run writes this):
      {
        "approved": true,
        "commit_sha": "<pinned sha>",
        "repo": "owner/name",
        "approver": "flexfactor-apply"
      }
    """
    if not project_dir or not os.path.isdir(project_dir):
        return False
    path = os.path.join(project_dir, FLEXFACTOR_APPLY_APPROVAL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("approved") is not True:
        return False
    if str(data.get("approver") or "") not in ("flexfactor-apply", "owner", "flexfactor"):
        return False
    if proposal:
        want = str(proposal.get("commit_sha") or "")
        got = str(data.get("commit_sha") or "")
        if want and want != "unpinned" and got:
            if not (got.lower() == want.lower() or want.lower().startswith(got.lower())
                    or got.lower().startswith(want.lower()[:12])):
                return False
        repo = str(proposal.get("repo") or "")
        if repo and data.get("repo") and str(data["repo"]) != repo:
            return False
    return True


def scout_may_mutate_target(args, project_dir: str | None, proposal: dict) -> tuple[bool, str]:
    """Decide whether Scout is allowed to mutate the target app.

    Production contract (bridge 97/100): proposals always; mutation only when a
    separate FlexFactor apply approval is present. Legacy `--apply` without
    approval writes proposals and refuses mutation (fail closed).
    """
    if not getattr(args, "apply", False):
        return False, "report-only (no --apply)"
    if getattr(args, "legacy_inline_apply", False):
        # Explicit break-glass for characterization tests / owner override.
        return True, "legacy-inline-apply override"
    if flexfactor_apply_approved(project_dir, proposal):
        return True, "flexfactor-apply-approval present"
    return False, (
        "refused: Scout emits proposals only until a separate FlexFactor apply "
        f"approval ({FLEXFACTOR_APPLY_APPROVAL_FILE}) is present"
    )


# --------------------------------------------------------------------------- #
# Report assembly (94 + 99)
# --------------------------------------------------------------------------- #

def recommendation_record(evaluation: dict) -> dict:
    """One commit-pinned recommendation row for the structured report."""
    proposal = evaluation.get("proposal") or build_integration_proposal(evaluation)
    ev = evaluation.get("evidence") or {}
    b = proposal.get("benefit") or {}
    return {
        "repo": proposal.get("repo"),
        "url": proposal.get("url"),
        "need": proposal.get("need"),
        "recommendation": proposal.get("recommendation"),
        "commit_sha": proposal.get("commit_sha") or "unpinned",
        "license": proposal.get("license"),
        "security": proposal.get("security"),
        "maintenance": proposal.get("maintenance"),
        "compatibility": proposal.get("compatibility"),
        "benefit": b,
        "integration_cost": proposal.get("integration_cost"),
        "rejection_reason": proposal.get("rejection_reason"),
        "transitive_risk": proposal.get("transitive_risk"),
        "metadata_screened_only": True,
        "safe_to_install": False,
        "advisories": ev.get("advisories"),
        "last_activity": (proposal.get("maintenance") or {}).get("last_activity"),
        "provenance": ev.get("provenance"),
    }


def build_scout_structured_report(
    profile_name: str,
    profile: dict,
    evaluations: list[dict],
    *,
    proposals: list[dict] | None = None,
    sandbox_summary: dict | None = None,
    scout_analysis: dict | None = None,
) -> dict:
    """Assemble a schema-versioned Scout report (never claims safe-to-install)."""
    recs = []
    props = list(proposals or [])
    for e in evaluations:
        pin_fields_from_evidence(e)
        if "proposal" not in e:
            build_integration_proposal(e)
        recs.append(recommendation_record(e))
        if e.get("proposal"):
            props.append(e["proposal"])
    report = {
        "schema": SCOUT_REPORT_SCHEMA_VERSION,
        "mode": SCOUT_MODE,
        "risk_model": SCOUT_RISK_MODEL,
        "metadata_screened_only": True,
        "safe_to_install_claims": False,
        "program": {
            "name": profile_name,
            "summary": (profile or {}).get("summary"),
            "stack": (profile or {}).get("stack") or [],
            "goals": (profile or {}).get("goals") or [],
            "users": (profile or {}).get("users") or [],
            "workflows": (profile or {}).get("workflows") or [],
            "capabilities": (profile or {}).get("capabilities") or [],
            "optimization_needs": (profile or {}).get("optimization_needs") or [],
            "coverage_gaps": (profile or {}).get("coverage_gaps") or [],
        },
        "recommendations": recs,
        "proposals": props,
        # THE DEFAULT DESCRIBES WHAT ACTUALLY HAPPENED. The single production
        # call site does not pass `sandbox_summary`, and the live enrichment
        # path (enrich_evidence_from_clone) executes no candidate code at all -
        # the code that would make an egress claim true (sandbox_eval_env /
        # run_sandboxed_candidate_eval) is not on it. Hardcoding a
        # "proxy-poisoned" posture therefore asserted a control this run never
        # applied, in every _scout_report.json. A caller that really does
        # sandbox an execution passes its own summary.
        "sandbox": sandbox_summary or {
            "candidate_execution": "not-executed",
            "credentials": "not-applicable (no candidate code was run)",
            "egress": "not-controlled (no candidate code was run)",
            "teardown": "not-applicable (no candidate code was run)",
        },
        "apply_contract": {
            "scout_mutates_target": False,
            "requires_flexfactor_apply_approval": True,
            "approval_file": FLEXFACTOR_APPLY_APPROVAL_FILE,
        },
    }
    if scout_analysis is not None:
        report["program_comparison"] = scout_analysis
    # Self-check: recommendation narratives must not claim safe-to-install.
    # Policy rules may mention the forbidden phrases (to forbid them); those
    # are excluded from the narrative scan. Boolean denial fields are also OK.
    narrative = json.dumps([
        {k: r.get(k) for k in ("benefit", "rejection_reason", "compatibility",
                               "how_it_helps", "integration_note")}
        for r in recs
    ], default=str)
    report["forbidden_label_hits"] = assert_no_safe_to_install_claim(narrative)
    return report


def write_scout_artifacts(base_dir: str, structured: dict,
                          proposals: list[dict] | None = None,
                          *, artifact_slug: str | None = None) -> dict:
    """Write JSON report + proposals next to the program (reversible artifacts)."""
    os.makedirs(base_dir, exist_ok=True)
    clean_slug = re.sub(r"[^a-z0-9._-]+", "-", str(artifact_slug or "").lower()).strip("-.")
    clean_slug = clean_slug[:120]
    if clean_slug:
        report_name = f"_scout_report.{clean_slug}.json"
        proposal_name = f".flexfactor-scout-proposals.{clean_slug}.json"
    else:
        report_name = SCOUT_REPORT_JSON
        proposal_name = SCOUT_PROPOSAL_FILE
    report_path = os.path.join(base_dir, report_name)
    proposal_path = os.path.join(base_dir, proposal_name)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(structured, f, indent=2, default=str)
        f.write("\n")
    payload = {
        "schema": "scout-proposals-v1",
        "metadata_screened_only": True,
        "requires_flexfactor_apply_approval": True,
        "proposals": proposals if proposals is not None else structured.get("proposals") or [],
    }
    with open(proposal_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
        f.write("\n")
    return {"report_json": report_path, "proposals_json": proposal_path}


def malicious_fixture_escape_probe(run: Callable,
                                   no_network_env: Callable[[], dict] | None = None,
                                   ) -> dict:
    """Bridge 98 helper: malicious fixture must not see host credentials.

    Env-level sandbox (ISOLATION_SPIKE option A): credential strip + poisoned
    proxy. Not an OS filesystem jail (AppContainer is the airtight successor);
    this probe asserts the contract we *do* enforce today: canary secrets from
    the parent env do not appear in the child, HOME is redirected to temp, and
    host-escaping eval argv is refused by run_sandboxed_candidate_eval.
    """
    parent_canary = {"OPENAI_API_KEY": "sk-test-SHOULD-NOT-LEAK",
                     "AWS_SECRET_ACCESS_KEY": "leakme",
                     "ANTHROPIC_API_KEY": "sk-ant-SHOULD-NOT-LEAK"}
    saved = {k: os.environ.get(k) for k in parent_canary}
    for k, v in parent_canary.items():
        os.environ[k] = v
    try:
        senv = sandbox_eval_env(no_network_env)
        with disposable_sandbox(prefix="ffscout-malicious-") as root:
            fixture = os.path.join(root, "malicious")
            os.makedirs(fixture)
            script = os.path.join(fixture, "probe.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(
                    "import os, json\n"
                    "want = ('OPENAI_API_KEY','AWS_SECRET_ACCESS_KEY','ANTHROPIC_API_KEY')\n"
                    "found = {k: os.environ.get(k) for k in want if k in os.environ}\n"
                    "home = os.environ.get('HOME') or os.environ.get('USERPROFILE')\n"
                    "proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')\n"
                    "print(json.dumps({'found': found, 'home': home, 'proxy': proxy}))\n"
                )
            cp = run(["python", script], cwd=fixture, timeout=30, env=senv)
            out = (getattr(cp, "stdout", "") or "").strip()
            try:
                data = json.loads(out.splitlines()[-1]) if out else {}
            except ValueError:
                data = {"parse_error": out[:500]}
        leaked = list((data.get("found") or {}).keys())
        home = data.get("home") or ""
        home_ok = os.path.normcase(os.path.abspath(home)) == os.path.normcase(
            os.path.abspath(tempfile.gettempdir())) if home else False
        proxy = str(data.get("proxy") or "")
        egress_ok = "127.0.0.1:9" in proxy
        # Escaping argv must be refused by the sandbox runner policy.
        refuse = run_sandboxed_candidate_eval(
            "https://example.com/x/y",
            run=run,
            hermetic_git_env=lambda: strip_credential_env(),
            no_network_env=no_network_env,
            eval_argv=["python", "..\\..\\..\\Windows\\System32\\notepad.exe"],
            timeout=5,
        )
        argv_refused = "host-escaping" in str(refuse.get("error") or "")
        return {
            "escaped": bool(leaked),
            "leaked_canaries": leaked,
            "home_redirected": home_ok,
            "egress_poisoned": egress_ok,
            "host_escaping_argv_refused": argv_refused,
            "sandbox_env_had_canary": any(k in senv for k in parent_canary),
            "ok": (not leaked) and home_ok and egress_ok and argv_refused
                  and not any(k in senv for k in parent_canary),
        }
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
