"""Truthful execution-containment boundary for FlexFactor.

Installing dependencies and running a target repository's build/tests executes
third-party code. Environment filtering and command classification
(flexfactor_cmdpolicy / flexfactor_egress) are NOT an OS sandbox: they do not
contain filesystem, network, process tree, credentials, time, CPU, or memory.

Product boundary (enforced here):
  - Unattended install / build / test / lifecycle-adjacent execution is allowed
    ONLY for owner-approved trusted repositories listed in
    ~/.flexfactor/policy.json {"trusted_repos": [...]} or env
    FLEXFACTOR_TRUSTED_REPOS (path prefixes, semicolon-separated on Windows /
    colon-separated on POSIX when using the env form; JSON list preferred).
  - Unknown / untrusted trees are blocked BEFORE install/build/test.
  - Dependency lifecycle scripts remain OFF unless explicitly authorized
    (--allow-scripts / allow_scripts=True).
  - Frozen installs are required when a lockfile exists; lock drift is a finding,
    not an incidental bootstrap mutation.

Do not claim untrusted-code containment without this gate (or a real OS sandbox).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass


POLICY_PATH = os.path.join(os.path.expanduser("~"), ".flexfactor", "policy.json")


@dataclass
class TrustDecision:
    allowed: bool
    reason: str
    normalized_root: str = ""
    matched_rule: str | None = None
    policy_source: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "normalized_root": self.normalized_root,
            "matched_rule": self.matched_rule,
            "policy_source": self.policy_source,
        }


def _norm_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def load_trusted_repo_rules() -> tuple[list[str], str]:
    """Return (rules, source). Empty rules => no repo is trusted for unattended exec."""
    env = (os.environ.get("FLEXFACTOR_TRUSTED_REPOS") or "").strip()
    if env:
        sep = ";" if os.name == "nt" else (";" if ";" in env else ":")
        rules = [p.strip() for p in env.split(sep) if p.strip()]
        return rules, "env:FLEXFACTOR_TRUSTED_REPOS"
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], f"missing:{POLICY_PATH}"
    except Exception as ex:
        return [], f"unreadable:{POLICY_PATH}:{type(ex).__name__}"
    if not isinstance(data, dict):
        return [], f"invalid:{POLICY_PATH}"
    rules = data.get("trusted_repos") or data.get("trusted_repositories") or []
    if not isinstance(rules, list):
        return [], f"invalid_trusted_repos:{POLICY_PATH}"
    out = [str(r).strip() for r in rules if str(r).strip()]
    return out, POLICY_PATH


def trust_decision(project_dir: str, *,
                   allow_untrusted: bool = False) -> TrustDecision:
    """Decide whether unattended third-party install/build/test may run.

    allow_untrusted: explicit owner opt-in for a single run (CLI flag). Still
    logged; never the default.
    """
    root = _norm_path(project_dir)
    if allow_untrusted:
        return TrustDecision(
            allowed=True,
            reason="owner passed --trust-repo for this run "
                   "(NOT an OS sandbox; third-party code may run)",
            normalized_root=root,
            matched_rule="--trust-repo",
            policy_source="cli",
        )
    rules, source = load_trusted_repo_rules()
    if not rules:
        return TrustDecision(
            allowed=False,
            reason=("no trusted_repos configured — refusing unattended "
                    "install/build/test on an unknown tree. Add the path to "
                    f"~/.flexfactor/policy.json trusted_repos, set "
                    "FLEXFACTOR_TRUSTED_REPOS, or pass --trust-repo "
                    "(run-level; recorded in the manifest; not a sandbox)."),
            normalized_root=root,
            policy_source=source,
        )
    for rule in rules:
        base = _norm_path(os.path.expanduser(rule))
        if root == base or root.startswith(base + os.sep):
            return TrustDecision(
                allowed=True,
                reason=f"project is under trusted_repos rule {rule!r}",
                normalized_root=root,
                matched_rule=rule,
                policy_source=source,
            )
    return TrustDecision(
        allowed=False,
        reason=(f"project {root} is not under any trusted_repos rule "
                f"({len(rules)} rule(s) from {source}); refusing unattended "
                "install/build/test before third-party code can run"),
        normalized_root=root,
        policy_source=source,
    )


def require_trusted_for_exec(project_dir: str, *,
                             allow_untrusted: bool = False) -> TrustDecision:
    """Fail-closed helper: return decision; callers must refuse when not allowed."""
    return trust_decision(project_dir, allow_untrusted=allow_untrusted)


def lockfile_for_ecosystem(project_dir: str, ecosystem: str) -> str | None:
    """Return the lockfile basename that should freeze installs, if present."""
    mapping = {
        "node": ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
                 "npm-shrinkwrap.json"],
        "python": ["poetry.lock", "Pipfile.lock", "uv.lock", "requirements.txt"],
        "go": ["go.sum"],
        "rust": ["Cargo.lock"],
        "ruby": ["Gemfile.lock"],
        "php": ["composer.lock"],
        "dotnet": ["packages.lock.json"],
    }
    names = mapping.get(ecosystem, [])
    for name in names:
        if os.path.isfile(os.path.join(project_dir, name)):
            return name
    return None


def frozen_install_argv(ecosystem: str, manager: str, lockfile: str | None,
                        *, allow_scripts: bool = False) -> list[str] | None:
    """Build a frozen install command when a lockfile exists.

    Returns None when no frozen form is known (caller should treat lock drift /
    missing freeze as a finding rather than mutating casually).
    """
    mgr = (manager or "").lower()
    eco = (ecosystem or "").lower()
    if eco == "node" or mgr in ("npm", "pnpm", "yarn", "bun"):
        if mgr == "pnpm" or (lockfile or "").endswith("pnpm-lock.yaml"):
            cmd = ["pnpm", "install", "--frozen-lockfile"]
        elif mgr == "yarn" or (lockfile or "") == "yarn.lock":
            cmd = ["yarn", "install", "--frozen-lockfile"]
        elif mgr == "bun":
            cmd = ["bun", "install", "--frozen-lockfile"]
        else:
            # npm ci requires package-lock.json; otherwise refuse casual npm install
            if lockfile in ("package-lock.json", "npm-shrinkwrap.json"):
                cmd = ["npm", "ci"]
            else:
                return None
        if not allow_scripts:
            if cmd[0] == "npm":
                cmd.extend(["--ignore-scripts"])
            elif cmd[0] == "pnpm":
                cmd.extend(["--ignore-scripts"])
            elif cmd[0] == "yarn":
                cmd.extend(["--ignore-scripts"])
        return cmd
    if eco == "python":
        if lockfile == "poetry.lock":
            return ["poetry", "install", "--no-root", "--no-interaction"]
        if lockfile == "uv.lock":
            return ["uv", "sync", "--frozen"]
        return None
    if eco == "go" and lockfile == "go.sum":
        return ["go", "mod", "download"]
    if eco == "rust" and lockfile == "Cargo.lock":
        return ["cargo", "fetch", "--locked"]
    return None


def containment_claim() -> str:
    """Single honest product statement, delegated to the execution broker's
    measured capability report so docs/reports never drift from the code."""
    try:
        import flexfactor_sandbox as _sb
        return str(_sb.capability_report().get("claim") or "")
    except Exception:  # noqa: BLE001 - the fallback must still be truthful
        return ("FlexFactor could not probe an OS sandbox on this host. Command policy "
                "and egress filtering reduce risk but do not contain filesystem, "
                "network, process tree, credentials, time, CPU, or memory.")


if __name__ == "__main__":
    print(containment_claim(), file=sys.stderr)
