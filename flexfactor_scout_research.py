"""Evidence acquisition and comparison contracts for FlexFactor Scout.

Scout has two different subjects:

* the *target* program that should become better; and
* the *scouted* program whose public website may evidence useful capabilities.

The original Scout path collapsed those subjects into one string and sent a
name/URL to a model without retrieving it.  This module supplies the network
half of the corrected contract.  It performs bounded public-web retrieval,
records every page used as evidence, and exposes schemas that force the model
to cite target and source evidence separately. The scouted subject is always a
public website URL; Repo Rewards owns repository discovery. Target repository
inspection is orchestrated by :mod:`flexfactor`, where the existing contained-
file and hermetic-git helpers live.

The module is stdlib-only.  Tests inject ``opener`` and ``resolver``; production
uses an SSRF-resistant HTTP client that rejects credentials, loopback/private
addresses, unsafe redirects, non-text responses, and oversized bodies.
"""
from __future__ import annotations

import datetime as _datetime
import html as _html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
from html.parser import HTMLParser
from typing import Callable, Iterable

__all__ = [
    "TARGET_PROFILE_SCHEMA",
    "SCOUTED_PROGRAM_PROFILE_SCHEMA",
    "PROGRAM_COMPARISON_SCHEMA",
    "TARGET_PROFILE_SYSTEM",
    "SCOUTED_PROGRAM_PROFILE_SYSTEM",
    "PROGRAM_COMPARISON_SYSTEM",
    "crawl_public_program",
    "format_evidence_context",
    "validate_profile_references",
    "validate_comparison",
    "is_public_http_url",
]


_UA = "Mozilla/5.0 (compatible; FlexFactor-Scout/2.0; program research)"
_HTTP_TIMEOUT = 25.0
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_PAGE_TEXT = 18_000
_MAX_TOTAL_TEXT = 72_000
_FEATURE_TERMS = re.compile(
    r"(?:feature|capabilit|product|solution|workflow|integration|document|"
    r"developer|platform|how[- ]it[- ]works|use[- ]case|about|overview)", re.I
)
_SKIP_SUFFIXES = (
    ".7z", ".avi", ".bin", ".dmg", ".doc", ".docx", ".exe", ".gif",
    ".gz", ".ico", ".iso", ".jpeg", ".jpg", ".mov", ".mp3", ".mp4",
    ".pdf", ".png", ".ppt", ".pptx", ".rar", ".svg", ".tar", ".webp",
    ".xls", ".xlsx", ".zip",
)


TARGET_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "users": {"type": "array", "items": {"type": "string"}},
        "goals": {"type": "array", "items": {"type": "string"}},
        "stack": {"type": "array", "items": {"type": "string"}},
        "workflows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "current_behavior": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["implemented", "partial", "missing", "unknown"],
                    },
                    "evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": ["name", "current_behavior", "status", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "current_behavior": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["implemented", "partial", "missing", "unknown"],
                    },
                    "evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": ["name", "current_behavior", "status", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "optimization_needs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "need": {"type": "string"},
                    "why": {"type": "string"},
                    "priority": {
                        "type": "string", "enum": ["critical", "high", "medium", "low"],
                    },
                    "evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": ["need", "why", "priority", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "name", "summary", "users", "goals", "stack", "workflows",
        "capabilities", "constraints", "optimization_needs", "coverage_gaps",
    ],
    "additionalProperties": False,
}


SCOUTED_PROGRAM_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "program_type": {
            "type": "string",
            "enum": ["open-source", "commercial", "service", "website", "unknown"],
        },
        "canonical_url": {"type": "string"},
        "workflows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "behavior": {"type": "string"},
                    "evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": ["name", "behavior", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "behavior": {"type": "string"},
                    "user_value": {"type": "string"},
                    "implementation_pattern": {"type": "string"},
                    "evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "confidence": {
                        "type": "string", "enum": ["high", "medium", "low"],
                    },
                },
                "required": [
                    "name", "behavior", "user_value", "implementation_pattern",
                    "evidence_refs", "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "stack_signals": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "name", "summary", "program_type", "canonical_url", "workflows",
        "capabilities", "stack_signals", "limitations", "coverage_gaps",
    ],
    "additionalProperties": False,
}


