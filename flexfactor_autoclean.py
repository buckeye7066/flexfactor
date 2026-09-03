#!/usr/bin/env python3
"""Unconditional pre-work repo cleanup.

WHY THIS EXISTS
---------------
2026-08-20. The launcher used to ask: "On a dirty working tree, commit the
pre-existing changes along with the fixes and continue instead of stopping?"
The owner read that as "do you want me to take care of whatever is left red in
the repo before starting", answered YES, and the run still threw SermonSmith
out at 17:13 with "baseline publication suite remains red".

The intent behind that click is now the implementation, and the question is
gone. Every run CLEANS THE REPO FIRST - pre-existing uncommitted changes, open
pull requests, Dependabot security alerts, open issues - and only then starts
new work. Nothing is left dangling.

THE PROPERTY THIS MODULE MUST NEVER LOSE
----------------------------------------
It must be impossible for this to do nothing and report success. Every result
satisfies:

    candidates == acted_on + skipped (with a reason each) + failed

`summarise()` asserts that identity. A cleanup that touched nothing says so
loudly, with the reason per item, because a silent no-op reported as success is
the defect this codebase treats as unacceptable.

It also never destroys owner work: uncommitted changes are COMMITTED, never
stashed away or discarded, and a red pull request is left open and reported
rather than force-merged.

Stdlib only. Every GitHub call goes through `gh`, which the owner's machine has
authenticated; when `gh` is missing the step is SKIPPED WITH THAT REASON, never
silently treated as clean.
"""
from __future__ import annotations

import datetime
import json
import os
import re

#: Check conclusions that do not block a merge. A check that has NOT REACHED a
#: conclusion is deliberately absent: see `_pr_blocking_checks`.
_OK_CHECK = {"SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED"}

#: A CheckRun's `status` when GitHub has finished running it. Anything else
#: (QUEUED, IN_PROGRESS, WAITING, PENDING, REQUESTED) means no verdict exists.
_COMPLETED = "COMPLETED"

#: A workflow job that "failed" faster than this ran no steps. GitHub reports an
#: account-wide Actions billing halt as an ordinary job FAILURE that starts and
#: completes within a couple of seconds with zero steps executed. Measured on
#: buckeye7066/GrantFlow PR #1401 (2026-08-26): 11 FAILURE jobs, every one 2-3s.
#: Duration NEVER changes the verdict - a red check blocks either way. It only
#: changes what the skip reason SAYS, so "CI never ran" is not reported to the
#: owner as "your code failed CI".
_NEVER_RAN_SECONDS = 5.0

TIMEOUT_S = 300

# CONTAINMENT (i-5). THIS MODULE OWNS NO PROCESS LAUNCHER.
#
# It used to hold a private `_run` built on the subprocess module, and it is
# the module that runs `git commit` and `gh pr merge` against the owner's
# repositories. A process started here is outside `flexfactor._run`, so
# `flexfactor_cmdpolicy` never classifies it, the execution ledger never
# records it, and the containment claim FlexFactor prints does not cover it -
# the same defect the contract named as g-5 in the purpose-evidence gatherer.
#
# Every entry point therefore takes a REQUIRED `run` runner with the contract
#
#     run(cmd: list[str], cwd: str, timeout: int = TIMEOUT_S)
#         -> (exit_code, combined_output)   # never raises
#
# and there is no default. "Somebody called clean_repo without wiring the
# chokepoint" is a TypeError at the call site, not a silent raw subprocess.


def _gh_json(args, cwd, run):
    """Run a `gh` command expecting JSON. Returns (data, error_reason)."""
    code, out = run(["gh"] + args, cwd)
    if code != 0:
        return None, out or ("gh exited " + str(code))
    try:
        return json.loads(out or "null"), ""
    except json.JSONDecodeError as exc:
        return None, "unparseable gh output: " + str(exc)


def _result(step, candidates=0):
    return {"step": step, "candidates": candidates,
            "acted_on": [], "skipped": [], "failed": []}


def _skip(res, item, reason):
    res["skipped"].append({"item": item, "reason": reason})


