#!/usr/bin/env python3
"""Find a source file the owner named by repo-relative path.

WHY THIS EXISTS
---------------
2026-08-20. The owner chose refactor mode and typed:

    Path to the source file to improve: backend/crawler-os/contract.js
    File not found: backend/crawler-os/contract.js

That path is real - it is `C:\\Users\\firer\\GrantFlow\\backend\\crawler-os\\
contract.js`. Refactor mode resolved it against the CURRENT DIRECTORY only, so
the one spelling a person actually knows (the path as it appears in the repo)
was the one spelling that could not work.

Owner order: option 1 checks the `buckeye7066` GitHub repos for the file
automatically. This module is that lookup, and it tries the cheap answer first:

    1. the path exactly as given (absolute, or relative to the CWD)
    2. the same relative path inside any locally cloned project
    3. GitHub code search across the owner's repos -> the local clone of the
       repo it names, cloning that repo if it isn't on disk yet

THE PROPERTY THIS MODULE MUST NEVER LOSE
----------------------------------------
Refactor mode WRITES THE FILE IN PLACE, so guessing wrong edits the wrong file.
When more than one repo holds the same relative path this module never picks
silently: every candidate is returned in `candidates`, `ambiguous` is True, and
the caller prints all of them alongside the one it chose (most recently
modified) plus the exact absolute path that overrides the guess.

`gh` absence, a rate limit, or a network failure are all REPORTED as notes, not
swallowed - a lookup that could not run must never read as "no such file".

Stdlib only, plus the `gh` CLI which the owner's machine has authenticated. The
caller injects the subprocess runner so tests never touch the network.
"""
from __future__ import annotations

import os

DEFAULT_OWNER = "buckeye7066"

#: Directories that are never a project checkout worth scanning into.
_SKIP_DIR_NAMES = {
    "node_modules", ".git", "dist", "build", ".next", "out", ".venv",
    "__pycache__", ".cache", "coverage", "vendor", "AppData", "Windows",
    "Program Files", "Program Files (x86)", ".claude", "$Recycle.Bin",
}

TIMEOUT_S = 120

#: What `_no_runner` reports when nobody wired the command chokepoint.
NO_RUNNER_NOTE = ("no brokered command runner was supplied: this module never "
                  "launches a process of its own, so the GitHub lookup could "
                  "not be performed (this is NOT 'no repo has this file')")


def _no_runner(cmd, cwd=None, timeout=TIMEOUT_S):
    """The runner used when the caller wired nothing.

    It runs NOTHING. This module used to fall back to a raw `subprocess.run`,
    which is the same containment hole the purpose-evidence gatherer had (g-5):
    a process started here is outside `flexfactor._run`, so no command policy
    classifies it and no containment claim covers it. A missing runner is now
    reported as a note - never conflated with a negative answer.
    """
    return 1, NO_RUNNER_NOTE


def canon_rel(raw):
    """Repo-relative form of a path the owner typed.

    Backslashes become forward slashes and whole leading `./` segments are
    dropped. Never `lstrip('./')` - that strips a character SET and would turn
    `.github/workflows/x.yml` into `github/workflows/x.yml`.
    """
    s = str(raw or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def _looks_like_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))


def _iter_project_dirs(roots):
    """Immediate subdirectories of each root that could be a checkout."""
    seen = set()
    for root in roots or []:
        try:
            entries = sorted(os.listdir(root))
        except (OSError, PermissionError):
            continue
        for name in entries:
            if name in _SKIP_DIR_NAMES or name.startswith("$"):
                continue
            full = os.path.join(root, name)
            key = os.path.normcase(os.path.abspath(full))
            if key in seen:
                continue
            try:
                if not os.path.isdir(full):
                    continue
            except OSError:
                continue
            seen.add(key)
            yield full