PROGRAM_COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "target_name": {"type": "string"},
        "scouted_name": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["adopt", "adapt", "reject", "investigate"],
                    },
                    "source_behavior": {"type": "string"},
                    "target_state": {"type": "string"},
                    "target_gap": {"type": "string"},
                    "purpose_alignment": {"type": "string"},
                    "how_it_optimizes": {"type": "string"},
                    "adaptation_plan": {"type": "string"},
                    "target_touchpoints": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "verification_plan": {"type": "string"},
                    "implementation_search_query": {"type": "string"},
                    "value_score": {"type": "integer"},
                    "confidence": {
                        "type": "string", "enum": ["high", "medium", "low"],
                    },
                    "target_evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "source_evidence_refs": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": [
                    "capability", "decision", "source_behavior", "target_state",
                    "target_gap", "purpose_alignment", "how_it_optimizes",
                    "adaptation_plan", "target_touchpoints", "verification_plan",
                    "implementation_search_query", "value_score", "confidence",
                    "target_evidence_refs", "source_evidence_refs",
                ],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "target_name", "scouted_name", "recommendations", "summary", "coverage_gaps",
    ],
    "additionalProperties": False,
}


TARGET_PROFILE_SYSTEM = (
    "You are a product architect establishing what the TARGET program actually "
    "does before any comparison. Use only the supplied target evidence. Build a "
    "workflow and capability inventory, distinguish implemented/partial/missing/"
    "unknown, and cite one or more exact [T#] evidence identifiers for every "
    "behavioral claim. A README promise without code/test/runtime corroboration is "
    "partial or unknown, never implemented. Name evidence shortfalls in "
    "coverage_gaps. Do not propose generic improvements. Respond with JSON only."
)

SCOUTED_PROGRAM_PROFILE_SYSTEM = (
    "You are examining one specifically named SCOUTED program for capabilities "
    "that might be useful elsewhere. Use only the retrieved website pages supplied. "
    "Describe observable behavior and implementation patterns; "
    "do not invent features from the product name. Every workflow/capability must "
    "cite exact [S#] evidence identifiers. When evidence is marketing-only, lower "
    "confidence and say so. Respond with JSON only."
)

PROGRAM_COMPARISON_SYSTEM = (
    "You are comparing two different programs. The TARGET'S own purpose is the "
    "authority. Create exactly one decision row for EVERY capability in the "
    "SCOUTED profile, using its exact capability name. Identify only capabilities "
    "proven in the SCOUTED program that "
    "would materially optimize a real target gap. Reject duplication, decorative "
    "parity, purpose drift, and changes whose cost outweighs their value. For each "
    "row cite BOTH exact target [T#] and source [S#] evidence identifiers, explain "
    "the current target state, describe the behavior to adapt (not copyrighted UI "
    "or closed source), name concrete target touchpoints, and give an executable "
    "verification plan. implementation_search_query must seek an implementation "
    "of that exact behavior, not merely repeat either product name. Respond with "
    "JSON only."
)


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def _resolve_public_http_url(value: str, resolver: Callable | None = None):
    """Resolve once and return ``(parsed, ascii_host, public_addresses)``.

    The returned addresses are the ones the transport must connect to. Keeping
    resolution and connection in one contract prevents a hostname from passing
    the public-address check and then rebinding to a private address during a
    second resolver lookup.
    """
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(f"malformed URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http(s) URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in URLs are refused")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("loopback hosts are refused")
    try:
        ascii_host = host.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"malformed hostname or port: {exc}") from exc
    try:
        direct = ipaddress.ip_address(ascii_host.split("%", 1)[0])
    except ValueError:
        direct = None
    if direct is not None:
        if not _public_ip(str(direct)):
            raise ValueError("private/reserved address is refused")
        return parsed, ascii_host, (str(direct),)
    resolver = resolver or socket.getaddrinfo
    try:
        rows = resolver(ascii_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"DNS resolution failed: {exc}") from exc
    addresses = tuple(dict.fromkeys(
        str(row[4][0]) for row in rows if row and len(row) > 4 and row[4]
    ))
    if not addresses:
        raise ValueError("DNS returned no addresses")
    unsafe = sorted(addr for addr in addresses if not _public_ip(addr))
    if unsafe:
        raise ValueError("hostname resolves to a private/reserved address")
    return parsed, ascii_host, addresses


