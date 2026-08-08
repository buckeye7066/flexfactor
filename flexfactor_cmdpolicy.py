"""Command classification + policy gate for FlexFactor's subprocess chokepoint.

Every subprocess FlexFactor launches THROUGH flexfactor._run (all build/test/
install/git/verify commands, including everything a model-generated fix or
integration can influence) goes through `command_allowed` before executing.
Commands are classified into behavior classes; the HIGH-RISK classes
(destructive / credentialed / deploy) are DENIED by default and require an
explicit owner policy opt-in. Everything FlexFactor legitimately runs today
(git workflow incl. rollback, npm/yarn/pnpm build/test/install, npx
playwright, node, python) stays allowed, so this gate is additive safety, not
a behavior change.

SCOPE (stated precisely, not aspirationally): the gate governs the `_run`
chokepoint. Three call sites intentionally do NOT go through `_run`: the Repo
Rewards launcher (hardcoded local .ps1 path), the dashboard
(`pythonw flexfactor_dashboard.py`) - both owner-owned constants - and .lnk
shortcut resolution, whose single variable (the user-supplied shortcut path)
is embedded as a quote-escaped PowerShell single-quoted literal with
control-character rejection. No string from a repo, README, LLM response, or
generated patch reaches any of those calls.

Policy opt-in, either of:
  * env FLEXFACTOR_ALLOW_CLASSES="deploy,destructive"  (comma-separated)
  * ~/.flexfactor/policy.json  {"allow_classes": ["deploy", ...]}

Design decisions (deliberate, documented):
  * `unknown` is ALLOWED: audits run arbitrary project test runners and
    blocking unknowns would break the tool's core function. The mitigation is
    that wrapper/indirection paths that could LAUNDER a high-risk command are
    classified high-risk themselves: shell interpreters with inline command
    strings, `npx <deploy-tool>`, `npm --prefix ... publish`, forced
    refspecs, `git -C ... clean`.
  * In-repo git state commands (`checkout --force`, `reset`, `branch -D`) are
    plain `vcs`: they are the tool's OWN rollback machinery and their blast
    radius is the repo the user pointed FlexFactor at. The destructive class
    is for host/remote-level damage (rm -rf, git clean's untracked-file
    deletion, lease-less force push, format/dd, shell -c ...).

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
# git global options that take a separate value (so `git -C repo clean` cannot
# smuggle `clean` past a naive first-positional scan).
_GIT_VALUE_OPTS = {"-c", "-C", "--git-dir", "--work-tree", "--exec-path",
                   "--namespace"}
# npm/yarn/pnpm options that take a separate value.
_NPM_VALUE_OPTS = {"--prefix", "--registry", "--cwd", "-w", "--workspace",
                   "-C", "--dir", "--loglevel"}

_DEPLOY_EXES = {"vercel", "railway", "netlify", "flyctl", "fly", "heroku",
                "kubectl", "helm", "terraform", "pulumi", "serverless", "sls",
                "wrangler", "firebase", "eb", "cdk", "twine"}
_CREDENTIALED_EXES = {"aws", "az", "gcloud", "ssh", "scp", "sftp", "op",
                      "vault", "doppler", "pass", "keytool"}
_DESTRUCTIVE_EXES = {"format", "mkfs", "diskpart", "shutdown", "reboot",
                     "fdisk", "dd"}
# Shell interpreters: with an inline-command flag they can launder ANY command
# through the gate, so that form is classified destructive (worst-case
# capability, fail closed). FlexFactor itself never runs shells through _run.
_SHELL_EXES = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "dash", "ksh"}
_SHELL_INLINE_FLAGS = {"-command", "-c", "/c", "/k", "-encodedcommand", "-e",
                       "-enc", "-ec"}


def _exe_name(cmd: list[str]) -> str:
    if not cmd or not cmd[0]:
        return ""
    base = os.path.basename(str(cmd[0]))
    return os.path.splitext(base)[0].lower()


def _positionals(args: list[str], value_opts: set[str] = frozenset()) -> list[str]:
    """Positional (non-option) arguments, skipping the VALUES of options known
    to take one - so `-C repo` never surfaces `repo` as a subcommand."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in value_opts:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def _has_flag(args: list[str], *flags: str) -> bool:
    low = [str(a).lower() for a in args]
    return any(f in low for f in flags)


