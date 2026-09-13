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
import sys as _sys
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


def _snapshot_attributes(git: GitRunner, project_dir: str, *, sources: tuple[str, ...] = ()
                         ) -> dict[str, dict[str, str]]:
    """Union attributes/paths from every tree a reset or checkout can expose."""
    listing = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], project_dir)
    if not _ok(listing):
        raise RuntimeError("could not list paths for WIP attribute preflight")
    paths = set(p for p in (getattr(listing, "stdout", "") or "").split("\0") if p)
    trees = set(sources)
    head = git(["rev-parse", "--verify", "-q", "HEAD"], project_dir)
    if _ok(head) and _out(head):
        trees.add(_out(head))
    elif getattr(head, "returncode", 2) != 1:
        raise RuntimeError("could not resolve HEAD for WIP attribute preflight")
    for tree in sorted(trees):
        listing = git(["ls-tree", "-r", "--name-only", "-z", tree], project_dir)
        if not _ok(listing):
            raise RuntimeError("could not list source-tree paths for WIP attribute preflight")
        paths.update(p for p in (getattr(listing, "stdout", "") or "").split("\0") if p)
    paths = sorted(paths)
    attributes: dict[str, dict[str, str]] = {}
    selectors = [[], ["--cached"], *[["--source=" + tree] for tree in sorted(trees)]]
    for selector in selectors:
        for offset in range(0, len(paths), 100):
            checked = git(["check-attr", *selector, "-z", "--all", "--",
                           *paths[offset:offset + 100]], project_dir)
            if not _ok(checked):
                raise RuntimeError("Git cannot inspect all WIP source-tree attributes")
            fields = (getattr(checked, "stdout", "") or "").split("\0")
            if fields[-1:] == [""]:
                fields.pop()
            if len(fields) % 3:
                raise RuntimeError("invalid WIP path attributes")
            for i in range(0, len(fields), 3):
                path, attribute, value = fields[i:i + 3]
                values = attributes.setdefault(path, {})
                # An unset working-tree attribute must not mask an executable
                # attribute that reset/checkout restores from a source tree.
                if value not in ("unset", "unspecified") or attribute not in values:
                    values[attribute] = value
    return attributes


def _active_attribute(attributes: dict[str, str], names: tuple[str, ...]) -> str:
    return next((name for name in names
                 if attributes.get(name, "unspecified") not in ("unspecified", "unset")), "")


