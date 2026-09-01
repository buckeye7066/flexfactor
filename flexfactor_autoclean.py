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


def _verdict(result):
    """Normalise a gate's return into (tri-state, note).

    Accepts either a bare tri-state or the `(ok, log)` pair that
    `_full_gate` / `_publication_gate` return. Any shape that is not
    recognisably one of those is UNVERIFIED - never a pass.
    """
    ok, note = (result if isinstance(result, tuple) and len(result) == 2
                else (result, ""))
    if ok is not True and ok is not False:
        ok = None
    note = str(note or "").strip()
    if len(note) > 600:
        note = note[-600:]
    return ok, note or "(no detail reported)"


def _verdict_line(ok, note):
    """The commit-message sentence. States the verdict, never softens it."""
    head = {
        True: ("Project gate: VERIFIED - the build and this repository's own "
               "suite ran and passed on this exact tree."),
        False: ("Project gate: RED - a command ran and FAILED on this exact "
                "tree. These changes are committed anyway so no work is lost, "
                "but this commit is NOT a verified state and must not be "
                "treated as one."),
        None: ("Project gate: UNVERIFIED - nothing was run, so nothing was "
               "proven. This is not a pass."),
    }[ok]
    return head + "\n\n" + note


# --------------------------------------------------------------------------
# Step 1: uncommitted changes
# --------------------------------------------------------------------------
def commit_pending_changes(project_dir, *, run, verify=None):
    """Commit pre-existing uncommitted changes. Never discards them.

    VERIFIES WHAT IT COMMITS (owner order 2026-09-01). `verify` is a zero-arg
    callable returning the project's own publication gate as the SAME TRI-STATE
    `_full_gate` uses:

        True  - the build and the repo's own suite ran and passed
        False - a command ran and FAILED: this tree is red
        None  - there was NO command to run, so NOTHING was verified

    and `None` is never treated as success, exactly as `_full_gate`'s docstring
    requires of its callers.

    WHY THIS EXISTS. Until now this function ran `git add -A` with no pathspec
    and no verification of any kind, then committed under a message asserting
    the changes "were already on disk" - which is true, and which readers take
    to mean "not ours". That implication is false by construction, and the cost
    was measured: FlexFactor aborts routinely (~150 crash-<pid>.log files in
    ~/.flexfactor), nothing restores the tree on abort (`_obituary` touches only
    status.json and the lock files), and `_gate_file` deliberately KEEPS an
    unverifiable candidate ("None -> keep, but flagged unverified"). So a run
    that died mid-fix leaves its OWN half-verified edits on disk, and the next
    run sweeps them onto the branch labelled as somebody else's pre-existing
    work. Two independent line-by-line reviews (sermonsmith + genemap-discovery,
    then GrantFlow + Ellie, 2026-09-01) found 47 real regressions that reached
    repositories this way, including a deleted `expect()` in an OTP test and a
    PII gate made green by allowlisting the PII.

    WHAT THIS DOES **NOT** DO, deliberately:
      - It does not restrict which paths may be committed. Autoclean commits
        source, and must: a cleanup that can only commit its own artifacts is
        the report-only failure mode the owner deleted from this tool.
      - It does not add a dry-run, a simulate mode, or an approval gate.
      - It does not discard, stash, or refuse the work on a red verdict. The
        commit ALWAYS happens; nothing the owner or a sibling agent left on
        disk is ever lost.

    What changes is only that the verdict becomes TRUE and VISIBLE: it is
    written into the commit message, and it is returned as `res["verified"]` so
    the caller can hand a red baseline to the repair pass that already exists
    rather than building a run on top of it and reporting "closed N of M gaps"
    over a tree that no longer builds.
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

    # Verify BEFORE staging, so the gate reads the tree exactly as it sits -
    # the state a reader of the resulting commit will check out.
    if callable(verify):
        try:
            res["verified"], res["verify_note"] = _verdict(verify())
        except Exception as exc:                      # noqa: BLE001
            # A gate that BLEW UP must never read as a verified tree, for the
            # same reason `clean_repo` failing must never read as a clean repo.
            res["verified"] = None
            res["verify_note"] = "verification gate raised: " + str(exc)[:200]
    else:
        res["verified"] = None
        res["verify_note"] = "no verification gate supplied by the caller"

    code, out = run(["git", "add", "-A"], project_dir)
    if code != 0:
        for ln in changed:
            res["failed"].append({"item": ln.strip(), "reason": "git add -A: " + out[:160]})
        return res
    msg = ("chore(autoclean): commit pre-existing working-tree changes\n\n"
           "FlexFactor cleans the repo before starting new work. These changes "
           "were already on disk; they are committed here so they stay visible "
           "in history instead of being swept into an unrelated fix commit.\n\n"
           "FlexFactor cannot tell whether these edits are the owner's work in "
           "progress or its own output left behind by an aborted earlier run, "
           "so it does not claim either. What it CAN state is whether the tree "
           "it is committing passes this project's own gate:\n\n"
           + _verdict_line(res["verified"], res["verify_note"]))
    code, out = run(["git", "commit", "-m", msg], project_dir)
    if code != 0:
        # "nothing to commit" is a legitimate no-op (e.g. all changes ignored).
        reason = ("nothing to commit after staging (ignored by .gitignore)"
                  if "nothing to commit" in out.lower() else "git commit: " + out[:160])
        bucket = res["skipped"] if "nothing to commit" in out.lower() else res["failed"]
        for ln in changed:
            if bucket is res["skipped"]:
                _skip(res, ln.strip(), reason)
            else:
                bucket.append({"item": ln.strip(), "reason": reason})
        return res
    res["acted_on"] = [ln.strip() for ln in changed]
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
    plan = ((commit_pending_changes, False),
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