def find_local_matches(rel, roots):
    """Every locally cloned project containing this relative path."""
    rel = canon_rel(rel)
    if not rel:
        return []
    parts = rel.split("/")
    hits = []
    for proj in _iter_project_dirs(roots):
        cand = os.path.join(proj, *parts)
        try:
            if os.path.isfile(cand):
                hits.append(os.path.abspath(cand))
        except OSError:
            continue
        # Also allow the owner to include the repo folder name in the path,
        # e.g. "GrantFlow/backend/crawler-os/contract.js".
        if len(parts) > 1 and os.path.basename(proj).lower() == parts[0].lower():
            cand2 = os.path.join(proj, *parts[1:])
            try:
                if os.path.isfile(cand2):
                    hits.append(os.path.abspath(cand2))
            except OSError:
                continue
    # De-duplicate while keeping order stable.
    out, seen = [], set()
    for h in hits:
        k = os.path.normcase(h)
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def _search_query(rel, owner):
    """GitHub code-search query for one repo-relative path."""
    rel = canon_rel(rel)
    base = os.path.basename(rel)
    directory = os.path.dirname(rel)
    q = ["user:" + owner, "filename:" + base]
    if directory:
        q.append("path:" + directory)
    return " ".join(q)


def github_repos_with_file(rel, owner=DEFAULT_OWNER, run=None):
    """Repos owned by `owner` whose tree contains `rel`.

    Returns (repo_full_names, note). `note` is non-empty when the lookup could
    not be performed - never conflated with "no repo has this file".
    """
    runner = run or _no_runner
    code, out = runner([
        "gh", "api", "-X", "GET", "search/code",
        "-f", "q=" + _search_query(rel, owner),
        "-q", ".items[]?.repository.full_name",
    ])
    if code != 0:
        return [], "GitHub lookup failed: " + (out or "gh exited " + str(code))[:200]
    names, seen = [], set()
    for line in (out or "").splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names, ""


def local_clone_of(repo_full_name, roots):
    """Existing local checkout of `owner/repo`, or None."""
    want = repo_full_name.split("/")[-1].lower()
    for proj in _iter_project_dirs(roots):
        if os.path.basename(proj).lower() == want and _looks_like_repo(proj):
            return os.path.abspath(proj)
    return None


def clone_repo(repo_full_name, dest_parent, run=None):
    """Clone `owner/repo` under `dest_parent`. Returns (path, note)."""
    runner = run or _no_runner
    name = repo_full_name.split("/")[-1]
    dest = os.path.join(dest_parent, name)
    if os.path.exists(dest):
        return (dest, "") if _looks_like_repo(dest) else (
            None, "cannot clone " + repo_full_name + ": " + dest + " exists and is not a checkout")
    code, out = runner(["gh", "repo", "clone", repo_full_name, dest], timeout=900)
    if code != 0:
        return None, "clone of " + repo_full_name + " failed: " + (out or "")[:200]
    return dest, ""


def _repo_dir_name(file_path, rel):
    """Directory name of the checkout holding `file_path` at `rel`.

    `file_path` ends with `rel`, so stripping that many segments lands on the
    checkout root - whose folder name is what a clone of `owner/repo` is called.
    """
    depth = len([p for p in canon_rel(rel).split("/") if p])
    root = os.path.abspath(file_path)
    for _ in range(depth):
        root = os.path.dirname(root)
    return os.path.basename(root)


def _newest(paths):
    """The most recently modified path (deterministic tiebreak on the name)."""
    def key(p):
        try:
            return (os.path.getmtime(p), p)
        except OSError:
            return (0.0, p)
    return sorted(paths, key=key, reverse=True)[0]