# Anything that is not literally `True` is NOT a pass. This mirrors the rule
# `_full_gate`'s docstring imposes on its callers - "`if final_ok is True`,
# never `if final_ok`" - because `None` (nothing ran) is the value that used to
# be reported as success and is the single worst overclaim this tool can make.
VERDICT_LABELS = {
    True: "VERIFIED",
    False: "RED",
    None: "UNVERIFIED",
}


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}\b"),
    re.compile(
        r"(?i)\b(authorization|api[ _-]?key|access[ _-]?key|token|secret|"
        r"password|passwd|pwd|connection[ _-]?string)\b\s*[:=]\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+@"),
)


def _safe_note(value, limit=600):
    """Return a bounded, single-line-safe verification note with secrets removed.

    Gate output is diagnostic data, not a payload for Git history. It may contain
    environment dumps, provider errors, or connection strings. Redaction lives at
    the normalization boundary so the run summary and any persisted result are
    protected too, not only the commit message.
    """
    text = str(value or "").replace("\x00", "").strip()
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)([a-z]"):
            text = pattern.sub(r"\1[REDACTED]@", text)
        elif "authorization|api" in pattern.pattern:
            text = pattern.sub(lambda m: m.group(1) + "=[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    if len(text) > limit:
        text = text[-limit:]
    return text or "(no detail reported)"


def _verdict(result):
    """Normalise a gate's return into (tri-state, redacted note).

    Accepts either a bare tri-state or the `(ok, log)` pair that
    `_full_gate` / `_publication_gate` return. Any shape that is not
    recognisably one of those is UNVERIFIED - never a pass.
    """
    ok, note = (result if isinstance(result, tuple) and len(result) == 2
                else (result, ""))
    if ok is not True and ok is not False:
        ok = None
    return ok, _safe_note(note)


def _verdict_line(ok, note):
    """Safe commit-message sentence: verdict and scope, never raw gate output."""
    build_only = ok is True and "no project test suite configured" in str(note).lower()
    if build_only:
        return ("Project gate: VERIFIED-BUILD-ONLY - the configured build command "
                "ran and passed on this exact candidate; this repository exposes "
                "no project test command to run.")
    return {
        True: ("Project gate: VERIFIED - the configured publication gate ran and "
               "passed on this exact candidate."),
        False: ("Project gate: RED - a configured command ran and FAILED. These "
                "changes are committed anyway so no work is lost, but this commit "
                "is NOT a verified state and must not be treated as one."),
        None: ("Project gate: UNVERIFIED - the exact committed candidate was not "
               "proven by a completed publication gate. This is not a pass."),
    }[ok]


_GENERATED_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".tox", ".nox", "htmlcov",
    ".coverage_html", "coverage", "dist", "build",
}
_GENERATED_SUFFIXES = (".pyc", ".pyo", ".pyd", ".orig", ".rej")


def _generated_path(path):
    text = str(path or "").replace("\\", "/").strip().strip("/")
    if not text:
        return False
    return (any(part in _GENERATED_PARTS for part in text.split("/"))
            or text.endswith(_GENERATED_SUFFIXES)
            or text == ".coverage")


def _nul_paths(run, cmd, cwd):
    code, out = run(cmd, cwd)
    return (code, [p for p in str(out or "").split("\0") if p])


def _run_path_chunks(run, prefix, paths, cwd, chunk=100):
    for start in range(0, len(paths), chunk):
        code, out = run(prefix + paths[start:start + chunk], cwd)
        if code != 0:
            return False, out
    return True, ""


# --------------------------------------------------------------------------
# Step 0: interrupted Git operations
# --------------------------------------------------------------------------
def recover_interrupted_git_operation(project_dir, *, run):
    """Abort a detected interrupted Git operation before ordinary cleanup.

    A normal dirty tree is handled by ``commit_pending_changes``.  An unfinished
    merge/rebase/cherry-pick/revert is different: its unmerged index makes
    ``git add -A`` fail, after which the old runner simply stopped at "working
    tree isn't clean."  Git supplies a purpose-built abort command for each
    operation, which restores its pre-operation state without choosing either
    side of the conflict.  A bare unmerged index with no operation marker is
    *reported* rather than guessed at; selecting conflict content is an owner
    decision, not a cleanup action.
    ``git ls-files --unmerged`` is used as the cross-platform plumbing view;
    porcelain ``git diff --diff-filter=U`` can omit a synthetic staged conflict
    on Git for Windows even though the index still contains stages 1/2/3.
    """
    res = _result("interrupted-git-operation")
    markers = (
        ("MERGE_HEAD", ["git", "merge", "--abort"], "merge"),
        ("CHERRY_PICK_HEAD", ["git", "cherry-pick", "--abort"], "cherry-pick"),
        ("REVERT_HEAD", ["git", "revert", "--abort"], "revert"),
    )
    active = []
    for marker, command, label in markers:
        code, out = run(["git", "rev-parse", "--git-path", marker], project_dir)
        if code != 0:
            res["candidates"] = 1
            res["failed"].append({"item": "git metadata", "reason": str(out)[:160]})
            return res
        path = str(out or "").strip()
        if path and os.path.exists(path if os.path.isabs(path)
                                else os.path.join(project_dir, path)):
            active.append((label, command))
    # Git stores rebase state in a directory instead of a single named ref.
    code, git_dir = run(["git", "rev-parse", "--git-dir"], project_dir)
    if code != 0:
        res["candidates"] = 1
        res["failed"].append({"item": "git metadata", "reason": str(git_dir)[:160]})
        return res
    base = str(git_dir or "").strip()
    if base and not os.path.isabs(base):
        base = os.path.join(project_dir, base)
    if base and (os.path.isdir(os.path.join(base, "rebase-merge"))
                 or os.path.isdir(os.path.join(base, "rebase-apply"))):
        active.append(("rebase", ["git", "rebase", "--abort"]))

    # Multiple active markers means Git state is inconsistent.  Do not run a
    # sequence of aborts against it and call that recovery.
    res["candidates"] = len(active)
    if not active:
        code, unmerged = run(["git", "ls-files", "--unmerged"], project_dir)
        if code != 0:
            res["candidates"] = 1
            res["failed"].append({"item": "unmerged check", "reason": str(unmerged)[:160]})
        elif str(unmerged or "").strip():
            res["candidates"] = 1
            res["failed"].append({
                "item": "unmerged index",
                "reason": "conflicted paths exist without a recoverable Git operation; no side was chosen",
            })
        return res
    if len(active) != 1:
        for label, _command in active:
            res["failed"].append({"item": label,
                                  "reason": "multiple interrupted Git operations detected"})
        return res
    label, command = active[0]
    code, out = run(command, project_dir)
    if code != 0:
        res["failed"].append({"item": label, "reason": str(out)[:240]})
        return res
    check, unmerged = run(["git", "ls-files", "--unmerged"], project_dir)
    if check != 0 or str(unmerged or "").strip():
        res["failed"].append({"item": label,
                              "reason": "abort ran but the index still has unmerged paths"})
        return res
    res["acted_on"].append("aborted interrupted " + label)
    return res


# --------------------------------------------------------------------------
# Step 1: uncommitted changes
# --------------------------------------------------------------------------
def commit_pending_changes(project_dir, *, run, verify=None):
    """Commit the exact pre-work candidate, never verification side effects.

    The candidate is staged *before* the publication gate and its Git tree hash
    is captured. The gate then runs against that working tree. Build/test output
    created by the gate is cleaned only when its path is mechanically generated;
    any source/config/index mutation invalidates a successful verdict. The index
    is restored to the captured candidate tree before commit, so `git commit`
    cannot sweep in a file the gate itself created.

    The commit still always happens for the original owner/sibling work, even on
    RED or UNVERIFIED. Raw gate output never enters Git history.
    """
    res = _result("uncommitted-changes")
    res["verified"] = None
    res["verify_note"] = "not attempted"
    code, out = run(["git", "status", "--porcelain"], project_dir)
    if code != 0:
        res["failed"].append({"item": "git status", "reason": out})
        res["candidates"] = 1
        return res
    changed = [ln for ln in out.splitlines() if ln.strip()]
    res["candidates"] = len(changed)
    if not changed:
        res["verify_note"] = "nothing to verify (clean tree)"
        return res

    # Freeze the candidate in the index before any command can create output.
    code, out = run(["git", "add", "-A"], project_dir)
    if code != 0:
        for ln in changed:
            res["failed"].append({"item": ln.strip(),
                                  "reason": "git add -A: " + str(out)[:160]})
        return res
    code, candidate_paths = _nul_paths(
        run, ["git", "diff", "--cached", "--name-only", "-z"], project_dir)
    if code != 0:
        res["failed"].append({"item": "staged candidate",
                              "reason": "cannot list staged paths"})
        return res
    if not candidate_paths:
        reason = "nothing to commit after staging (ignored by .gitignore)"
        for ln in changed:
            _skip(res, ln.strip(), reason)
        return res
    res["candidates"] = len(candidate_paths)

    code, tree_out = run(["git", "write-tree"], project_dir)
    candidate_tree = str(tree_out or "").strip() if code == 0 else ""
    if not candidate_tree:
        res["failed"].append({"item": "staged candidate",
                              "reason": "git write-tree failed: " + str(tree_out)[:160]})
        return res
    # Snapshot baseline ignored/untracked generated paths so pre-existing caches
    # are not removed; only newly created ignored outputs after verification go.
    _, baseline_ignored = _nul_paths(
        run, ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        project_dir)
    baseline_generated_ignored = {p for p in baseline_ignored if _generated_path(p)}

    if callable(verify):
        try:
            res["verified"], res["verify_note"] = _verdict(verify())
        except Exception as exc:                      # noqa: BLE001
            res["verified"] = None
            res["verify_note"] = _safe_note("verification gate raised: " + str(exc))
    else:
        res["verified"] = None
        res["verify_note"] = "no verification gate supplied by the caller"

    # A gate must not be able to stage a different tree. Detect and restore the
    # exact candidate index FIRST, so cleanup of a tracked generated file reads
    # from the candidate rather than from an index the gate may have changed.
    code, after_tree_out = run(["git", "write-tree"], project_dir)
    after_tree = str(after_tree_out or "").strip() if code == 0 else ""
    index_changed = after_tree != candidate_tree
    if index_changed:
        restore_code, restore_out = run(["git", "read-tree", candidate_tree],
                                        project_dir)
        if restore_code != 0:
            res["failed"].append({"item": "staged candidate",
                                  "reason": "cannot restore candidate index: "
                                  + str(restore_out)[:160]})
            return res

    # The gate may write coverage, caches, generated bundles, or even source.
    # Generated paths are removed/restored against the frozen candidate. Any
    # other mutation invalidates a green verdict because the candidate committed
    # below is no longer the same state that completed the gate.
    cleanup_failed = []
    _, tracked_dirty = _nul_paths(
        run, ["git", "diff", "--name-only", "-z"], project_dir)
    _, untracked = _nul_paths(
        run, ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        project_dir)
    generated_tracked = [p for p in tracked_dirty if _generated_path(p)]
    if generated_tracked:
        ok, detail = _run_path_chunks(
            run, ["git", "checkout", "--"], generated_tracked, project_dir)
        if not ok:
            cleanup_failed.append("tracked generated output: " + str(detail)[:120])
    # Only remove newly created ignored generated outputs compared to baseline.
    _, post_ignored = _nul_paths(
        run, ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        project_dir)
    post_generated_ignored = {p for p in post_ignored if _generated_path(p)}
    new_ignored_generated = sorted(post_generated_ignored - baseline_generated_ignored)
    if new_ignored_generated:
        ok, detail = _run_path_chunks(
            run, ["git", "clean", "-f", "-d", "--"], new_ignored_generated, project_dir)
        if not ok:
            cleanup_failed.append("untracked generated output: " + str(detail)[:120])

    # Cleanup must not alter the staged candidate either. This is a belt-and-
    # braces exact-byte check before Git history is written.
    code, final_tree_out = run(["git", "write-tree"], project_dir)
    final_tree = str(final_tree_out or "").strip() if code == 0 else ""
    if final_tree != candidate_tree:
        res["failed"].append({"item": "staged candidate",
                              "reason": "candidate index changed during cleanup"})
        return res

    _, remaining_tracked = _nul_paths(
        run, ["git", "diff", "--name-only", "-z"], project_dir)
    _, remaining_untracked = _nul_paths(
        run, ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        project_dir)
    non_generated_mutation = sorted(set(
        [p for p in remaining_tracked if not _generated_path(p)]
        + [p for p in remaining_untracked if not _generated_path(p)]
    ))
    if index_changed or non_generated_mutation or cleanup_failed:
        prior = VERDICT_LABELS.get(res["verified"], "UNVERIFIED")
        reasons = []
        if index_changed:
            reasons.append("the gate changed the Git index")
        if non_generated_mutation:
            reasons.append("the gate changed non-generated path(s): "
                           + ", ".join(non_generated_mutation[:8]))
        reasons.extend(cleanup_failed)
        res["verified"] = None
        res["verify_note"] = _safe_note(
            f"{prior} result invalidated because " + "; ".join(reasons)
            + ". The original staged candidate is committed, not the gate output.")

    # Finally, restore the ENTIRE working tree to the exact frozen candidate and
    # remove any newly-created unignored path not present in the candidate.
    run(["git", "checkout-index", "-a", "-f"], project_dir)
    _, now_untracked = _nul_paths(
        run, ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        project_dir)
    # Preserve set = ALL files tracked in the candidate index (staged adds included,
    # staged deletions excluded). This is stronger than using changed-path names.
    _, keep_list = _nul_paths(run, ["git", "ls-files", "-z"], project_dir)
    keep_set = set(keep_list)
    purge = [p for p in now_untracked if p not in keep_set]
    if purge:
        _run_path_chunks(run, ["git", "clean", "-f", "-d", "--"], purge, project_dir)

    msg = ("chore(autoclean): commit pre-existing working-tree changes\n\n"
           "FlexFactor cleans the repo before starting new work. These changes "
           "were already on disk; they are committed here so they stay visible "
           "in history instead of being swept into an unrelated fix commit.\n\n"
           "FlexFactor cannot tell whether these edits are the owner's work in "
           "progress or its own output left behind by an aborted earlier run, "
           "so it does not claim either. What it CAN state is whether the exact "
           "candidate committed below passed this project's own gate:\n\n"
           + _verdict_line(res["verified"], res["verify_note"])
           + "\n\nVerification output is retained in the redacted run report, "
             "not copied into Git history.")
    code, out = run(["git", "commit", "-m", msg], project_dir)
    if code != 0:
        reason = ("nothing to commit after staging (ignored by .gitignore)"
                  if "nothing to commit" in str(out).lower()
                  else "git commit: " + str(out)[:160])
        bucket = res["skipped"] if "nothing to commit" in str(out).lower() else res["failed"]
        for path in candidate_paths:
            if bucket is res["skipped"]:
                _skip(res, path, reason)
            else:
                bucket.append({"item": path, "reason": reason})
        return res
    res["acted_on"] = candidate_paths
    return res


# --------------------------------------------------------------------------
# Step 2: open pull requests
# --------------------------------------------------------------------------
def _check_seconds(c):
    """Wall-clock seconds a CheckRun took, or None if GitHub did not say."""
    started, completed = c.get("startedAt"), c.get("completedAt")
    if not started or not completed:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        return (datetime.datetime.strptime(completed, fmt)
                - datetime.datetime.strptime(started, fmt)).total_seconds()
    except (ValueError, TypeError):
        return None


def _ci_never_ran(rollup):
    """True when the whole rollup carries the zero-step signature.

    An account-wide GitHub Actions billing halt does not look like an outage: it
    looks like every workflow job failing. The distinguishing evidence is that
    each one starts and completes within seconds having executed no steps.

    The test is deliberately on the WHOLE rollup, never a single check: one lint
    that legitimately fails in two seconds is a real failure, whereas EVERY
    workflow job failing in two seconds is CI not running at all. Fewer than two
    failures is never enough evidence to make this claim.
    """
    fast = 0
    for c in rollup or []:
        if c.get("__typename") == "StatusContext":
            continue  # third-party contexts have no duration to judge
        if (c.get("conclusion") or "") not in ("FAILURE", "TIMED_OUT",
                                               "STARTUP_FAILURE", "CANCELLED"):
            continue
        secs = _check_seconds(c)
        if secs is None or secs > _NEVER_RAN_SECONDS:
            return False  # a real, measurable failure exists -> CI did run
        fast += 1
    return fast >= 2


def _pr_blocking_checks(pr):
    """Names of checks on this PR that are not passing OR not finished.

    THREE STATES, NOT TWO. A check that has not reached a conclusion is NOT a
    passing check. `_OK_CHECK` used to contain None and "", so a CheckRun that
    was still QUEUED or IN_PROGRESS - `conclusion` empty until it completes, and
    no `state` key at all on that GraphQL type - read as green and the PR was
    merged with its CI still running. That is the "never ran counted as passed"
    shape this codebase treats as unacceptable, and it was reachable from every
    audit/prodready run through `clean_repo`.

    A never-run check is reported as `name=NOT RUN (status)` so the reason the
    owner reads is the true one.
    """
    rollup = pr.get("statusCheckRollup") or []
    never_ran = _ci_never_ran(rollup)
    bad = []
    for c in rollup:
        name = c.get("name") or c.get("context") or "?"
        if "status" in c or "conclusion" in c:  # CheckRun
            status = str(c.get("status") or "").upper()
            conclusion = str(c.get("conclusion") or "").upper()
            if status and status != _COMPLETED:
                bad.append(f"{name}=NOT RUN ({status or 'no status'})")
                continue
            if not conclusion:
                bad.append(f"{name}=NOT RUN (no conclusion reported)")
                continue
            if conclusion not in _OK_CHECK:
                if never_ran:
                    secs = _check_seconds(c)
                    bad.append(f"{name}={conclusion} (CI NEVER RAN: zero steps in "
                               f"{'?' if secs is None else int(secs)}s - Actions "
                               "billing halt or workflow startup failure, NOT a "
                               "code failure)")
                else:
                    bad.append(f"{name}={conclusion}")
            continue
        state = str(c.get("state") or "").upper()  # StatusContext
        if not state:
            bad.append(f"{name}=NOT RUN (no state reported)")
        elif state not in _OK_CHECK:
            bad.append(f"{name}={state}")
    return bad


def land_open_prs(project_dir, repo=None, *, run):
    """Merge every green, mergeable, non-draft PR. Leave red ones OPEN."""
    res = _result("open-pull-requests")
    args = ["pr", "list", "--state", "open", "--limit", "100",
            "--json", "number,title,isDraft,mergeable,statusCheckRollup"]
    if repo:
        args += ["-R", repo]
    prs, err = _gh_json(args, project_dir, run)
    if prs is None:
        res["candidates"] = 1
        res["failed"].append({"item": "gh pr list", "reason": err[:200]})
        return res

    res["candidates"] = len(prs)
    for pr in prs:
        num = pr.get("number")
        if pr.get("isDraft"):
            _skip(res, num, "draft PR - the author has not marked it ready")
            continue
        if pr.get("mergeable") == "CONFLICTING":
            _skip(res, num, "merge conflict - needs a human decision")
            continue
        blocking = _pr_blocking_checks(pr)
        if blocking:
            _skip(res, num, "checks not green: " + ", ".join(blocking[:4]))
            continue
        margs = ["pr", "merge", str(num), "--squash", "--delete-branch"]
        if repo:
            margs += ["-R", repo]
        code, out = run(["gh"] + margs, project_dir)
        if code == 0:
            res["acted_on"].append(num)
        else:
            res["failed"].append({"item": num, "reason": out[:300]})
    return res


# --------------------------------------------------------------------------
# Step 3: Dependabot security alerts
# --------------------------------------------------------------------------
def report_dependabot(project_dir, repo=None, *, run):
    """Account for open Dependabot alerts.

    Dependabot's own PRs are merged by `land_open_prs`. Anything still open
    after that has no ready patch, so it is REPORTED, never quietly dropped.
    """
    res = _result("dependabot-alerts")
    if not repo:
        res["candidates"] = 1
        _skip(res, "dependabot", "no GitHub repo slug resolved for this program")
        return res
    query = ("[.[]|select(.state==\"open\")|"
             "{n:.number,sev:.security_vulnerability.severity,"
             "pkg:.security_vulnerability.package.name}]")
    alerts, err = _gh_json(
        ["api", "repos/" + repo + "/dependabot/alerts", "--paginate", "-q", query],
        project_dir, run)
    if alerts is None:
        res["candidates"] = 1
        _skip(res, "dependabot", "alerts unavailable: " + err[:160])
        return res
    res["candidates"] = len(alerts)
    for a in alerts:
        _skip(res, a.get("n"),
              "open " + str(a.get("sev")) + " alert on " + str(a.get("pkg"))
              + " - no merged fix PR")
    return res


# --------------------------------------------------------------------------
# Step 4: open issues
# --------------------------------------------------------------------------
def report_open_issues(project_dir, repo=None, *, run):
    """Account for open issues so the run can fold them into its work theme."""
    res = _result("open-issues")
    args = ["issue", "list", "--state", "open", "--limit", "100",
            "--json", "number,title"]
    if repo:
        args += ["-R", repo]
    issues, err = _gh_json(args, project_dir, run)
    if issues is None:
        res["candidates"] = 1
        _skip(res, "issues", "issue list unavailable: " + err[:160])
        return res
    res["candidates"] = len(issues)
    for i in issues:
        _skip(res, i.get("number"),
              "open issue carried into the run theme: "
              + str(i.get("title"))[:70])
    return res


# --------------------------------------------------------------------------
# Step 5: start from the real union
# --------------------------------------------------------------------------
def sync_with_main(project_dir, *, run):
    """Pull origin's current branch so new work builds on what just landed."""
    res = _result("sync-with-origin", candidates=1)
    code, out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], project_dir)
    if code != 0:
        res["failed"].append({"item": "rev-parse", "reason": out[:160]})
        return res
    branch = out.strip()
    code, out = run(["git", "pull", "--ff-only", "origin", branch], project_dir)
    if code == 0:
        res["acted_on"].append("fast-forwarded " + branch + " from origin")
        return res
    tail = out.splitlines()[-1][:160] if out else "(no output)"
    _skip(res, branch, "no fast-forward from origin: " + tail)
    return res


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def summarise(steps):
    """Fold step results into one accounted-for total.

    Raises AssertionError when a step loses an item - the accounting identity
    is the whole point of this module.
    """
    total = {"candidates": 0, "acted_on": 0, "skipped": 0, "failed": 0,
             "steps": steps}
    for s in steps:
        a, k, f = len(s["acted_on"]), len(s["skipped"]), len(s["failed"])
        if s["candidates"] != a + k + f:
            raise AssertionError(
                "autoclean step " + repr(s["step"]) + " lost items: candidates="
                + str(s["candidates"]) + " acted=" + str(a) + " skipped="
                + str(k) + " failed=" + str(f))
        total["candidates"] += s["candidates"]
        total["acted_on"] += a
        total["skipped"] += k
        total["failed"] += f
    return total


