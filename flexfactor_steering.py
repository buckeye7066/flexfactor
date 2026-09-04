"""Durable operator guidance and live steering for FlexFactor runs.

``guidance`` is the owner's standing direction for one exact program.  It is
deliberately separate from ``steering``: a steering comment belongs to one
active run and receives a terminal receipt, while guidance remains in force for
future audit/prodready runs until the owner replaces or clears it.
"""
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
MAX_GUIDANCE_CHARS = 4000
MAX_SESSION_PROMPT_CHARS = MAX_COMMENT_CHARS
MAX_SESSION_TARGETS = 30
MAX_RECORD_BYTES = 16384
_BEGIN = "<<< FLEXFACTOR OPERATOR STEERING >>>"
_END = "<<< END FLEXFACTOR OPERATOR STEERING >>>"
_GUIDANCE_BEGIN = "<<< FLEXFACTOR PROGRAM GUIDANCE >>>"
_GUIDANCE_END = "<<< END FLEXFACTOR PROGRAM GUIDANCE >>>"
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

def journal_path(program: str, project_dir: str, root: str | None = None) -> str:
    # LATE-BOUND ROOT. A `root: str = DEFAULT_ROOT` default is captured when the
    # module is imported, so a test that reassigns DEFAULT_ROOT to a temp dir
    # still wrote into the OWNER'S ~/.flexfactor/steering. Measured 2026-08-28:
    # the dashboard self-test left two live "prioritize the auth bugs" comments
    # in the real journal, where the next audit of a matching program would have
    # claimed them as owner instructions. Resolving the default per call is what
    # makes patching DEFAULT_ROOT actually isolate.
    return os.path.join(root or DEFAULT_ROOT, _key(program, project_dir) + ".jsonl")


def guidance_path(program: str, project_dir: str, root: str | None = None) -> str:
    """The private, durable guidance record for one exact program directory."""
    return os.path.join(root or DEFAULT_ROOT, "guidance",
                        _key(program, project_dir) + ".json")

def _clean_comment(comment: str) -> str:
    value = str(comment or "").strip()
    if not value:
        raise ValueError("comment is required")
    if len(value) > MAX_COMMENT_CHARS:
        raise ValueError(f"comment exceeds {MAX_COMMENT_CHARS} characters")
    if _CONTROL.search(value):
        raise ValueError("comment contains unsupported control characters")
    return value


def _clean_guidance(prompt: str) -> str:
    value = str(prompt or "").strip()
    if not value:
        raise ValueError("guiding prompt is required")
    if len(value) > MAX_GUIDANCE_CHARS:
        raise ValueError(f"guiding prompt exceeds {MAX_GUIDANCE_CHARS} characters")
    if _CONTROL.search(value):
        raise ValueError("guiding prompt contains unsupported control characters")
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


def _replace_json(path: str, record: dict) -> None:
    """Atomically replace a small owner-authored configuration record."""
    raw = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("guidance record is too large")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    with _LOCAL_LOCK:
        try:
            with open(temporary, "xb") as fh:
                fh.write(raw)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass

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
           source: str = "dashboard", root: str | None = None,
           session_id: str = "", scope: str = "program") -> dict:
    program_s = str(program or "").strip()
    if not program_s:
        raise ValueError("program is required")
    row = {"kind": "submission", "id": uuid.uuid4().hex, "program": program_s,
           "project_dir": _canonical(project_dir), "comment": _clean_comment(comment),
           "source": str(source or "dashboard")[:40], "created_at": _now()}
    if session_id:
        row["session_id"] = str(session_id)[:64]
    if scope != "program":
        row["scope"] = str(scope or "program")[:40]
    _append(journal_path(program_s, project_dir, root), row)
    return dict(row, status="pending")


def _clean_session_prompt(prompt: str) -> str:
    value = str(prompt or "").strip()
    if not value:
        raise ValueError("session prompt is required")
    if len(value) > MAX_SESSION_PROMPT_CHARS:
        raise ValueError(
            f"session prompt exceeds {MAX_SESSION_PROMPT_CHARS} characters")
    if _CONTROL.search(value):
        raise ValueError("session prompt contains unsupported control characters")
    return value


