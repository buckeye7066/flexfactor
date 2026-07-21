"""Command classification + policy gate for FlexFactor's subprocess chokepoint.

Every subprocess FlexFactor launches goes through flexfactor._run, which calls
`command_allowed` before executing. Commands are classified into behavior
classes; the HIGH-RISK classes (destructive / credentialed / deploy) are DENIED
by default and require an explicit policy opt-in. Everything FlexFactor
legitimately runs today (git workflow incl. rollback, npm/yarn/pnpm
build/test/install, npx playwright, node, python) stays allowed, so this gate
is additive safety, not a behavior change.

Policy opt-in, either of:
  * env FLEXFACTOR_ALLOW_CLASSES="deploy,destructive"  (comma-separated)
  * ~/.flexfactor/policy.json  {"allow_classes": ["deploy", ...]}

Fail-closed direction: an unknown command is allowed (it is what the tool did
before this module existed, and the audit runs arbitrary project test runners),
but a command that MATCHES a high-risk signature is blocked even when it also
matches a benign class. Classification of an empty/malformed command is
{"unknown"} and allowed (the launch will fail on its own in _run).

This module is deliberately standalone (stdlib-only, no import of flexfactor)
so it is unit-testable in isolation and reusable by scout/audit/refactor alike.
"""
from __future__ import annotations

import json
import os

# The full class vocabulary (for reports/telemetry).
ALL_CLASSES = frozenset({
    "read_only", "vcs", "build", "test", "install", "network",
    "destructive", "credentialed", "deploy", "unknown",
})
# Denied unless a policy explicitly allows them.
HIGH_RISK = frozenset({"destructive", "credentialed", "deploy"})

_GIT_READ = {"status", "log", "diff", "show", "ls-files", "rev-parse",
             "branch", "config", "remote", "describe", "check-ignore"}
_GIT_NETWORK = {"push", "fetch", "pull", "clone", "ls-remote"}

_DEPLOY_EXES = {"vercel", "railway", "netlify", "flyctl", "fly", "heroku",
                "kubectl", "helm", "terraform", "pulumi", "serverless", "sls",
                "wrangler", "firebase", "eb", "cdk", "twine"}
_CREDENTIALED_EXES = {"aws", "az", "gcloud", "ssh", "scp", "sftp", "op",
                      "vault", "doppler", "pass", "keytool"}
_DESTRUCTIVE_EXES = {"format", "mkfs", "diskpart", "shutdown", "reboot",
                     "fdisk", "dd"}


def _exe_name(cmd: list[str]) -> str:
    if not cmd or not cmd[0]:
        return ""
    base = os.path.basename(str(cmd[0]))
    return os.path.splitext(base)[0].lower()


def _has_flag(args: list[str], *flags: str) -> bool:
    low = [str(a).lower() for a in args]
    return any(f in low for f in flags)


