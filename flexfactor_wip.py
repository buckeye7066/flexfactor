"""Owner WIP snapshot that is NEVER an ancestor of a pushed FlexFactor branch.

Contract:
  - Capture dirty-tree state as an ORPHAN commit under refs/flexfactor-wip/<id>
  - Never commit owner WIP onto the sandbox branch history
  - Refuse push/merge while a snapshot is attached unless separation is proven
  - Secret-scan retained snapshot trees before any publishable operation
  - Restore byte-for-byte (tracked/untracked) across success paths
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Callable


WIP_REF_PREFIX = "refs/flexfactor-wip/"
DIRTY_SNAPSHOT_MSG = (
    "[FlexFactor] orphan WIP snapshot: pre-existing uncommitted changes\n\n"
    "These changes were already in the working tree when the run started. They are\n"
    "preserved as an ORPHAN commit under refs/flexfactor-wip/* so they are NEVER an\n"
    "ancestor of the sandbox branch FlexFactor may push or merge. Files on disk\n"
    "are restored onto your original branch as uncommitted changes when the run\n"
    "finishes (or the ref is preserved if restore fails).\n"
)

# High-confidence secret shapes (same spirit as egress gate; local scan only).
_SECRET_PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("generic_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[=:]\s*['\"][^'\"]{8,}['\"]")),
]


GitRunner = Callable[[list[str], str], object]


def _out(cp) -> str:
    return (getattr(cp, "stdout", None) or "").strip()


def _ok(cp) -> bool:
    return getattr(cp, "returncode", 1) == 0


def snapshot_ref_name(sha_or_id: str | None = None) -> str:
    tail = (sha_or_id or uuid.uuid4().hex)[:12]
    return f"{WIP_REF_PREFIX}{tail}"


def is_wip_snapshot_ref(ref: str | None) -> bool:
    return bool(ref) and str(ref).startswith(WIP_REF_PREFIX)


def resolve_snapshot_sha(git: GitRunner, project_dir: str, snapshot_id: str) -> str | None:
    """Resolve a WIP ref or raw sha to a commit sha."""
    if not snapshot_id:
        return None
    if is_wip_snapshot_ref(snapshot_id):
        cp = git(["rev-parse", snapshot_id], project_dir)
        return _out(cp) if _ok(cp) and _out(cp) else None
    # Accept bare sha
    cp = git(["rev-parse", "--verify", snapshot_id], project_dir)
    return _out(cp) if _ok(cp) and _out(cp) else None


def scan_tree_for_secrets(git: GitRunner, project_dir: str, treeish: str,
                          *, max_files: int = 200, max_bytes: int = 200_000
                          ) -> list[dict]:
    """Scan blob contents of a commit/tree for high-confidence secrets."""
    ls = git(["ls-tree", "-r", "--name-only", treeish], project_dir)
    if not _ok(ls):
        return [{"category": "scan_error", "path": None,
                 "detail": "ls-tree failed; treat as unscanned"}]
    findings: list[dict] = []
    names = [n for n in _out(ls).splitlines() if n.strip()][:max_files]
    for path in names:
        # Skip obvious binary/vendor paths
        low = path.lower()
        if any(low.endswith(ext) for ext in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                ".zip", ".gz", ".whl", ".exe", ".dll", ".so", ".dylib",
                ".lock", ".lockb")):
            continue
        show = git(["show", f"{treeish}:{path}"], project_dir)
        if not _ok(show):
            continue
        raw = getattr(show, "stdout", None) or ""
        if isinstance(raw, bytes):
            try:
                text = raw[:max_bytes].decode("utf-8", errors="replace")
            except Exception:
                continue
        else:
            text = str(raw)[:max_bytes]
        for cat, pat in _SECRET_PATTERNS:
            if pat.search(text):
                findings.append({"category": cat, "path": path, "detail": "matched"})
                break
    return findings


def snapshot_is_ancestor_of(git: GitRunner, project_dir: str,
                            snapshot_id: str, branch: str) -> bool | None:
    """True if the snapshot commit is an ancestor of branch (UNSAFE to push).

    None = could not determine (fail closed = treat as unsafe).
    """
    sha = resolve_snapshot_sha(git, project_dir, snapshot_id)
    if not sha:
        return None
    cp = git(["merge-base", "--is-ancestor", sha, branch], project_dir)
    rc = getattr(cp, "returncode", 2)
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def publish_allowed(git: GitRunner, project_dir: str, *,
                    snapshot_id: str | None, branch: str | None,
                    secret_findings: list[dict] | None = None
                    ) -> tuple[bool, str]:
    """Refuse push/merge while a WIP snapshot could enter publishable history."""
    if not snapshot_id:
        return True, "no WIP snapshot attached"
    if secret_findings:
        return False, (f"WIP snapshot secret scan found {len(secret_findings)} "
                       f"finding(s); refuse publish")
    if not branch:
        return False, "no branch to prove separation against"
    anc = snapshot_is_ancestor_of(git, project_dir, snapshot_id, branch)
    if anc is True:
        return False, ("WIP snapshot is an ancestor of the sandbox branch; "
                       "refuse push/merge")
    if anc is None:
        return False, "could not prove WIP snapshot is absent from branch history"
    # Also refuse if HEAD equals the snapshot (should never happen with orphan design)
    head = git(["rev-parse", "HEAD"], project_dir)
    snap = resolve_snapshot_sha(git, project_dir, snapshot_id)
    if _ok(head) and snap and _out(head) == snap:
        return False, "HEAD is the WIP snapshot commit; refuse publish"
    return True, "WIP snapshot is orphan/ref-only; not an ancestor of branch"


def capture_orphan_wip_snapshot(git: GitRunner, project_dir: str
                                ) -> tuple[bool, str | None, list[dict]]:
    """Stage a verbatim orphan WIP snapshot under refs/flexfactor-wip/* then
    reset the worktree back to HEAD so subsequent FlexFactor commits contain
    only tool changes.

    Returns (ok, ref_name_or_none, secret_findings).
    On failure the worktree is left as close to the original dirty state as
    possible (reset of the staging area only).
    """
    # Record porcelain fingerprint for restore verification callers.
    before = git(["status", "--porcelain", "-uall"], project_dir)
    if not _ok(before):
        return False, None, []

    add = git(["add", "-A"], project_dir)
    if not _ok(add):
        return False, None, []
    write = git(["write-tree"], project_dir)
    if not _ok(write) or not _out(write):
        git(["reset"], project_dir)
        return False, None, []
    tree = _out(write)
    # ORPHAN: no -p parent. This commit can never be an ancestor of the
    # sandbox branch unless someone explicitly merges it.
    commit = git([
        "commit-tree", tree, "-m", DIRTY_SNAPSHOT_MSG,
    ], project_dir)
    if not _ok(commit) or not _out(commit):
        git(["reset"], project_dir)
        return False, None, []
    sha = _out(commit)
    ref = snapshot_ref_name(sha)
    upd = git(["update-ref", ref, sha], project_dir)
    if not _ok(upd):
        git(["reset"], project_dir)
        return False, None, []

    secrets = scan_tree_for_secrets(git, project_dir, sha)

    # Return worktree to HEAD (sandbox base) so later commits are tool-only.
    # Mixed reset unstages; hard reset restores tracked files; clean removes
    # untracked that we captured into the orphan tree.
    git(["reset", "--hard", "HEAD"], project_dir)
    git(["clean", "-fd"], project_dir)
    return True, ref, secrets


def restore_orphan_wip_snapshot(git: GitRunner, project_dir: str,
                                snapshot_id: str) -> bool:
    """Re-apply an orphan WIP snapshot as plain uncommitted worktree changes."""
    sha = resolve_snapshot_sha(git, project_dir, snapshot_id)
    if not sha:
        return False
    # Materialize orphan tree into index+worktree, then unstage so the dirty
    # state matches a pre-run porcelain snapshot (modified + untracked).
    # First: remove tracked files that the WIP deleted (present in HEAD, absent
    # in orphan).
    deleted = git(["diff", "--name-only", "--diff-filter=D", sha, "HEAD"], project_dir)
    if _ok(deleted):
        for path in _out(deleted).splitlines():
            path = path.strip()
            if not path:
                continue
            # Prefer git rm --cached + unlink so restore stays reversible.
            git(["rm", "-f", "--ignore-unmatch", "--", path], project_dir)
            full = os.path.join(project_dir, path.replace("/", os.sep))
            try:
                if os.path.lexists(full):
                    os.unlink(full)
            except OSError:
                pass
    co = git(["checkout", sha, "--", "."], project_dir)
    if not _ok(co):
        # Fallback: read-tree merge
        rt = git(["read-tree", "-u", "--reset", sha], project_dir)
        if not _ok(rt):
            return False
    git(["reset"], project_dir)  # unstage -> plain dirty state
    return True


def drop_wip_ref(git: GitRunner, project_dir: str, snapshot_id: str) -> bool:
    """Delete a local WIP ref after successful restore (never push these refs)."""
    if not is_wip_snapshot_ref(snapshot_id):
        return False
    cp = git(["update-ref", "-d", snapshot_id], project_dir)
    return _ok(cp)


def porcelain_fingerprint(git: GitRunner, project_dir: str) -> str:
    """Stable fingerprint of dirty state for byte-for-byte restore proofs."""
    st = git(["status", "--porcelain", "-uall"], project_dir)
    text = _out(st) if _ok(st) else ""
    lines = sorted(l for l in text.splitlines() if l.strip())
    # Include content hashes for each dirty path so "same names" isn't enough.
    parts = []
    for line in lines:
        # porcelain: XY PATH or XY ORIG -> PATH
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        full = os.path.join(project_dir, path.replace("/", os.sep))
        h = "missing"
        try:
            if os.path.islink(full):
                h = "symlink:" + hashlib.sha256(
                    os.readlink(full).encode("utf-8", errors="replace")).hexdigest()[:16]
            elif os.path.isfile(full):
                with open(full, "rb") as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()[:16]
            elif os.path.isdir(full):
                h = "dir"
        except OSError:
            h = "unreadable"
        parts.append(f"{line}|{h}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
