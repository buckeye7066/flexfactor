"""Content-addressed chunking + completeness ledger for FlexFactor reviews.

Why this exists: `_independent_final_review` used to truncate the candidate
patch at 180,000 chars and send one prompt. A verdict cannot bind to a commit
it only partly saw. Likewise `flexfactor_evidence` labelled big source files
"too-large-for-structural-parser" instead of chunking them.

This module is the primitive layer only (stdlib, no I/O besides the optional
git runner passed in by the caller):

  * ``chunk_text``          — line-boundary chunks; every input line lands in
                              exactly one chunk; reassembly is byte-exact.
  * ``split_unified_diff``  — per-file pieces of a multi-file unified diff,
                              byte-exact when re-joined in order.
  * ``chunk_patch``         — per-file chunks that never cut a hunk header and
                              prefer ``@@`` boundaries when splitting.
  * ``ReviewLedger``        — records a verdict per chunk; ``complete()`` is
                              true only when every chunk has exactly one
                              status; ``verdict_allowed()`` refuses to
                              synthesize an approval over missing or blocked
                              scope.
  * ``head_matches``        — revoke approval when HEAD moved after review.
  * ``file_chunks_for_index`` — evidence-inventory adapter.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "sha256_text", "Chunk", "chunk_text", "split_unified_diff", "chunk_patch",
    "ReviewLedger", "head_matches", "file_chunks_for_index",
    "VALID_STATUSES", "COMMIT_METADATA_KEY",
]

VALID_STATUSES = ("clean", "findings", "blocked")
COMMIT_METADATA_KEY = "<commit-metadata>"
DIFF_HEADER = "diff --git "


def sha256_text(s: str) -> str:
    """SHA-256 hex digest of ``s`` encoded as UTF-8 (surrogates escaped, so
    any ``str`` — including lone surrogates from ``errors='surrogateescape'``
    decoding — hashes instead of raising)."""
    return hashlib.sha256(s.encode("utf-8", "surrogateescape")).hexdigest()


# --------------------------------------------------------------------------
# Chunk
# --------------------------------------------------------------------------

@dataclass
class Chunk:
    id: str
    file: str
    file_sha256: str
    index: int
    count: int
    line_start: int
    line_end: int
    text: str
    sha256: str
    continuation_of: str | None

    def to_dict(self) -> dict[str, Any]:
        """Ledger-facing view: everything except the text itself."""
        return {
            "id": self.id,
            "file": self.file,
            "file_sha256": self.file_sha256,
            "index": self.index,
            "count": self.count,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "chars": len(self.text),
            "sha256": self.sha256,
            "continuation_of": self.continuation_of,
        }


def _chunk_id(file_sha256: str, index: int, count: int) -> str:
    return f"{file_sha256[:12]}:{index}/{count}"


def _build_chunks(file: str, file_sha256: str, groups: list[list[str]],
                  line_offsets: list[int]) -> list[Chunk]:
    """Turn grouped lines into Chunk objects with stable ids + links.

    ``groups[i]`` is a list of lines (with their original terminators);
    ``line_offsets[i]`` is the 1-based number of the first line in group i.
    """
    count = len(groups)
    chunks: list[Chunk] = []
    prev_id: str | None = None
    for i, lines in enumerate(groups):
        text = "".join(lines)
        cid = _chunk_id(file_sha256, i, count)
        start = line_offsets[i]
        end = start + len(lines) - 1
        chunks.append(Chunk(
            id=cid, file=file, file_sha256=file_sha256, index=i, count=count,
            line_start=start, line_end=end, text=text, sha256=sha256_text(text),
            continuation_of=prev_id,
        ))
        prev_id = cid
    return chunks


def _group_lines(lines: list[str], *, max_chars: int, max_lines: int | None,
                 must_not_start: Callable[[str], bool] | None = None,
                 prefer_start: Callable[[str], bool] | None = None,
                 ) -> tuple[list[list[str]], list[int]]:
    """Greedy line packer.

    A chunk closes when adding the next line would exceed ``max_chars`` (or
    ``max_lines``). A single line longer than ``max_chars`` becomes its own
    chunk and is never cut. Optional ``prefer_start`` marks lines that are
    good places to open a new chunk (e.g. ``@@`` hunk headers): when the
    budget would be exceeded we back up to the most recent preferred line in
    the current chunk (if any) and cut there instead.
    """
    groups: list[list[str]] = []
    offsets: list[int] = []
    cur: list[str] = []
    cur_chars = 0
    cur_start = 1
    last_pref: int | None = None  # index within cur of the latest preferred line (>0)

    def close(upto: int | None = None) -> None:
        nonlocal cur, cur_chars, cur_start, last_pref
        if upto is None or upto >= len(cur):
            groups.append(cur)
            offsets.append(cur_start)
            cur_start += len(cur)
            cur, cur_chars, last_pref = [], 0, None
            return
        head, tail = cur[:upto], cur[upto:]
        groups.append(head)
        offsets.append(cur_start)
        cur_start += len(head)
        cur = tail
        cur_chars = sum(len(x) for x in tail)
        last_pref = None
        # Re-scan the tail for a later preferred line (none can be at 0
        # because we just opened the chunk there).
        if prefer_start is not None:
            for j in range(1, len(tail)):
                if prefer_start(tail[j]):
                    last_pref = j

    for ln, line in enumerate(lines, 1):
        n = len(line)
        would_exceed = cur and (cur_chars + n > max_chars
                                or (max_lines is not None and len(cur) >= max_lines))
        if would_exceed:
            if last_pref is not None and last_pref > 0:
                close(last_pref)
                # After back-up the tail may itself be over budget; keep
                # closing at whole-chunk boundaries until the new line fits
                # or we are left with a single oversized line.
                while cur and cur_chars + n > max_chars:
                    close()
            else:
                close()
        if prefer_start is not None and cur and prefer_start(line):
            last_pref = len(cur)
        cur.append(line)
        cur_chars += n
    if cur or not groups:
        # An empty file still yields one (empty) chunk so it has a ledger row.
        groups.append(cur)
        offsets.append(cur_start)
    return groups, offsets


def chunk_text(text: str, *, file: str, max_chars: int = 60_000,
               max_lines: int | None = None) -> list[Chunk]:
    """Split ``text`` at line boundaries only. Terminators (``\\n``, ``\\r\\n``,
    ``\\r``) stay attached to their line, so ``"".join(c.text for c in
    chunks) == text`` always holds. Line ranges are 1-based inclusive and
    contiguous across chunks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    file_sha = sha256_text(text)
    lines = text.splitlines(keepends=True)
    groups, offsets = _group_lines(lines, max_chars=max_chars, max_lines=max_lines)
    return _build_chunks(file, file_sha, groups, offsets)