def resolve_source_file(raw, roots=None, owner=DEFAULT_OWNER, run=None,
                        allow_clone=True, search_github=True):
    """Resolve a path the owner typed into a real file on disk.

    Returns a dict: path, method, candidates, notes, ambiguous, repos.
    `path` is None when nothing was found; `notes` then says what was tried.
    """
    roots = list(roots or [])
    notes = []
    res = {"path": None, "method": "not-found", "candidates": [],
           "notes": notes, "ambiguous": False, "repos": []}

    raw_s = str(raw or "").strip().strip('"')
    if not raw_s:
        notes.append("no path was given")
        return res

    # 1. Exactly as typed.
    try:
        if os.path.isfile(raw_s):
            res.update({"path": os.path.abspath(raw_s), "method": "as-given"})
            return res
    except OSError:
        pass

    rel = canon_rel(raw_s)
    if os.path.isabs(raw_s):
        notes.append("absolute path does not exist: " + raw_s)
        return res

    # 2. The same relative path inside a local checkout.
    local = find_local_matches(rel, roots)
    if local:
        res["candidates"] = local
        if len(local) == 1:
            res.update({"path": local[0], "method": "local-project"})
            return res

        # AMBIGUOUS. Measured 2026-08-20: eleven local checkouts held
        # `backend/crawler-os/contract.js`, and "most recently modified" chose
        # a scratch worktree (`gf-pkg-wt`) over the owner's real `GrantFlow`.
        # Refactor writes in place, so that default edits the wrong tree.
        # Ask GitHub which repo actually owns the path and prefer the checkout
        # named after it; recency is only the last resort.
        res["ambiguous"] = True
        canonical = None
        if search_github:
            repos, err = github_repos_with_file(rel, owner=owner, run=run)
            res["repos"] = repos
            if err:
                notes.append(err)
            elif repos:
                names = {r.split("/")[-1].lower() for r in repos}
                # `p` is the FILE; the checkout is its repo root, so match on
                # the ancestor directory whose name is the repo name.
                named = [p for p in local if _repo_dir_name(p, rel).lower() in names]
                if named:
                    canonical = named[0] if len(named) == 1 else _newest(named)
                    notes.append(
                        "GitHub says '" + rel + "' belongs to "
                        + ", ".join(repos) + "; using the checkout named after it.")
        chosen = canonical or _newest(local)
        res.update({"path": chosen, "method": "local-project"})
        if canonical is None:
            notes.append(
                str(len(local)) + " local projects contain '" + rel
                + "'; using the most recently modified. Pass an absolute path "
                  "to choose a different one.")
        else:
            notes.append(
                str(len(local)) + " local checkouts contain '" + rel
                + "'. Pass an absolute path to use a different one.")
        return res

    # 3. The owner's GitHub repos.
    if not search_github:
        notes.append("GitHub lookup was disabled")
        return res
    repos, err = github_repos_with_file(rel, owner=owner, run=run)
    if err:
        notes.append(err)
        return res
    res["repos"] = repos
    if not repos:
        notes.append("no repo under '" + owner + "' contains '" + rel + "'")
        return res
    if len(repos) > 1:
        res["ambiguous"] = True
        notes.append(
            str(len(repos)) + " repos contain '" + rel + "': "
            + ", ".join(repos) + " - using the first.")

    repo = repos[0]
    clone = local_clone_of(repo, roots)
    if clone is None:
        if not allow_clone:
            notes.append(repo + " has the file but is not cloned locally")
            return res
        if not roots:
            notes.append("no project root to clone " + repo + " into")
            return res
        clone, cerr = clone_repo(repo, roots[0], run=run)
        if clone is None:
            notes.append(cerr)
            return res
        notes.append("cloned " + repo + " to " + clone)

    cand = os.path.join(clone, *rel.split("/"))
    if not os.path.isfile(cand):
        notes.append(
            "GitHub says " + repo + " contains '" + rel
            + "' but the local checkout does not - it may need `git pull`")
        return res
    res.update({"path": os.path.abspath(cand), "method": "github:" + repo,
                "candidates": [os.path.abspath(cand)]})
    return res


def format_resolution(raw, res):
    """One-or-more lines explaining what was resolved, for the console."""
    if res.get("path"):
        lines = ["found '" + str(raw) + "' via " + res["method"] + ": " + res["path"]]
        if res.get("ambiguous"):
            for c in res.get("candidates") or []:
                if c != res["path"]:
                    lines.append("  also matched: " + c)
    else:
        lines = ["could not find '" + str(raw) + "'"]
    for n in res.get("notes") or []:
        lines.append("  " + n)
    return "\n".join(lines)