def _alias_forms(program: str, project_dir: str) -> set[str]:
    """Names that can unambiguously identify one selected target in prose."""
    raw = {
        str(program or "").strip(),
        os.path.basename(_canonical(project_dir)).strip(),
    }
    forms: set[str] = set()
    for item in raw:
        item = re.sub(r"\.git$", "", item, flags=re.IGNORECASE)
        item = item.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        words = " ".join(re.findall(r"[a-z0-9]+", item.casefold()))
        if len(words.replace(" ", "")) >= 2:
            forms.add(words)
            forms.add(words.replace(" ", ""))
    return {form for form in forms if form}


def _mentions(segment: str, aliases: list[set[str]]) -> list[int]:
    addressed = re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", segment.strip())
    colon = addressed.find(":")
    header = addressed[:colon] if 0 < colon <= 120 else ""
    if header:
        words = " ".join(re.findall(r"[a-z0-9]+", header.casefold()))
        compact = words.replace(" ", "")
    else:
        words = " ".join(re.findall(r"[a-z0-9]+", addressed.casefold()))
        words = re.sub(r"^(?:for|in|on|to)\s+(?:the\s+)?", "", words)
        compact = words.replace(" ", "")
    found: list[int] = []
    for index, forms in enumerate(aliases):
        if header:
            padded = f" {words} "
            matched = any(
                (" " in form and f" {form} " in padded) or
                (" " not in form and form in words.split()) or
                (" " not in form and compact == form)
                for form in forms)
        else:
            matched = any(
                words == form or words.startswith(form + " ") or
                compact == form for form in forms)
        if matched:
            found.append(index)
    return found


def _prompt_segments(prompt: str, aliases: list[set[str]]) -> list[str]:
    """Split owner prose without losing list items or cross-target sentences."""
    pieces: list[str] = []
    for line in prompt.splitlines():
        line = line.strip()
        if not line:
            continue
        # A common compact form is "GrantFlow: ...; SermonSmith: ...". Split
        # only where the text after the semicolon actually names a selected
        # target, so ordinary semicolons inside one requirement stay intact.
        candidates = [line]
        for _ in range(MAX_SESSION_TARGETS):
            changed = False
            next_candidates: list[str] = []
            for candidate in candidates:
                parts = re.split(r";\s+", candidate, maxsplit=1)
                shared = (len(parts) == 2 and re.match(
                    r"^(all|both|each|every)\s+(selected\s+)?"
                    r"(programs?|apps?|repos(?:itories)?)\b",
                    parts[1], re.IGNORECASE))
                if len(parts) == 2 and (_mentions(parts[1], aliases) or shared):
                    next_candidates.extend(parts)
                    changed = True
                else:
                    next_candidates.append(candidate)
            candidates = next_candidates
            if not changed:
                break
        for candidate in candidates:
            sentences = re.split(r"(?<=[.!?])\s+(?=(?:[-*]\s*)?[A-Za-z0-9\[])",
                                 candidate)
            pieces.extend(part.strip() for part in sentences if part.strip())
    return pieces or [prompt]