def clean_repo(project_dir, repo=None, report=None, *, run, verify=None):
    """Run every cleanup step in order and return the accounted summary.

    `verify` is threaded to `commit_pending_changes` only. It is the project's
    own publication gate (build + the repository's own suite), returning the
    tri-state `_full_gate` documents. Callers that omit it get an UNVERIFIED
    verdict recorded in the commit and on the summary - never a pass.
    """
    say = report if callable(report) else (lambda *a, **k: None)
    steps = []
    plan = ((recover_interrupted_git_operation, False),
            (commit_pending_changes, False),
            (land_open_prs, True),
            (report_dependabot, True),
            (report_open_issues, True),
            (sync_with_main, False))
    for fn, needs_repo in plan:
        if fn is commit_pending_changes:
            res = fn(project_dir, run=run, verify=verify)
        elif needs_repo:
            res = fn(project_dir, repo, run=run)
        else:
            res = fn(project_dir, run=run)
        steps.append(res)
        say("autoclean " + res["step"] + ": " + str(len(res["acted_on"]))
            + " done, " + str(len(res["skipped"])) + " skipped, "
            + str(len(res["failed"])) + " failed (of "
            + str(res["candidates"]) + ")")
    return summarise(steps)


def format_summary(total):
    """Human-readable cleanup report - every skip keeps its reason."""
    lines = ["repo cleanup: " + str(total["acted_on"]) + " actioned, "
             + str(total["skipped"]) + " skipped, " + str(total["failed"])
             + " failed, of " + str(total["candidates"]) + " candidate(s)"]
    for s in total["steps"]:
        if not s["candidates"]:
            lines.append("  " + s["step"] + ": nothing to do")
            continue
        lines.append("  " + s["step"] + ": " + str(len(s["acted_on"])) + "/"
                     + str(s["candidates"]) + " actioned")
        # A committed sweep is only a CLEAN sweep when the project's own gate
        # said so. Reporting "N actioned" alone is what let a red or entirely
        # unverified tree read as a successful cleanup.
        if s["step"] == "uncommitted-changes" and s["acted_on"]:
            lines.append("    gate: " + VERDICT_LABELS[s.get("verified")]
                         + " - " + str(s.get("verify_note") or ""))
        for item in s["acted_on"]:
            lines.append("    + " + str(item))
        for sk in s["skipped"]:
            lines.append("    - " + str(sk["item"]) + ": " + str(sk["reason"]))
        for fl in s["failed"]:
            lines.append("    ! " + str(fl["item"]) + ": " + str(fl["reason"]))
    return "\n".join(lines)
