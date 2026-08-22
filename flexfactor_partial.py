"""First-class partial structured-output salvage evidence.

Truncation/malformed-tail salvage may recover complete leading elements so a
review can INFORM further work, but a partial result must NEVER authorize
CLEAN / READY / approval / merge / push / a skipped remainder.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


PARTIAL_META_KEY = "_flexfactor_partial"

MISSING_SCOPE_WARNING = (
    "Structured output was truncated or malformed; only complete leading "
    "elements were recovered. The missing remainder must not authorize CLEAN, "
    "READY, approval, merge, push, or a skipped remainder."
)


@dataclass
class PartialSalvageEvidence:
    partial: bool = True
    cut_point: int | None = None
    provider: str | None = None
    closers_appended: str = ""
    missing_scope_warning: str = MISSING_SCOPE_WARNING
    continuation_history: list[dict] = field(default_factory=list)
    correlation_id: str | None = None
    raw_len: int = 0
    reason: str = "truncated_or_malformed_tail"

    def to_dict(self) -> dict:
        return asdict(self)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def attach_partial_meta(data: Any, evidence: PartialSalvageEvidence) -> Any:
    """Stamp first-class partial=True evidence onto salvaged structured output."""
    meta = evidence.to_dict()
    meta["partial"] = True
    if isinstance(data, dict):
        out = dict(data)
        out[PARTIAL_META_KEY] = meta
        return out
    return {PARTIAL_META_KEY: meta, "value": data}


def is_partial_structured(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    meta = data.get(PARTIAL_META_KEY)
    return isinstance(meta, dict) and bool(meta.get("partial"))


def partial_evidence(data: Any) -> dict | None:
    if not isinstance(data, dict):
        return None
    meta = data.get(PARTIAL_META_KEY)
    return meta if isinstance(meta, dict) and meta.get("partial") else None


def strip_partial_meta(data: Any) -> Any:
    if not isinstance(data, dict) or PARTIAL_META_KEY not in data:
        return data
    out = dict(data)
    out.pop(PARTIAL_META_KEY, None)
    if "value" in out and len(out) == 1:
        return out["value"]
    return out


def may_authorize_clean(data: Any) -> bool:
    """Partial (or missing) structured output must never authorize CLEAN/READY."""
    if data is None:
        return False
    if is_partial_structured(data):
        return False
    return True


def salvage_truncated_json_ex(
    text: str | None,
    *,
    provider: str | None = None,
    correlation_id: str | None = None,
    continuation_history: list[dict] | None = None,
) -> tuple[Any | None, PartialSalvageEvidence | None]:
    """Best-effort repair of truncated/malformed structured output.

    Returns (parsed_value, evidence). evidence is non-None iff a PARTIAL
    salvage succeeded (complete leading elements only). Full valid JSON that
    parses without repair returns (value, None). Unsalvageable input returns
    (None, None).
    """
    if not text:
        return None, None
    s = text.strip()
    # Strip BOTH fence markers. Keeping the trailing ``` made a COMPLETE
    # fenced payload fail json.loads, fall into salvage, and come back stamped
    # partial=True (cut_point before the trailer) - a false partial that
    # would block a legitimately clean verdict.
    fence = re.match(r"```(?:json|JSON)?\s*(.*?)\s*(?:```)?\s*$", s, re.S)
    if fence:
        s = fence.group(1).strip()
    # Try a clean full parse first — not partial.
    try:
        return json.loads(s), None
    except Exception:
        pass
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        return None, None
    start = min(starts)
    # Also try extracting a balanced top-level object first.
    try:
        full = json.loads(s[start:])
        return full, None
    except Exception:
        pass

    closers: list[str] = []
    in_str = esc = False
    candidates: list[tuple[int, str]] = []
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            closers.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not closers or ch != closers[-1]:
                break
            closers.pop()
            candidates.append((i + 1, "".join(reversed(closers))))
            if not closers:
                break
    for idx, suffix in reversed(candidates):
        frag = s[start:idx].rstrip().rstrip(",")
        try:
            data = json.loads(frag + suffix)
        except Exception:
            continue
        # If suffix is empty and we parsed the whole remainder as valid JSON,
        # and the original text was only trailing junk after a complete value,
        # that is still a complete value — but we are in the salvage path
        # because the full string failed. Treat any salvage with dropped
        # trailing bytes OR appended closers as partial.
        dropped_tail = idx < len(s)
        appended = bool(suffix)
        if not dropped_tail and not appended:
            return data, None
        evidence = PartialSalvageEvidence(
            partial=True,
            cut_point=idx,
            provider=provider,
            closers_appended=suffix,
            correlation_id=correlation_id or new_correlation_id(),
            continuation_history=list(continuation_history or []),
            raw_len=len(text),
            reason=("appended_closers" if appended else "dropped_incomplete_tail"),
        )
        return attach_partial_meta(data, evidence), evidence
    return None, None


def salvage_truncated_json(text: str | None) -> Any | None:
    """Backward-compatible: return salvaged payload (with partial meta if partial)."""
    data, _ev = salvage_truncated_json_ex(text)
    return data


def merge_continuation_fragments(
    prefix: Any,
    continuation: Any,
    *,
    list_keys: tuple[str, ...] = ("findings", "residual", "issues"),
    correlation_id: str | None = None,
    mark_complete: bool = False,
) -> Any:
    """Duplicate-safe merge of a continuation fragment onto a partial prefix.

    Correlation: elements already present (by coarse key) are not duplicated.

    Contract (fail closed): the result is partial=True UNLESS BOTH hold:
      - the caller passes mark_complete=True (an explicit assertion that the
        continuation brought the response to its end), AND
      - the continuation fragment is not itself stamped partial (a fragment
        that was itself salvaged from a truncation cannot complete anything).
    A caller that forgets the flag gets a partial result, never a clean one.
    """
    if prefix is None:
        return continuation
    if continuation is None:
        return prefix
    base = strip_partial_meta(prefix)
    cont = strip_partial_meta(continuation)
    if not isinstance(base, dict) or not isinstance(cont, dict):
        return prefix
    out = dict(base)
    history = []
    meta = partial_evidence(prefix) or {}
    history.extend(meta.get("continuation_history") or [])
    history.append({
        "correlation_id": correlation_id or meta.get("correlation_id"),
        "merged_keys": [k for k in list_keys if k in cont],
    })
    for key in list_keys:
        if key not in cont:
            continue
        left = list(out.get(key) or []) if isinstance(out.get(key), list) else []
        right = cont.get(key) or []
        if not isinstance(right, list):
            continue
        seen = {_element_key(x) for x in left if isinstance(x, dict)}
        for item in right:
            if not isinstance(item, dict):
                continue
            k = _element_key(item)
            if k in seen:
                continue
            left.append(item)
            seen.add(k)
        out[key] = left
    # Completion is an explicit caller assertion AND requires a non-partial
    # fragment; anything less stays partial.
    if mark_complete and not is_partial_structured(continuation):
        out.pop(PARTIAL_META_KEY, None)
        return out
    evidence = PartialSalvageEvidence(
        partial=True,
        cut_point=meta.get("cut_point"),
        provider=meta.get("provider"),
        closers_appended=str(meta.get("closers_appended") or ""),
        correlation_id=correlation_id or meta.get("correlation_id") or new_correlation_id(),
        continuation_history=history,
        raw_len=int(meta.get("raw_len") or 0),
        reason="continuation_merge_still_partial",
    )
    return attach_partial_meta(out, evidence)


def _element_key(item: dict) -> tuple:
    return (
        item.get("file"),
        item.get("line"),
        str(item.get("title", ""))[:60].lower(),
        str(item.get("severity", "")).lower(),
    )


def continuation_prompt(correlation_id: str, partial_summary: str) -> str:
    return (
        f"CONTINUATION REQUEST correlation_id={correlation_id}\n"
        "Your previous structured JSON response was truncated or malformed. "
        "Resume from the cut point. Return ONLY the remaining JSON fragment "
        "needed to complete the response (or a JSON object with the remaining "
        f"array elements). Do not repeat already-emitted elements.\n"
        f"PARTIAL SUMMARY (untrusted):\n{partial_summary[:2000]}\n"
    )


def refuse_clean_if_partial(data: Any, verdict_key: str = "verdict") -> Any:
    """If data is partial, force any CLEAN/READY/keep verdict off."""
    if not is_partial_structured(data) or not isinstance(data, dict):
        return data
    out = dict(data)
    v = str(out.get(verdict_key, "")).lower()
    if v in ("clean", "ready", "keep", "approve", "approved", "pass"):
        out[verdict_key] = "needs_work"
        residual = list(out.get("residual") or [])
        residual.append({
            "severity": "high",
            "line": 0,
            "title": "partial verifier output",
            "problem": MISSING_SCOPE_WARNING,
            "realistic_input": True,
            "affects_core": True,
        })
        out["residual"] = residual
    return out
