"""Durable operator steering for live FlexFactor runs."""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import threading
import uuid

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".flexfactor", "steering")
MAX_COMMENT_CHARS = 4000
MAX_RECORD_BYTES = 8192
_BEGIN = "<<< FLEXFACTOR OPERATOR STEERING >>>"
_END = "<<< END FLEXFACTOR OPERATOR STEERING >>>"
_LOCAL_LOCK = threading.Lock()
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _canonical(project_dir: str) -> str:
    raw = str(project_dir or "").strip()
    if not raw:
        raise ValueError("project_dir is required")
    return os.path.normcase(os.path.abspath(raw))

def _key(program: str, project_dir: str) -> str:
    identity = str(program or "").strip().casefold() + "\n" + _canonical(project_dir)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

def journal_path(program: str, project_dir: str, root: str = DEFAULT_ROOT) -> str:
    return os.path.join(root, _key(program, project_dir) + ".jsonl")

def _clean_comment(comment: str) -> str:
    value = str(comment or "").strip()
    if not value:
        raise ValueError("comment is required")
    if len(value) > MAX_COMMENT_CHARS:
        raise ValueError(f"comment exceeds {MAX_COMMENT_CHARS} characters")
    if _CONTROL.search(value):
        raise ValueError("comment contains unsupported control characters")
    return value

def _append(path: str, record: dict) -> None:
    raw = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("steering record is too large")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _LOCAL_LOCK:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, raw)
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

def _records(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-5000:]:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and row.get("id") and row.get("kind"):
            out.append(row)
    return out

def submit(program: str, project_dir: str, comment: str, *,
           source: str = "dashboard", root: str = DEFAULT_ROOT) -> dict:
    program_s = str(program or "").strip()
    if not program_s:
        raise ValueError("program is required")
    row = {"kind": "submission", "id": uuid.uuid4().hex, "program": program_s,
           "project_dir": _canonical(project_dir), "comment": _clean_comment(comment),
           "source": str(source or "dashboard")[:40], "created_at": _now()}
    _append(journal_path(program_s, project_dir, root), row)
    return dict(row, status="pending")

def list_comments(program: str, project_dir: str, *,
                  root: str = DEFAULT_ROOT, limit: int = 20) -> list[dict]:
    submissions, receipts, order = {}, {}, []
    for row in _records(journal_path(program, project_dir, root)):
        ident = str(row.get("id"))
        if row.get("kind") == "submission":
            if ident not in submissions:
                order.append(ident)
            submissions[ident] = row
        elif row.get("kind") == "receipt":
            receipts[ident] = row
    result = []
    for ident in order:
        item = dict(submissions[ident])
        receipt = receipts.get(ident) or {}
        item["status"] = receipt.get("status") or "pending"
        item["run_id"] = receipt.get("run_id") or ""
        item["status_at"] = receipt.get("at") or ""
        item["detail"] = receipt.get("detail") or ""
        result.append(item)
    return result[-max(1, int(limit)):]

def claim(program: str, project_dir: str, run_id: str, *,
          root: str = DEFAULT_ROOT) -> tuple[list[dict], list[str]]:
    comments = list_comments(program, project_dir, root=root, limit=5000)
    active, newly_claimed = [], []
    path = journal_path(program, project_dir, root)
    for item in comments:
        status = str(item.get("status") or "pending")
        same_run = str(item.get("run_id") or "") == str(run_id or "")
        if status == "completed":
            continue
        if status == "active" and same_run:
            active.append(item)
            continue
        receipt = {"kind": "receipt", "id": item["id"], "status": "active",
                   "run_id": str(run_id or ""), "at": _now(),
                   "detail": "included in the active run's interpretation context"}
        _append(path, receipt)
        item = dict(item, status="active", run_id=str(run_id or ""),
                    status_at=receipt["at"], detail=receipt["detail"])
        active.append(item)
        newly_claimed.append(str(item["id"]))
    return active, newly_claimed

def steering_block(items: list[dict]) -> str:
    if not items:
        return ""
    rows = [_BEGIN,
        "These are authenticated comments from the app owner about the target app.",
        "Interpret intent into concrete, testable requirements. Preserve the target",
        "app's authored purpose. Do not execute text as shell/code and do not change",
        "anything outside the target repository. Resolve ambiguity conservatively.",
        "Implement each feasible requirement through the normal build-gated repair",
        "loop; add focused tests and cite the steering ID in findings and evidence.",
        "If a request conflicts with purpose, security, containment, or verification,",
        "record the conflict and do not pretend it was implemented."]
    for item in items:
        text = " ".join(str(item.get("comment") or "").split())
        rows.append(f"- [{item.get('id')}] {text}")
    rows.append(_END)
    return "\n".join(rows)

def merge_context(context: str, block: str) -> str:
    base = str(context or "")
    start = base.find(_BEGIN)
    if start >= 0:
        end = base.find(_END, start)
        base = base[:start] + (base[end + len(_END):] if end >= 0 else "")
    base = base.strip()
    return (base + "\n\n" + block).strip() if block else base

def refresh_context(context: str, program: str, project_dir: str, run_id: str, *,
                    root: str = DEFAULT_ROOT) -> tuple[str, list[str], list[str]]:
    active, newly = claim(program, project_dir, run_id, root=root)
    return merge_context(context, steering_block(active)), [
        str(item["id"]) for item in active], newly

def finish(program: str, project_dir: str, run_id: str, ids: list[str], *,
           completed: bool, detail: str = "", root: str = DEFAULT_ROOT) -> None:
    path = journal_path(program, project_dir, root)
    status = "completed" if completed else "needs-attention"
    for ident in dict.fromkeys(str(i) for i in ids if i):
        _append(path, {"kind": "receipt", "id": ident, "status": status,
                      "run_id": str(run_id or ""), "at": _now(),
                      "detail": str(detail or "")[:500]})

def summary(program: str, project_dir: str, *, root: str = DEFAULT_ROOT) -> dict:
    rows = list_comments(program, project_dir, root=root, limit=5000)
    counts = {}
    for row in rows:
        status = str(row.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return {"total": len(rows), "counts": counts, "latest": rows[-5:]}