def _classify_launched_tool(args: list[str], after: str | None,
                            value_opts: set[str],
                            inline_opts: set[str] = frozenset()) -> set[str]:
    """Classify a launcher (`npx <tool> ...`, `npm exec <tool> ...`) by the
    tool it launches, recursing on the RAW argument tail from the tool token
    onward - not on an option-stripped view, so `npx bash -c '...'` hands
    `bash -c '...'` (inline shell -> destructive) to the classifier instead of
    losing the inline string to an option-value skip. `after` positions the
    scan after a subcommand token (e.g. 'exec'); network is always added
    (launchers may download the tool)."""
    start = 0
    if after is not None:
        low = [a.lower() for a in args]
        if after in low:
            start = low.index(after) + 1
        else:
            return {"unknown", "network"}
    skip = False
    for i in range(start, len(args)):
        a = args[i]
        if skip:
            skip = False
            continue
        if a.lower() in inline_opts:
            # `npx -c/--call "<string>"` executes an arbitrary shell string:
            # same laundering capability as `bash -c` -> worst-case class.
            return {"destructive"}
        if a in value_opts:
            skip = True
            continue
        if a == "--":
            continue
        if a.startswith("-"):
            continue
        if a.lower() == "playwright":
            return {"test", "network"}
        inner = classify_command(args[i:])
        return (inner - {"unknown"} or {"install"}) | {"network"}
    return {"install", "network"}


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
    if exe in _SHELL_EXES:
        low = [a.lower() for a in args]
        if any(f in low for f in _SHELL_INLINE_FLAGS):
            return {"destructive"}  # inline shell string = arbitrary command
        return {"unknown"}
    if exe == "docker":
        # Scan ALL positionals (skipping global value options) so nested forms
        # (`docker image rm`, `docker --context x system prune`) are caught.
        pos = [p.lower() for p in _positionals(
            args, {"--context", "--config", "-H", "--host", "-l", "--log-level"})]
        if "push" in pos:
            return {"deploy", "network"}
        if any(p in ("rm", "rmi", "prune") for p in pos):
            return {"destructive"}
        return {"unknown"}
    if exe == "gh":
        pos = [p.lower() for p in _positionals(args)]
        sub = pos[0] if pos else ""
        if sub in ("release", "deploy", "workflow"):
            return {"deploy", "network"}
        if sub in ("secret", "auth"):
            return {"credentialed", "network"}
        return {"network"}

    if exe in ("rm", "del", "rmdir", "rd", "erase"):
        # FlexFactor never shells out to delete (it uses contained unlink
        # helpers), so ANY delete command here is out-of-contract.
        return {"destructive"}
    if exe == "reg" and args and args[0].lower() == "delete":
        return {"destructive"}

    if exe == "git":
        pos = _positionals(args, _GIT_VALUE_OPTS)
        sub = pos[0].lower() if pos else ""
        if sub == "clean":
            # _rollback deliberately never uses git clean (it would nuke
            # unrelated untracked files); anything reaching for it is
            # out-of-contract.
            return {"vcs", "destructive"}
        if sub == "push":
            forced_flag = _has_flag(args, "--force", "-f") \
                and not _has_flag(args, "--force-with-lease")
            forced_refspec = any(p.startswith("+") for p in pos[1:])
            if forced_flag or forced_refspec:
                return {"vcs", "network", "destructive"}  # lease-less force push
            return {"vcs", "network"}
        if sub in _GIT_NETWORK:
            return {"vcs", "network"}
        if sub in _GIT_READ and "-D" not in args and "-d" not in args:
            return {"vcs", "read_only"}
        # checkout --force / reset / branch -D etc.: the rollback machinery,
        # scoped to the repo the user pointed the tool at (see module doc).
        return {"vcs"}

    if exe in ("npm", "yarn", "pnpm", "bun"):
        pos = [p for p in _positionals(args, _NPM_VALUE_OPTS)]
        low = [p.lower() for p in pos]
        sub = low[0] if low else ""
        # `publish` anywhere outside a `run <script>` invocation is a deploy,
        # regardless of what options precede it (--prefix etc.).
        if sub != "run" and "publish" in low:
            return {"deploy", "network"}
        if sub in ("install", "i", "ci", "add", "update", "upgrade"):
            return {"install", "network"}
        if sub == "run":
            script = low[1] if len(low) > 1 else ""
            if script.startswith(("deploy", "publish", "release")):
                return {"deploy", "network"}
            if "test" in script or script in ("unit", "e2e", "smoke", "ci"):
                return {"test"}
            return {"build"}
        if sub in ("test", "t"):
            return {"test"}
        if exe == "npm" and sub == "exec":
            return _classify_launched_tool(args, after="exec",
                                           value_opts={"--package", "-p"},
                                           inline_opts={"-c", "--call"})
        return {"unknown"}
    if exe == "npx":
        # npx downloads-and-runs a tool: classify the TOOL it launches (so
        # `npx vercel deploy` is a deploy, not a benign install) plus network.
        return _classify_launched_tool(args, after=None,
                                       value_opts={"-p", "--package"},
                                       inline_opts={"-c", "--call"})

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