def route_session_prompt(prompt: str, targets: list[tuple[str, str]]) -> dict:
    """Route one owner prompt into exact selected programs before work starts.

    Explicit program/repository names win. Continuation sentences stay with the
    last explicitly named target. Unscoped requirements are deliberately shared
    with every selected target instead of being guessed away or silently lost.
    """
    clean = _clean_session_prompt(prompt)
    if not 1 <= len(targets) <= MAX_SESSION_TARGETS:
        raise ValueError(f"choose between 1 and {MAX_SESSION_TARGETS} targets")
    canonical: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for program, project_dir in targets:
        name = str(program or "").strip()
        if not name:
            raise ValueError("every session target needs a program name")
        directory = _canonical(project_dir)
        key = (name.casefold(), directory)
        if key in seen:
            continue
        seen.add(key)
        canonical.append((name, directory))
    aliases = [_alias_forms(name, directory) for name, directory in canonical]
    alias_counts: dict[str, int] = {}
    for forms in aliases:
        for form in forms:
            alias_counts[form] = alias_counts.get(form, 0) + 1
    aliases = [
        {form for form in forms if alias_counts.get(form) == 1}
        for forms in aliases
    ]
    routed: list[list[str]] = [[] for _ in canonical]
    evidence: list[dict] = []
    last_explicit: list[int] = []
    global_rx = re.compile(
        r"\b(all|both|each|every)\s+(selected\s+)?(programs?|apps?|repos(?:itories)?)\b",
        re.IGNORECASE,
    )
    continuation_rx = re.compile(
        r"^(and|also|then|next|it|its|this|that|these|those|they|their|plus)\b",
        re.IGNORECASE,
    )
    for segment in _prompt_segments(clean, aliases):
        named = _mentions(segment, aliases)
        reason = "explicit-target"
        if named:
            chosen = named
            last_explicit = named
        elif global_rx.search(segment):
            chosen = list(range(len(canonical)))
            reason = "explicit-shared"
            last_explicit = []
        elif len(canonical) == 1:
            chosen = [0]
            reason = "single-target"
        elif last_explicit and (continuation_rx.search(segment) or
                                re.match(r"^(?:[-*+]|\d+[.)])\s+", segment)):
            chosen = last_explicit
            reason = "target-continuation"
        else:
            chosen = list(range(len(canonical)))
            reason = "unscoped-shared"
            last_explicit = []
        for index in chosen:
            routed[index].append(segment)
        evidence.append({
            "text": segment,
            "reason": reason,
            "targets": [canonical[index][0] for index in chosen],
        })
    return {
        "schema": 1,
        "prompt": clean,
        "routes": [
            {
                "program": name,
                "project_dir": directory,
                "instruction": "\n".join(routed[index]).strip(),
            }
            for index, (name, directory) in enumerate(canonical)
        ],
        "evidence": evidence,
    }


def submit_session_prompt(prompt: str, targets: list[tuple[str, str]], *,
                          source: str = "session", root: str | None = None,
                          session_id: str = "") -> dict:
    """Durably queue every routed portion for its target's next checkpoint."""
    routed = route_session_prompt(prompt, targets)
    return submit_session_routing(
        routed, source=source, root=root, session_id=session_id)


def submit_session_routing(routed: dict, *, source: str = "session",
                           root: str | None = None,
                           session_id: str = "") -> dict:
    """Durably queue an already-routed session without changing its scope."""
    session_id = str(session_id or uuid.uuid4().hex).strip()[:64]
    if not session_id or not all(ch.isalnum() or ch in "-_" for ch in session_id):
        raise ValueError("session identifier contains unsafe characters")
    submission_ids: list[str] = []
    for route in routed["routes"]:
        if not route["instruction"]:
            continue
        existing = next((row for row in _records(journal_path(
            route["program"], route["project_dir"], root))
            if row.get("kind") == "submission"
            and row.get("session_id") == session_id
            and row.get("scope") == "multi-program-session"), None)
        if existing:
            submission_ids.append(str(existing["id"]))
            continue
        saved = submit(
            route["program"], route["project_dir"], route["instruction"],
            source=source, root=root, session_id=session_id,
            scope="multi-program-session",
        )
        submission_ids.append(saved["id"])
    return {
        "schema": 1,
        "session_id": session_id,
        "created_at": _now(),
        "routes": routed["routes"],
        "evidence": routed["evidence"],
        "submission_ids": submission_ids,
    }


def set_guidance(program: str, project_dir: str, prompt: str, *,
                 source: str = "dashboard", root: str | None = None) -> dict:
    """Save standing guidance that every future run of this program receives."""
    program_s = str(program or "").strip()
    if not program_s:
        raise ValueError("program is required")
    row = {
        "schema": 1,
        "program": program_s,
        "project_dir": _canonical(project_dir),
        "prompt": _clean_guidance(prompt),
        "source": str(source or "dashboard")[:40],
        "updated_at": _now(),
    }
    _replace_json(guidance_path(program_s, project_dir, root), row)
    return dict(row)