# --------------------------------------------------------------------------
# Unified-diff splitting
# --------------------------------------------------------------------------

def _diff_file_key(header_line: str) -> str:
    """Extract the ``b/`` path from ``diff --git a/x b/y``.

    Handles renames (a/old b/new → "new"), deletions (b/ path still present
    in the header), and quoted paths. Falls back to the raw remainder when
    the header does not match the canonical shape.
    """
    rest = header_line[len(DIFF_HEADER):].rstrip("\r\n")
    # Quoted form: diff --git "a/sp ace" "b/sp ace"
    if rest.startswith('"'):
        parts = rest.split('" "')
        if len(parts) == 2 and parts[1].endswith('"'):
            b = parts[1][:-1]
            return b[2:] if b.startswith("b/") else b
    marker = " b/"
    idx = rest.rfind(marker)
    if idx != -1:
        return rest[idx + len(marker):]
    # "diff --git a/x b/x" without the b/ prefix (rare; --no-prefix output)
    halves = rest.split(" ", 1)
    return halves[-1] if len(halves) == 2 else rest


def split_unified_diff(patch: str) -> list[tuple[str, str]]:
    """Split a multi-file unified diff into ``(file_key, piece)`` tuples.

    Everything before the first ``diff --git`` header (``git show`` commit
    metadata) is returned under ``COMMIT_METADATA_KEY``. Binary-file notices
    and rename-only diffs are just short pieces. ``"".join(p for _, p in
    result) == patch`` is guaranteed.
    """
    if not patch:
        return []
    lines = patch.splitlines(keepends=True)
    pieces: list[tuple[str, list[str]]] = []
    cur_key = COMMIT_METADATA_KEY
    cur: list[str] = []
    for line in lines:
        if line.startswith(DIFF_HEADER):
            if cur:
                pieces.append((cur_key, cur))
            cur_key = _diff_file_key(line)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        pieces.append((cur_key, cur))
    # Duplicate file keys (same path appearing twice, e.g. a combined diff)
    # get disambiguated so ledger ids stay unique per piece.
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for key, ls in pieces:
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append((key if n == 0 else f"{key}#{n}", "".join(ls)))
    return out