def snapshot_preflight(git: GitRunner, project_dir: str, *, sources: tuple[str, ...] = ()
                       ) -> tuple[bool, str]:
    """Reject executable/content-transforming attributes before even Git status."""
    try:
        attributes = _snapshot_attributes(git, project_dir, sources=sources)
    except Exception as exc:
        return False, str(exc)
    for path, values in attributes.items():
        attribute = _active_attribute(values, ("filter", "working-tree-encoding", "ident"))
        if attribute:
            return False, f"{path}: {attribute} cannot be safely snapshotted without transformations"
    return True, ""


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
    # -z: raw NUL-separated names. Without it git octal-quotes any non-ASCII
    # path (core.quotePath), `git show sha:"\303\274..."` then fails, and a
    # secret inside a Unicode-named file is silently never scanned.
    ls = git(["ls-tree", "-r", "--name-only", "-z", treeish], project_dir)
    if not _ok(ls):
        return [{"category": "scan_error", "path": None,
                 "detail": "ls-tree failed; treat as unscanned"}]
    findings: list[dict] = []
    raw_names = getattr(ls, "stdout", None) or ""
    names = [n for n in raw_names.split("\0") if n.strip()][:max_files]
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
    # Partially staged bytes live in a second orphan commit. Its ancestry
    # must be excluded independently; merging it does not merge the main WIP
    # snapshot, so checking only the latter can publish owner staged work.
    message = git(["show", "-s", "--format=%B", snapshot_id], project_dir)
    if not _ok(message):
        return False, "could not read WIP snapshot index metadata"
    index = re.search(r"^FlexFactor-WIP-Index: ([0-9a-f]{40,64})$", _out(message), re.M)
    if index:
        index_ancestor = snapshot_is_ancestor_of(git, project_dir, index[1], branch)
        if index_ancestor is not False:
            return False, ("WIP index snapshot is an ancestor of the branch" if index_ancestor
                           else "could not prove WIP index snapshot is absent from branch history")
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
    On failure before the ref exists the worktree and original staging state
    are preserved and ref is None. If the ref
    was written but the worktree could not be returned to HEAD, ok is False
    and ref is STILL returned: the snapshot is safe and recoverable, the tree
    is simply not clean, and the caller must not proceed as if it were.

    Ignored files stay on disk. Dirty nested repositories and in-progress Git
    operations are refused because a tree object cannot preserve their state.
    """
    safe, reason = snapshot_preflight(git, project_dir)
    if not safe:
        print(f"[flexfactor-wip] REFUSED snapshot - {reason}", file=_sys.stderr)
        return False, None, []
    attributes = _snapshot_attributes(git, project_dir)
    # Keep the original index in its own orphan commit. A working-tree-only
    # snapshot loses staged bytes whenever a path is partially staged.
    before = git(["status", "--porcelain", "-uall"], project_dir)
    if not _ok(before):
        return False, None, []
    # An orphan tree cannot represent merge/rebase metadata, even after all
    # conflicted paths have been staged. A hard reset would discard that state.
    metadata = git(["rev-parse", "--absolute-git-dir"], project_dir)
    if not _ok(metadata) or not _out(metadata):
        return False, None, []
    if any(os.path.exists(os.path.join(_out(metadata), marker)) for marker in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")):
        return False, None, []
    # status -uall emits individual files except gitlinks/embedded repos. Their
    # inner work is NOT stored by git add; never claim that it was captured.
    for line in (getattr(before, "stdout", "") or "").splitlines():
        for raw_path in line[3:].split(" -> "):
            rel = _unquote_porcelain_path(raw_path)
            path = os.path.join(project_dir, rel)
            if os.path.isdir(path) and not os.path.islink(path):
                return False, None, []
            if _active_attribute(attributes.get(rel, {}), ("text", "eol", "crlf")):
                print(f"[flexfactor-wip] REFUSED snapshot - {rel}: newline attributes "
                      "cannot guarantee exact owner bytes", file=_sys.stderr)
                return False, None, []
    # Untracked paths captured into the snapshot. After the orphan commit they
    # are removed INDIVIDUALLY (never `git clean`, which FlexFactor's command
    # policy classifies destructive because it would also nuke unrelated
    # untracked files - only what this snapshot holds is touched).
    untracked = [line[3:] for line in _out(before).splitlines()
                 if line.startswith("?? ")]

    index = git(["write-tree"], project_dir)
    base = git(["rev-parse", "HEAD"], project_dir)
    if not _ok(index) or not _out(index) or not _ok(base):
        return False, None, []
    index_commit = git(["commit-tree", _out(index), "-m",
                        "[FlexFactor] orphan WIP index"], project_dir)
    if not _ok(index_commit) or not _out(index_commit):
        return False, None, []
    ref = snapshot_ref_name()
    index_ref = ref + "-index"
    if not _ok(git(["update-ref", index_ref, _out(index_commit)], project_dir)):
        return False, None, []
    captured = False
    cleaned = False
    secrets = []
    try:
        if not _ok(git(["-c", "core.autocrlf=false", "add", "-A"], project_dir)):
            return False, None, []
        write = git(["write-tree"], project_dir)
        if not _ok(write) or not _out(write):
            return False, None, []
        # Both commits have NO parents; neither can enter candidate ancestry.
        message = (DIRTY_SNAPSHOT_MSG + "\nFlexFactor-WIP-Base: " + _out(base)
                   + "\nFlexFactor-WIP-Index: " + _out(index_commit))
        commit = git(["commit-tree", _out(write), "-m", message], project_dir)
        if not _ok(commit) or not _out(commit):
            return False, None, []
        if not _ok(git(["update-ref", ref, _out(commit)], project_dir)):
            return False, None, []
        captured = True
        secrets = scan_tree_for_secrets(git, project_dir, _out(commit))
        for finding in scan_tree_for_secrets(git, project_dir, _out(index_commit)):
            if finding not in secrets:
                secrets.append(finding)
        # Only captured owner paths may be removed. Ignored files stay put.
        if not _ok(git(["reset", "--hard", "--no-recurse-submodules", "HEAD"], project_dir)):
            return False, ref, secrets
        if _remove_captured_untracked(project_dir, untracked):
            return False, ref, secrets
        cleaned = True
        return True, ref, secrets
    except Exception:
        return False, ref if captured else None, secrets
    finally:
        restored_index = True
        if not cleaned:
            # read-tree changes only the index; never discard owner bytes to
            # recover from a staging/write-tree/ref failure.
            try:
                restored_index = _ok(git(["read-tree", _out(index)], project_dir))
            except Exception:
                restored_index = False
            if not restored_index:
                print(f"[flexfactor-wip] original index retained under {index_ref}; "
                      "index restoration failed", file=_sys.stderr)
        if not captured and restored_index:
            git(["update-ref", "-d", index_ref], project_dir)


def _unquote_porcelain_path(raw: str) -> str:
    """git quotes paths with special/non-ASCII characters as C strings."""
    rel = raw.strip()
    if rel.startswith('"') and rel.endswith('"') and len(rel) >= 2:
        body = rel[1:-1]
        try:
            rel = body.encode("latin-1", "backslashreplace").decode(
                "unicode_escape").encode("latin-1").decode("utf-8")
        except Exception:
            rel = body
    return rel[:-1] if rel.endswith("/") else rel


def _is_rooted_anywhere(rel: str) -> bool:
    r"""True for a path that is rooted on EITHER platform's rules.

    `os.path.isabs` and `os.path.splitdrive` answer for the HOST, and that made
    this containment guard mean different things on different machines: on
    Linux, `C:\Windows\System32` has no drive and no leading slash, so it sailed
    through as an ordinary relative filename (caught by CI 2026-08-25, when the
    Windows-only test that asserted otherwise finally ran on ubuntu).

    A containment rule whose answer depends on the host is the same family of
    defect as claiming containment the host cannot enforce. Nothing legitimate
    that `git status --porcelain` reports as untracked is spelled `X:\...`,
    `\\server\share` or `\rooted`, so refusing all three everywhere costs
    nothing and keeps one answer.
    """
    if rel[:1] in ("/", "\\"):
        return True                      # POSIX-absolute, UNC, or drive-relative
    return bool(_DRIVE_PREFIX_RE.match(rel))


_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:[\\/]")


def refuse_removal_reason(project_dir: str, rel: str) -> str:
    """'' when `rel` is a safe removal target under `project_dir`, else WHY not.

    This is the last thing standing between a bad input and a real deletion.
    `_remove_captured_untracked` unlinks individual captured files and refuses
    replacement directories; this guard also rejects unsafe path names, since
    nothing upstream proves those paths are safe: they are sliced out of
    `git status --porcelain` TEXT (`line[3:]`), so a malformed line, an
    unexpected porcelain shape, or git having run against the WRONG repository
    all arrive here as an ordinary-looking relative path.

    Measured 2026-08-24: every FILE inside C:/Users/firer/flexfactor/.git was
    removed twice during live audit runs (01:57 and 05:12) while the directory
    itself survived - precisely the shape of the walk below, which unlinks every
    file, rmdir's every subdirectory, and would then fail its final rmdir on a
    handle git still held. The cause was never proven. This guard makes that
    mechanism impossible whatever the cause was, and it costs nothing in normal
    operation: git never reports `.git` as untracked, so a correct run never
    trips it. A refusal is returned to the caller as a FAILED path, which keeps
    the WIP ref and stops the run from proceeding as if the tree were clean -
    never a silent skip.
    """
    if not rel:
        return "empty path"
    if os.path.isabs(rel) or os.path.splitdrive(rel)[0] or _is_rooted_anywhere(rel):
        return f"absolute path refused: {rel!r}"
    parts = [q for q in rel.replace("\\", "/").split("/") if q not in ("", ".")]
    if not parts:
        return f"path resolves to the project root itself: {rel!r}"
    # A repository directory is NEVER a legitimate removal target for a tool
    # whose whole job is to preserve and restore work.
    for q in parts:
        if q.casefold() == ".git":
            return f"refusing to delete a git directory: {rel!r}"
    if ".." in parts:
        return f"path escapes with '..': {rel!r}"
    root = os.path.realpath(project_dir)
    full = os.path.realpath(os.path.join(project_dir, rel.replace("/", os.sep)))
    if os.path.normcase(full) == os.path.normcase(root):
        return f"path resolves to the project root itself: {rel!r}"
    # realpath resolves symlinks AND Windows junctions, so a link pointing out
    # of the tree cannot smuggle a deletion past the containment check.
    if not os.path.normcase(full).startswith(os.path.normcase(root) + os.sep):
        return f"path escapes the project directory: {rel!r} -> {full}"
    return ""


def _remove_captured_untracked(project_dir: str, untracked: list[str]) -> list[str]:
    """Unlink exactly the untracked paths the snapshot captured, then prune
    directories that became empty. Returns the paths that could not be removed."""
    failed: list[str] = []
    dirs: set[str] = set()
    for raw in untracked:
        rel = _unquote_porcelain_path(raw)
        if not rel:
            continue
        refusal = refuse_removal_reason(project_dir, rel)
        if refusal:
            # Loud, and counted as a failure so the caller keeps the WIP ref and
            # refuses to treat the worktree as clean. Never a silent skip.
            print(f"[flexfactor-wip] REFUSED removal - {refusal}", file=_sys.stderr)
            failed.append(rel)
            continue
        full = os.path.join(project_dir, rel.replace("/", os.sep))
        try:
            if os.path.islink(full) or os.path.isfile(full):
                os.unlink(full)
            elif os.path.isdir(full):
                # A captured file may have been replaced with a directory;
                # none of its children has an individual preservation proof.
                failed.append(rel)
                continue
        except OSError:
            failed.append(rel)
            continue
        parent = os.path.dirname(full)
        while parent and os.path.abspath(parent) != os.path.abspath(project_dir):
            dirs.add(parent)
            parent = os.path.dirname(parent)
    for d in sorted(dirs, key=len, reverse=True):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return failed


def restore_orphan_wip_snapshot(git: GitRunner, project_dir: str,
                                snapshot_id: str) -> bool:
    """Re-apply an orphan WIP snapshot as plain uncommitted worktree changes."""
    sha = resolve_snapshot_sha(git, project_dir, snapshot_id)
    if not sha:
        return False
    message = git(["show", "-s", "--format=%B", sha], project_dir)
    if not _ok(message):
        return False
    base = re.search(r"^FlexFactor-WIP-Base: ([0-9a-f]{40,64})$", _out(message), re.M)
    index = re.search(r"^FlexFactor-WIP-Index: ([0-9a-f]{40,64})$", _out(message), re.M)
    sources = (sha, index[1]) if index else (sha,)
    safe, _reason = snapshot_preflight(git, project_dir, sources=sources)
    if not safe:
        return False
    attributes = _snapshot_attributes(git, project_dir, sources=sources)
    if base and index:
        # Restore ONLY owner-dirty paths, retaining clean paths fixed by the
        # run. Replacing the entire orphan tree silently reverses those fixes.
        changed = set()
        staged_paths = set()
        for treeish in (sha, index[1]):
            diff = git(["diff", "--name-only", "-z", "--no-renames",
                        base[1], treeish], project_dir)
            if not _ok(diff):
                return False
            paths = {p for p in (getattr(diff, "stdout", "") or "").split("\0") if p}
            changed.update(paths)
            if treeish == index[1]:
                staged_paths = paths
        listing = git(["ls-tree", "-r", "--name-only", "-z", sha], project_dir)
        if not _ok(listing):
            return False
        present = set((getattr(listing, "stdout", "") or "").split("\0"))
        if any(_active_attribute(attributes.get(path, {}), ("text", "eol", "crlf")) for path in changed):
            return False
        for path in sorted(changed):
            # Parent symlinks must not redirect checkout/removal outside the
            # repository, even when the ref was retained across an interruption.
            if refuse_removal_reason(project_dir, path):
                return False
            if path in present:
                if not _ok(git(["-c", "core.autocrlf=false", "checkout", sha, "--",
                                ":(literal)" + path], project_dir)):
                    return False
            else:
                full = os.path.join(project_dir, path.replace("/", os.sep))
                if os.path.isdir(full) and not os.path.islink(full):
                    return False  # never recursively delete a replacement directory
                if os.path.lexists(full):
                    os.unlink(full)
            # This restores the original staged blob independently of the
            # worktree blob (including staged additions/deletions and MM files).
            index_source = index[1] if path in staged_paths else "HEAD"
            if not _ok(git(["reset", index_source, "--", ":(literal)" + path], project_dir)):
                return False
        return True
    # Materialize orphan tree into index+worktree, then unstage so the dirty
    # state matches a pre-run porcelain snapshot (modified + untracked).
    # First: remove tracked files that the WIP deleted (present in HEAD, absent
    # in orphan). `git diff A B` lists changes going FROM A TO B, so "deleted
    # in the WIP" is `diff HEAD <snapshot> --diff-filter=D`. (The original
    # `diff <snapshot> HEAD` listed the WIP's NEW files instead, so a deletion
    # was never re-applied and the file came back after restore.) -z keeps
    # non-ASCII paths raw instead of octal-quoted. --no-renames: a `git mv`
    # is otherwise reported as R, not D, and the old name is never removed.
    deleted = git(["diff", "--name-only", "-z", "--no-renames",
                   "--diff-filter=D", "HEAD", sha], project_dir)
    if _ok(deleted):
        for path in (getattr(deleted, "stdout", None) or "").split("\0"):
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
    if not _ok(cp):
        return False
    return _ok(git(["update-ref", "-d", snapshot_id + "-index"], project_dir))


def porcelain_fingerprint(git: GitRunner, project_dir: str) -> str:
    """Stable fingerprint of dirty state for byte-for-byte restore proofs."""
    safe, reason = snapshot_preflight(git, project_dir)
    if not safe:
        raise RuntimeError("could not fingerprint owner WIP safely: " + reason)
    # quotePath=false: otherwise non-ASCII paths arrive octal-escaped and hash
    # as "missing" even though the file is right there.
    st = git(["status", "--porcelain=v1", "-z", "-uall"],
             project_dir)
    if not _ok(st):
        raise RuntimeError("could not fingerprint owner WIP status")
    records = iter((getattr(st, "stdout", "") or "").split("\0"))
    lines = []
    for record in records:
        if not record:
            continue
        lines.append((record, record[3:]))
        if "R" in record[:2] or "C" in record[:2]:
            original = next(records, "")
            lines.append(("original " + original, original))
    # Include content hashes for each dirty path so "same names" isn't enough.
    parts = []
    for line, path in sorted(lines):
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
        staged_bytes = ""
        if line[:1] not in (" ", "?"):
            staged = git(["ls-files", "--stage", "-z", "--", ":(literal)" + path], project_dir)
            if not _ok(staged):
                raise RuntimeError("could not fingerprint owner WIP index")
            staged_bytes = getattr(staged, "stdout", "") or ""
        parts.append(f"{line}|{h}|{staged_bytes}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