def classify_command(cmd: list[str]) -> set[str]:
    """Classify a command line into behavior classes. Pure + deterministic."""
    exe = _exe_name(cmd)
    args = [str(a) for a in (cmd[1:] if cmd else [])]
    if not exe:
        return {"unknown"}

    if exe in _DESTRUCTIVE_EXES:
        return {"destructive"}
    if exe in _CREDENTIALED_EXES:
        return {"credentialed"}
    if exe in _DEPLOY_EXES:
        return {"deploy", "network"}
    if exe == "docker":
        sub = args[0].lower() if args else ""
        if sub == "push":
            return {"deploy", "network"}
        return {"unknown"}
    if exe == "gh":
        sub = args[0].lower() if args else ""
        if sub in ("release", "deploy", "workflow", "secret", "auth"):
            return {"deploy", "network"} if sub in ("release", "deploy", "workflow") \
                else {"credentialed", "network"}
        return {"network"}

    if exe in ("rm", "del", "rmdir", "rd"):
        # Recursive/forced deletes are the destructive form; a plain single-file
        # rm is still flagged (FlexFactor never shells out to delete - it uses
        # contained unlink helpers - so any rm here is out-of-contract).
        return {"destructive"}
    if exe == "reg" and args and args[0].lower() == "delete":
        return {"destructive"}

    if exe == "git":
        sub = next((a for a in args if not a.startswith("-")), "").lower()
        low = [a.lower() for a in args]
        if sub == "clean":
            # _rollback deliberately never uses git clean (it would nuke
            # unrelated untracked files); anything reaching for it is
            # out-of-contract.
            return {"vcs", "destructive"}
        if sub == "push" and _has_flag(args, "--force", "-f") \
                and not _has_flag(args, "--force-with-lease"):
            return {"vcs", "network", "destructive"}  # lease-less force push
        if sub in _GIT_NETWORK:
            return {"vcs", "network"}
        if sub in _GIT_READ and "-d" not in low and "-D" not in args:
            return {"vcs", "read_only"}
        # checkout --force / reset / branch -D etc. are the rollback machinery.
        return {"vcs"}

    if exe in ("npm", "yarn", "pnpm", "bun"):
        sub = next((a for a in args if not a.startswith("-")), "").lower()
        if sub == "publish":
            return {"deploy", "network"}
        if sub in ("install", "i", "ci", "add", "update", "upgrade"):
            return {"install", "network"}
        if sub == "run":
            positional = [a for a in args if not a.startswith("-")]
            script = positional[1].lower() if len(positional) > 1 else ""
            if script.startswith(("deploy", "publish", "release")):
                return {"deploy", "network"}
            if "test" in script or script in ("unit", "e2e", "smoke", "ci"):
                return {"test"}
            return {"build"}
        if sub in ("test", "t"):
            return {"test"}
        return {"unknown"}
    if exe == "npx":
        # npx may download the tool it runs -> network; playwright is the one
        # runner FlexFactor drives through it.
        if _has_flag(args, "playwright") or any("playwright" in a.lower() for a in args):
            return {"test", "network"}
        return {"install", "network"}

    if exe in ("pytest", "unittest", "vitest", "jest"):
        return {"test"}
    if exe in ("node", "python", "python3", "pythonw", "py"):
        return {"build"}  # project/tool script execution (the pre-gate norm)
    if exe in ("curl", "wget", "irm", "iwr", "invoke-webrequest"):
        return {"network"}
    return {"unknown"}


def _load_policy_allow() -> set[str]:
    """Classes the owner has explicitly allowed. Env wins; file supplements."""
    allow: set[str] = set()
    env = os.environ.get("FLEXFACTOR_ALLOW_CLASSES", "")
    allow |= {t.strip().lower() for t in env.split(",") if t.strip()}
    path = os.path.join(os.path.expanduser("~"), ".flexfactor", "policy.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for t in (data.get("allow_classes") or []):
            allow.add(str(t).strip().lower())
    except (OSError, ValueError):
        pass  # no/unreadable policy file -> nothing extra allowed (fail closed)
    return allow & ALL_CLASSES


def command_allowed(cmd: list[str],
                    allow: set[str] | None = None) -> tuple[bool, str, set[str]]:
    """Gate a command: (allowed, reason_if_blocked, classes).

    High-risk classes (destructive/credentialed/deploy) are denied unless the
    owner's policy allows them. Everything else - including 'unknown' - is
    allowed, preserving pre-gate behavior for the tool's legitimate call sites.
    """
    classes = classify_command(cmd)
    effective_allow = _load_policy_allow() if allow is None else allow
    denied = (classes & HIGH_RISK) - effective_allow
    if denied:
        return (False,
                f"command class(es) {sorted(denied)} are blocked by policy "
                f"(cmd: {' '.join(str(c) for c in (cmd or []))[:200]}). "
                "Allow via FLEXFACTOR_ALLOW_CLASSES or ~/.flexfactor/policy.json.",
                classes)
    return True, "", classes