def _is_hunk_header(line: str) -> bool:
    return line.startswith("@@")


def chunk_patch(patch: str, *, max_chars: int = 60_000) -> list[Chunk]:
    """Per-file chunks of a unified diff. A hunk header line is never cut
    (no line ever is — chunks are line-aligned) and, when a file piece
    exceeds ``max_chars``, the split prefers ``@@`` boundaries so a chunk
    begins on a hunk header whenever one is available."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    out: list[Chunk] = []
    for key, piece in split_unified_diff(patch):
        file_sha = sha256_text(piece)
        lines = piece.splitlines(keepends=True)
        groups, offsets = _group_lines(
            lines, max_chars=max_chars, max_lines=None,
            prefer_start=_is_hunk_header,
        )
        out.extend(_build_chunks(key, file_sha, groups, offsets))
    return out


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

@dataclass
class _Record:
    status: str
    reviewer: str
    findings: list[dict] = field(default_factory=list)
    reason: str | None = None
    response_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "reviewer": self.reviewer,
            "findings": list(self.findings), "reason": self.reason,
            "response_sha256": self.response_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_Record":
        return cls(status=d["status"], reviewer=d.get("reviewer", ""),
                   findings=list(d.get("findings") or []),
                   reason=d.get("reason"), response_sha256=d.get("response_sha256"))


class ReviewLedger:
    """Completeness ledger: one expected row per chunk, one live verdict each.

    ``record`` on an already-recorded chunk overwrites the live verdict but
    the previous one is kept in ``history`` so a flip from "findings" to
    "clean" is auditable.
    """

    def __init__(self, *, baseline_sha: str | None, candidate_sha: str | None,
                 chunks: list[Chunk]):
        self.baseline_sha = baseline_sha
        self.candidate_sha = candidate_sha
        self._chunks: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        for c in chunks:
            d = c.to_dict() if isinstance(c, Chunk) else dict(c)
            cid = d["id"]
            if cid in self._chunks:
                raise ValueError(f"duplicate chunk id: {cid}")
            self._chunks[cid] = d
            self._order.append(cid)
        self._records: dict[str, _Record] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    # -- mutation ---------------------------------------------------------

    def record(self, chunk_id: str, *, status: str, reviewer: str,
               findings: list[dict] | None = None, reason: str | None = None,
               response_sha256: str | None = None) -> None:
        if chunk_id not in self._chunks:
            raise ValueError(f"unknown chunk id: {chunk_id!r}")
        if status not in VALID_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; expected one of {VALID_STATUSES}")
        rec = _Record(status=status, reviewer=reviewer,
                      findings=list(findings or []), reason=reason,
                      response_sha256=response_sha256)
        prev = self._records.get(chunk_id)
        if prev is not None:
            self._history.setdefault(chunk_id, []).append(prev.to_dict())
        self._records[chunk_id] = rec

    # -- queries ----------------------------------------------------------

    @property
    def chunk_ids(self) -> list[str]:
        return list(self._order)

    def history(self, chunk_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(chunk_id, []))

    def missing(self) -> list[str]:
        return [cid for cid in self._order if cid not in self._records]

    def _count(self, status: str) -> int:
        return sum(1 for r in self._records.values() if r.status == status)

    def complete(self) -> bool:
        expected = len(self._order)
        recorded = len(self._records)
        tally = sum(self._count(s) for s in VALID_STATUSES)
        return (expected == tally == recorded
                and set(self._records) == set(self._order))

    def blocked(self) -> list[str]:
        return [cid for cid in self._order
                if cid in self._records and self._records[cid].status == "blocked"]

    def all_findings(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cid in self._order:
            rec = self._records.get(cid)
            if rec is None:
                continue
            meta = self._chunks[cid]
            for f in rec.findings:
                item = dict(f)
                item.setdefault("chunk_id", cid)
                item.setdefault("file", meta["file"])
                item.setdefault("reviewer", rec.reviewer)
                out.append(item)
        return out

    def summary(self) -> dict[str, Any]:
        rows = []
        for cid in self._order:
            row = dict(self._chunks[cid])
            rec = self._records.get(cid)
            row["status"] = rec.status if rec else "missing"
            row["reviewer"] = rec.reviewer if rec else None
            row["reason"] = rec.reason if rec else None
            row["findings_count"] = len(rec.findings) if rec else 0
            rows.append(row)
        return {
            "expected": len(self._order),
            "reviewed_clean": self._count("clean"),
            "reviewed_findings": self._count("findings"),
            "blocked": self._count("blocked"),
            "missing": self.missing(),
            "complete": self.complete(),
            "baseline_sha": self.baseline_sha,
            "candidate_sha": self.candidate_sha,
            "chunks": rows,
        }

    def verdict_allowed(self) -> tuple[bool, str]:
        """An approval may be synthesized only over fully-reviewed,
        unblocked scope. Returns (False, reason) otherwise."""
        if not self._order:
            return False, "ledger has no chunks: nothing was reviewed"
        miss = self.missing()
        if miss:
            return False, (f"{len(miss)} of {len(self._order)} chunks never "
                           f"reviewed: {', '.join(miss[:5])}"
                           + (" ..." if len(miss) > 5 else ""))
        if not self.complete():
            return False, "ledger tally does not reconcile with expected chunks"
        blk = self.blocked()
        if blk:
            reasons = "; ".join(
                f"{cid}: {self._records[cid].reason or 'no reason given'}"
                for cid in blk[:5])
            return False, (f"{len(blk)} chunk(s) blocked — approval cannot be "
                           f"synthesized over unreviewed scope ({reasons})")
        return True, (f"all {len(self._order)} chunks reviewed "
                      f"({self._count('clean')} clean, "
                      f"{self._count('findings')} with findings)")

    # -- persistence ------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({
            "baseline_sha": self.baseline_sha,
            "candidate_sha": self.candidate_sha,
            "chunks": [self._chunks[cid] for cid in self._order],
            "records": {cid: r.to_dict() for cid, r in self._records.items()},
            "history": self._history,
        }, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "ReviewLedger":
        d = json.loads(s)
        led = cls(baseline_sha=d.get("baseline_sha"),
                  candidate_sha=d.get("candidate_sha"), chunks=[])
        for c in d.get("chunks", []):
            led._chunks[c["id"]] = dict(c)
            led._order.append(c["id"])
        for cid, r in (d.get("records") or {}).items():
            if cid not in led._chunks:
                raise ValueError(f"record for unknown chunk id: {cid!r}")
            led._records[cid] = _Record.from_dict(r)
        led._history = {k: list(v) for k, v in (d.get("history") or {}).items()}
        return led


# --------------------------------------------------------------------------
# Commit race guard
# --------------------------------------------------------------------------

def head_matches(git_runner: Callable[..., Any], project_dir: str,
                 expected_sha: str | None) -> tuple[bool, str]:
    """Compare ``git rev-parse HEAD`` with ``expected_sha``.

    Any failure to read HEAD returns ``(False, reason)`` — never True — so a
    git error can't be mistaken for "nothing moved". Short SHAs are accepted
    as prefixes of the full one.
    """
    if not expected_sha:
        return False, "no expected sha to compare against"
    try:
        res = git_runner(["git", "rev-parse", "HEAD"], cwd=project_dir)
    except Exception as exc:  # noqa: BLE001 — any runner failure is a mismatch
        return False, f"git rev-parse HEAD raised {type(exc).__name__}: {exc}"
    rc = getattr(res, "returncode", None)
    out = getattr(res, "stdout", None)
    if rc != 0:
        err = getattr(res, "stderr", "") or ""
        return False, f"git rev-parse HEAD failed (rc={rc}) {str(err).strip()}".rstrip()
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    head = (out or "").strip()
    if not head:
        return False, "git rev-parse HEAD returned no output"
    exp = expected_sha.strip()
    if head == exp or (len(exp) >= 7 and head.startswith(exp)):
        return True, f"HEAD is {head}"
    return False, f"HEAD moved: expected {exp}, found {head}"


# --------------------------------------------------------------------------
# Evidence-inventory adapter
# --------------------------------------------------------------------------

def file_chunks_for_index(path: str, text: str, *, max_bytes: int) -> list[dict]:
    """Chunk dicts for a large source file so the evidence inventory carries a
    complete chunk ledger instead of a "too large" label. ``max_bytes`` is
    applied as a character budget per chunk (a byte cap is at least as
    strict as the same char cap for UTF-8)."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    return [c.to_dict() for c in chunk_text(text, file=path, max_chars=max_bytes)]