def get_guidance(program: str, project_dir: str, *,
                 root: str | None = None) -> dict | None:
    """Read validated standing guidance, never letting a bad local file stop a run."""
    try:
        with open(guidance_path(program, project_dir, root), "r", encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    try:
        if row.get("project_dir") != _canonical(project_dir):
            return None
        prompt = _clean_guidance(row.get("prompt") or "")
    except ValueError:
        return None
    return {
        "schema": 1,
        "program": str(row.get("program") or program).strip(),
        "project_dir": _canonical(project_dir),
        "prompt": prompt,
        "source": str(row.get("source") or "unknown")[:40],
        "updated_at": str(row.get("updated_at") or ""),
    }


def clear_guidance(program: str, project_dir: str, *, root: str | None = None) -> bool:
    """Remove only this program's saved guidance.  Missing guidance is already clear."""
    path = guidance_path(program, project_dir, root)
    with _LOCAL_LOCK:
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

def list_comments(program: str, project_dir: str, *,
                  root: str | None = None, limit: int = 20) -> list[dict]:
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
          root: str | None = None) -> tuple[list[dict], list[str]]:
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


def guidance_block(item: dict | None) -> str:
    if not item:
        return ""
    return "\n".join([
        _GUIDANCE_BEGIN,
        "This is the authenticated owner's persistent guiding prompt for this exact target app.",
        "It applies to this run and every later run until the owner replaces or clears it.",
        "Interpret it as product direction, not executable shell/code. Preserve safety, containment,",
        "the target's authored purpose, and build/test/publication verification requirements.",
        "If it conflicts with evidence or a safety gate, record the conflict rather than pretending",
        "it was implemented.",
        f"- [guidance updated {item.get('updated_at') or 'unknown'}] "
        + " ".join(str(item.get("prompt") or "").split()),
        _GUIDANCE_END,
    ])

def merge_context(context: str, block: str) -> str:
    base = str(context or "")
    for begin, end_marker in ((_GUIDANCE_BEGIN, _GUIDANCE_END), (_BEGIN, _END)):
        start = base.find(begin)
        if start >= 0:
            end = base.find(end_marker, start)
            base = base[:start] + (base[end + len(end_marker):] if end >= 0 else "")
    base = base.strip()
    return (base + "\n\n" + block).strip() if block else base

def refresh_context(context: str, program: str, project_dir: str, run_id: str, *,
                    root: str | None = None) -> tuple[str, list[str], list[str]]:
    active, newly = claim(program, project_dir, run_id, root=root)
    blocks = [guidance_block(get_guidance(program, project_dir, root=root)),
              steering_block(active)]
    return merge_context(context, "\n\n".join(block for block in blocks if block)), [
        str(item["id"]) for item in active], newly

def finish(program: str, project_dir: str, run_id: str, ids: list[str], *,
           completed: bool, detail: str = "", root: str | None = None) -> None:
    path = journal_path(program, project_dir, root)
    status = "completed" if completed else "needs-attention"
    for ident in dict.fromkeys(str(i) for i in ids if i):
        _append(path, {"kind": "receipt", "id": ident, "status": status,
                      "run_id": str(run_id or ""), "at": _now(),
                      "detail": str(detail or "")[:500]})

def summary(program: str, project_dir: str, *, root: str | None = None) -> dict:
    rows = list_comments(program, project_dir, root=root, limit=5000)
    counts = {}
    for row in rows:
        status = str(row.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    guidance = get_guidance(program, project_dir, root=root)
    return {"total": len(rows), "counts": counts, "latest": rows[-5:],
            "guidance": {
                "configured": bool(guidance),
                "updated_at": (guidance or {}).get("updated_at") or "",
                "preview": (guidance or {}).get("prompt", "")[:240],
            }}