def is_public_http_url(value: str, resolver: Callable | None = None) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a credential-free public HTTP(S) URL."""
    try:
        _resolve_public_http_url(value, resolver)
    except ValueError as exc:
        return False, str(exc)
    return True, "public host"


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose socket target is the address already validated."""

    def __init__(self, host: str, port: int, address: str, *, timeout: float):
        self._validated_address = address
        super().__init__(host, port=port, timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS equivalent that preserves hostname verification and TLS SNI."""

    def __init__(self, host: str, port: int, address: str, *, timeout: float,
                 context: ssl.SSLContext):
        self._validated_address = address
        super().__init__(host, port=port, timeout=timeout, context=context)

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _default_open(url: str, *, resolver: Callable | None = None,
                  timeout: float = _HTTP_TIMEOUT) -> tuple[str, str, str]:
    """Fetch one public text page and return ``(final_url, content_type, body)``.

    Redirects are handled manually so every hop goes through the same DNS/IP
    guard. The connection is pinned to the address that passed that guard, and
    the response body is capped before decoding.
    """
    current = str(url)
    tls_context = ssl.create_default_context()
    for _hop in range(6):
        try:
            parsed, host, addresses = _resolve_public_http_url(current, resolver)
        except ValueError as exc:
            raise ValueError(f"URL refused ({exc}): {current}") from exc
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += "?" + parsed.query
        request_target = urllib.parse.quote(
            request_target, safe="/%?:@&=+$,;[]!~*'()")
        response = None
        connection = None
        last_error: Exception | None = None
        for address in addresses:
            try:
                if parsed.scheme.lower() == "https":
                    connection = _PinnedHTTPSConnection(
                        host, port, address, timeout=timeout, context=tls_context)
                else:
                    connection = _PinnedHTTPConnection(
                        host, port, address, timeout=timeout)
                connection.request("GET", request_target, headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,text/plain,application/json",
                })
                response = connection.getresponse()
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                connection = None
        if response is None or connection is None:
            raise RuntimeError(f"public URL connection failed: {last_error}")
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError(
                        f"HTTP {response.status} redirect had no Location")
                current = urllib.parse.urljoin(current, location)
                continue
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"HTTP {response.status} {response.reason or 'request failed'}")
            ctype = str(response.headers.get_content_type() or "").lower()
            if ctype not in {"text/html", "text/plain", "application/json", "text/json"}:
                raise ValueError(f"non-text content type refused: {ctype or 'unknown'}")
            encoding = str(response.headers.get("Content-Encoding") or "").lower()
            if encoding not in {"", "identity"}:
                raise ValueError(f"encoded response refused: {encoding}")
            announced = response.headers.get("Content-Length")
            if announced and int(announced) > _MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the Scout page-size cap")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the Scout page-size cap")
            charset = response.headers.get_content_charset() or "utf-8"
            return current, ctype, raw.decode(charset, "replace")
        finally:
            response.close()
            connection.close()
    raise RuntimeError("too many redirects")


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._title_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._anchor_href = ""
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self._skip += 1
            return
        if self._skip:
            return
        if low == "title":
            self._title_depth += 1
        if low == "a":
            values = {str(k).lower(): str(v or "") for k, v in attrs}
            self._anchor_href = values.get("href", "")
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if low == "title":
            self._title_depth = max(0, self._title_depth - 1)
        if low == "a" and self._anchor_href:
            self.links.append((self._anchor_href, " ".join(self._anchor_parts)))
            self._anchor_href = ""
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._title_depth:
            self.title_parts.append(text)
        self.text_parts.append(text)
        if self._anchor_href:
            self._anchor_parts.append(text)


def _page_text(body: str, content_type: str) -> tuple[str, str, list[tuple[str, str]]]:
    if "json" in content_type:
        try:
            root = json.loads(body)
            text = json.dumps(root, indent=2, ensure_ascii=False)
        except ValueError:
            text = body
        return "JSON response", text[:_MAX_PAGE_TEXT], []
    if content_type == "text/plain":
        return "Text page", body[:_MAX_PAGE_TEXT], []
    parser = _PageParser()
    parser.feed(body)
    title = " ".join(parser.title_parts).strip()
    text = "\n".join(parser.text_parts)
    text = _html.unescape(re.sub(r"[ \t]+", " ", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text[:_MAX_PAGE_TEXT], parser.links


def _same_site(left: str, right: str) -> bool:
    def host(url: str) -> str:
        h = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
        return h[4:] if h.startswith("www.") else h
    return bool(host(left)) and host(left) == host(right)


def _candidate_links(page_url: str, links: Iterable[tuple[str, str]]) -> list[str]:
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw_href, anchor in links:
        href = urllib.parse.urljoin(page_url, raw_href)
        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme not in {"http", "https"} or not _same_site(page_url, href):
            continue
        clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        low_path = parsed.path.lower()
        if low_path.endswith(_SKIP_SUFFIXES) or clean in seen:
            continue
        seen.add(clean)
        signal = f"{anchor} {parsed.path} {parsed.query}"
        score = 10 if _FEATURE_TERMS.search(signal) else 0
        score -= min(5, parsed.path.count("/"))
        if parsed.query:
            score -= 1
        scored.append((score, clean))
    scored.sort(key=lambda row: (-row[0], len(row[1]), row[1]))
    return [url for _score, url in scored]


def crawl_public_program(
    start_url: str,
    *,
    prefix: str = "S",
    max_pages: int = 6,
    opener: Callable | None = None,
    resolver: Callable | None = None,
) -> dict:
    """Retrieve a program URL and its most relevant same-site feature pages.

    ``opener`` may return either ``(final_url, content_type, body)`` or a body
    string (the latter is convenient for deterministic tests).
    """
    max_pages = max(1, min(int(max_pages), 12))
    fetch = opener or (lambda url: _default_open(url, resolver=resolver))
    queue = [str(start_url).strip()]
    queued = set(queue)
    visited: set[str] = set()
    pages: list[dict] = []
    errors: list[dict] = []
    total = 0
    while queue and len(pages) < max_pages and total < _MAX_TOTAL_TEXT:
        requested = queue.pop(0)
        try:
            got = fetch(requested)
            if isinstance(got, tuple) and len(got) == 3:
                final_url, content_type, body = got
            else:
                final_url, content_type, body = requested, "text/html", str(got)
            if final_url in visited:
                continue
            visited.add(final_url)
            title, text, links = _page_text(str(body), str(content_type))
            if len(text.strip()) < 80:
                errors.append({"url": final_url, "error": "page yielded too little readable text"})
            else:
                remaining = _MAX_TOTAL_TEXT - total
                text = text[:remaining]
                evidence_id = f"{prefix}{len(pages) + 1}"
                pages.append({
                    "id": evidence_id,
                    "url": final_url,
                    "title": title or final_url,
                    "text": text,
                })
                total += len(text)
            for link in _candidate_links(final_url, links):
                if link not in queued and link not in visited:
                    queue.append(link)
                    queued.add(link)
        except Exception as exc:  # named evidence gap, never an invented page
            errors.append({"url": requested, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "reference": start_url,
        "kind": "website-url",
        "canonical_url": pages[0]["url"] if pages else start_url,
        "retrieved_at": _utc_now(),
        "evidence": pages,
        "coverage": {
            "attempted": len(visited) + len(errors),
            "retrieved": len(pages),
            "text_chars": total,
            "errors": errors,
            "complete": bool(pages),
        },
    }


def format_evidence_context(bundle: dict) -> str:
    """Render a bundle into model context with stable, citable identifiers."""
    rows = [
        f"REFERENCE: {bundle.get('reference')}",
        f"KIND: {bundle.get('kind')}",
        f"CANONICAL URL: {bundle.get('canonical_url') or ''}",
        f"RETRIEVED: {bundle.get('retrieved_at') or ''}",
    ]
    coverage = bundle.get("coverage") or {}
    rows.append("COVERAGE: " + json.dumps(coverage, ensure_ascii=False, default=str))
    for item in bundle.get("evidence") or []:
        evidence_id = str(item.get("id") or "?")
        ref = item.get("url") or item.get("path") or item.get("ref") or "unknown"
        title = item.get("title") or item.get("kind") or "evidence"
        rows.extend([
            "",
            f"[{evidence_id}] {title}",
            f"SOURCE: {ref}",
            str(item.get("text") or ""),
        ])
    return "\n".join(rows)


def _known_ids(bundle: dict, prefix: str) -> set[str]:
    return {
        str(row.get("id")) for row in (bundle.get("evidence") or [])
        if (str(row.get("id") or "").startswith(prefix)
            and str(row.get("kind") or "") != "search-ledger")
    }


def validate_profile_references(profile: dict, bundle: dict, prefix: str) -> dict:
    """Remove uncited profile rows and retain an explicit validation ledger."""
    root = dict(profile or {})
    known = _known_ids(bundle, prefix)
    rejected: list[dict] = []
    for field in ("workflows", "capabilities", "optimization_needs"):
        valid: list[dict] = []
        seen_names: set[str] = set()
        for raw in root.get(field) or []:
            if not isinstance(raw, dict):
                rejected.append({
                    "profile_field": field,
                    "value": raw,
                    "validation_error": "profile row is not an object",
                })
                continue
            row = dict(raw)
            reasons: list[str] = []
            refs = {str(x) for x in (row.get("evidence_refs") or [])}
            if not refs or not refs.issubset(known):
                reasons.append("evidence reference is missing or unknown")
            name_key = "need" if field == "optimization_needs" else "name"
            name = str(row.get(name_key) or "").strip()
            if not name:
                reasons.append(f"{name_key} is empty")
            folded = name.casefold()
            if folded and folded in seen_names:
                reasons.append(f"duplicate {field} row")
            if field == "workflows":
                behavior_key = "current_behavior" if "current_behavior" in row else "behavior"
                if not str(row.get(behavior_key) or "").strip():
                    reasons.append(f"{behavior_key} is empty")
            elif field == "capabilities":
                behavior_key = "current_behavior" if "current_behavior" in row else "behavior"
                if not str(row.get(behavior_key) or "").strip():
                    reasons.append(f"{behavior_key} is empty")
                if "behavior" in row:
                    for key in ("user_value", "implementation_pattern"):
                        if not str(row.get(key) or "").strip():
                            reasons.append(f"{key} is empty")
            else:
                if not str(row.get("why") or "").strip():
                    reasons.append("why is empty")
            if not reasons:
                valid.append(row)
                if folded:
                    seen_names.add(folded)
                continue
            row["validation_error"] = "; ".join(reasons)
            row["profile_field"] = field
            rejected.append(row)
        root[field] = valid
    gaps = list(root.get("coverage_gaps") or [])
    if rejected:
        gaps.append(
            f"{len(rejected)} uncited profile row(s) were rejected instead of treated as facts"
        )
    root["coverage_gaps"] = gaps
    root["evidence_validation"] = {
        "known_ids": sorted(known),
        "rejected_rows": rejected,
        "complete": not rejected,
    }
    return root


def validate_comparison(comparison: dict, target_bundle: dict,
                        source_bundle: dict, source_profile: dict | None = None) -> dict:
    """Fail closed on uncited or content-free model comparison rows.

    Invalid rows remain in ``rejected_rows`` with their exact reason so a thin
    model response can never look like a clean "no useful capabilities" result.
    """
    root = dict(comparison or {})
    target_ids = _known_ids(target_bundle, "T")
    source_ids = _known_ids(source_bundle, "S")
    accepted: list[dict] = []
    rejected: list[dict] = []
    for raw in root.get("recommendations") or []:
        if not isinstance(raw, dict):
            rejected.append({
                "value": raw,
                "validation_errors": ["comparison row is not an object"],
            })
            continue
        row = dict(raw)
        reasons: list[str] = []
        t_refs = {str(x) for x in (row.get("target_evidence_refs") or [])}
        s_refs = {str(x) for x in (row.get("source_evidence_refs") or [])}
        if not t_refs or not t_refs.issubset(target_ids):
            reasons.append("target evidence reference is missing or unknown")
        if not s_refs or not s_refs.issubset(source_ids):
            reasons.append("source evidence reference is missing or unknown")
        for field in (
            "capability", "source_behavior", "target_state", "target_gap",
            "purpose_alignment", "how_it_optimizes", "adaptation_plan",
            "verification_plan",
        ):
            if not str(row.get(field) or "").strip():
                reasons.append(f"{field} is empty")
        decision = str(row.get("decision") or "").lower()
        if decision not in {"adopt", "adapt", "reject", "investigate"}:
            reasons.append("decision is unknown")
        confidence = str(row.get("confidence") or "").lower()
        if confidence not in {"high", "medium", "low"}:
            reasons.append("confidence is unknown")
        try:
            score = -1 if isinstance(row.get("value_score"), bool) else int(
                row.get("value_score"))
        except (TypeError, ValueError):
            score = -1
        if not 0 <= score <= 100:
            reasons.append("value_score is outside 0..100")
        if decision in {"adopt", "adapt"}:
            if not str(row.get("implementation_search_query") or "").strip():
                reasons.append("accepted capability has no implementation search query")
            touchpoints = row.get("target_touchpoints")
            if (not isinstance(touchpoints, list)
                    or not any(str(item or "").strip() for item in touchpoints)):
                reasons.append("accepted capability has no target touchpoint")
        if reasons:
            row["validation_errors"] = reasons
            rejected.append(row)
        else:
            accepted.append(row)
    if source_profile is not None:
        expected = {
            str(item.get("name") or "").strip().casefold(): {
                "name": str(item.get("name") or "").strip(),
                "evidence_refs": {
                    str(ref) for ref in (item.get("evidence_refs") or [])
                },
            }
            for item in (source_profile.get("capabilities") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        reconciled: list[dict] = []
        seen: set[str] = set()
        for row in accepted:
            key = str(row.get("capability") or "").strip().casefold()
            reason = ""
            if key not in expected:
                reason = "capability is not in the scouted-program capability inventory"
            elif str(row.get("capability") or "").strip() != expected[key]["name"]:
                reason = "capability name does not exactly match the scouted inventory"
            elif key in seen:
                reason = "duplicate decision for one scouted-program capability"
            elif not ({str(ref) for ref in (row.get("source_evidence_refs") or [])}
                      & expected[key]["evidence_refs"]):
                reason = "decision does not cite evidence used to profile this capability"
            if reason:
                row["validation_errors"] = list(row.get("validation_errors") or []) + [reason]
                rejected.append(row)
            else:
                seen.add(key)
                reconciled.append(row)
        accepted = reconciled
        missing = [expected[key]["name"] for key in sorted(set(expected) - seen)]
    else:
        expected = {}
        missing = []
    root["recommendations"] = accepted
    root["rejected_rows"] = rejected
    gaps = list(root.get("coverage_gaps") or [])
    if missing:
        gaps.append(
            "comparison omitted scouted capability decision(s): " + ", ".join(missing)
        )
    root["coverage_gaps"] = gaps
    root["validation"] = {
        "target_evidence_ids": sorted(target_ids),
        "source_evidence_ids": sorted(source_ids),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "source_capabilities": [expected[key]["name"] for key in sorted(expected)],
        "unaccounted_source_capabilities": missing,
        "complete": not rejected and not missing,
    }
    return root
