"""Competitor research for the FlexFactor audit (owner order 2026-08-16).

    "it would probably be a good thing for flexfactor to have it too. Maybe
     allow flexfactor and factorydeck (and purpose foundry) by default also
     use scout and repo rewards as well their own web search for competitors."

FlexFactor audits a program against the job it was created to do. Until now it
had no idea what ANYONE ELSE had already built for that job, so a program could
be driven to 10/10 on its own acceptance criteria while shipping less than every
product its users could buy instead. This module closes that: it finds the
program's real competitors - market products AND inspectable open-source
implementations - extracts the single most valuable adoptable idea from each,
and hands those back as improvement candidates.

Three rules govern it, and each one exists because the obvious implementation
gets it wrong:

1. **The purpose contract stays the authority.** A competitor idea is a
   PROPOSAL, judged against the audited program's own purpose. "Competitor X
   has feature Y" is never a reason to build Y. `accept=False` ideas are
   reported, never bridged into fixes. Copying a competitor's roadmap is
   exactly the "make every program resemble the same generic application"
   failure the portfolio directive forbids.

2. **The licence decides the reuse mode, mechanically.** `license_reuse_mode`
   is a pure function of (SPDX id, is there source at all). Permissive and
   verified-compatible => source may be read and reused. Copyleft/incompatible,
   or a closed-source product => CLEAN ROOM: the documented public behaviour is
   the only input, never the source. Unknown/unverifiable licence =>
   REFERENCE ONLY: record that they do it, derive nothing. `may_copy_source()`
   is True for exactly one mode and is the single place that answer lives.

3. **A missing source is a NAMED SKIP, never silence and never an invention.**
   Every search backend that fails lands in `sources_skipped` with the reason,
   goes into the report, and the run continues. A competitor the model named but
   that no reachable source corroborated is kept, marked
   `evidence_status="unverified"`, excluded from the verified count, and barred
   from bridging. Fewer than the target number of competitors is SAID OUT LOUD
   in `coverage_note` rather than padded.

Stdlib only, and it never imports flexfactor: the caller injects the judging
callable (`judge(system, prompt, schema) -> dict`), the Repo Rewards search
function, and - in tests - the URL opener. Same containment contract as
flexfactor_prodready.py.
"""
from __future__ import annotations

import concurrent.futures
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "REUSE_DIRECT", "REUSE_CLEAN_ROOM", "REUSE_REFERENCE",
    "license_reuse_mode", "may_copy_source", "may_bridge",
    "coverage_note", "web_search", "github_repo_search",
    "research_competitors", "competitor_findings", "report_lines",
    "DEFAULT_TARGET", "DEFAULT_FIX_STREAM_CAP",
]

DEFAULT_TARGET = 5
DEFAULT_FIX_STREAM_CAP = 5
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FlexFactor-competitor-research"
_HTTP_TIMEOUT = 25.0
_FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"

# The caller may install flexfactor's own licence oracle (`_license_compatible`)
# so there is a SINGLE source of truth at runtime and the scout integrate gate
# can never drift from the competitor reuse gate. The module-level default below
# keeps this file usable standalone.
_COMPATIBLE_ORACLE: list = [None]


def set_license_oracle(fn) -> None:
    """flexfactor installs `_license_compatible` here at import time."""
    _COMPATIBLE_ORACLE[0] = fn


# --------------------------------------------------------------------------- #
# 1. THE LEGAL GATE. Pure, deterministic, and the only thing allowed to decide
#    whether a competitor's source may be touched.
# --------------------------------------------------------------------------- #
REUSE_DIRECT = "direct-code-reuse"
REUSE_CLEAN_ROOM = "clean-room-from-documented-behavior"
REUSE_REFERENCE = "reference-only"

# Kept deliberately identical to flexfactor._LICENSE_COMPATIBLE /
# _LICENSE_INCOMPATIBLE. `license_reuse_mode` prefers the caller's oracle when
# one is injected; this table is the standalone fallback, and a drift test in
# flexfactor_tests.py fails if the two disagree.
_PERMISSIVE = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "0bsd",
    "unlicense", "cc0-1.0", "zlib", "mpl-2.0", "python-2.0", "bsl-1.0",
}
_COPYLEFT = {
    "gpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-2.1", "lgpl-3.0",
    "sspl-1.0", "busl-1.1", "cc-by-nc-4.0", "proprietary",
}


def _default_compatible(spdx: str | None) -> bool | None:
    """True = verified permissive, False = verified copyleft/restricted,
    None = unknown. Unknown NEVER means permitted."""
    if not spdx:
        return None
    s = str(spdx).strip().lower()
    if s in ("noassertion", "unknown", "none", "other"):
        return None
    if s in _PERMISSIVE:
        return True
    if s in _COPYLEFT or s.startswith(("gpl", "agpl", "lgpl", "sspl")):
        return False
    return None


def license_reuse_mode(spdx: str | None, source_available: bool = True,
                       compatible_fn=None) -> tuple[str, str]:
    """(reuse_mode, human reason). The ONLY licence decision in this module.

    - verified permissive + source exists  -> direct code reuse is permitted
    - verified copyleft/restricted, or a closed-source product with no
      inspectable source at all             -> clean room from PUBLIC DOCS only
    - anything unverifiable                 -> reference only, derive nothing

    Note the asymmetry that makes this safe: a *known bad* licence still allows
    clean-room work from published behaviour, while an *unknown* licence allows
    less, because we cannot even establish what reading the source would oblige.
    """
    fn = compatible_fn or _COMPATIBLE_ORACLE[0] or _default_compatible
    label = (str(spdx).strip() if spdx else "") or "UNKNOWN"
    if not source_available:
        return (REUSE_CLEAN_ROOM,
                f"no inspectable source (licence {label}); only publicly "
                "documented behaviour may inform our own independent design")
    verdict = fn(spdx)
    if verdict is True:
        return (REUSE_DIRECT,
                f"licence {label} is permissive and compatible; source may be "
                "read and adapted with attribution")
    if verdict is False:
        return (REUSE_CLEAN_ROOM,
                f"licence {label} is copyleft/restricted; source must NOT be "
                "copied - work from documented behaviour only")
    return (REUSE_REFERENCE,
            f"licence {label} could not be verified; record the capability as a "
            "reference and copy nothing")


def may_copy_source(reuse_mode: str) -> bool:
    """Exactly one mode permits reading and reusing the competitor's source."""
    return reuse_mode == REUSE_DIRECT


def may_bridge(competitor: dict) -> bool:
    """May this competitor's idea enter the audit's FIX stream?

    Requires (a) a corroborated competitor - a name the model recalled but no
    reachable source confirmed is a hypothesis, and a hypothesis must never
    drive an edit - and (b) a reuse mode that permits deriving anything at all.
    REFERENCE-ONLY candidates are reported and never applied.
    """
    if competitor.get("evidence_status") != "verified":
        return False
    return competitor.get("reuse_mode") in (REUSE_DIRECT, REUSE_CLEAN_ROOM)


def coverage_note(verified: int, target: int = DEFAULT_TARGET,
                  unverified: int = 0) -> str:
    """Say the shortfall out loud. Padding a thin result is the failure mode."""
    if verified >= target:
        return (f"{verified} competitor(s) covered with corroborating sources "
                f"(target {target}).")
    extra = (f" {unverified} further name(s) were proposed but NO reachable "
             "source corroborated them; they are listed as unverified and were "
             "not acted on.") if unverified else ""
    return (f"ONLY {verified} of the target {target} competitors could be "
            f"corroborated from a reachable source. This is a coverage SHORTFALL, "
            f"not evidence that fewer competitors exist.{extra}")


# --------------------------------------------------------------------------- #
# 2. Keyless search backends. Each returns a list of {title,url,snippet} and
#    raises nothing: failures are reported to the caller as a named skip.
# --------------------------------------------------------------------------- #
def _default_opener(url: str, data: bytes | None = None,
                    headers: dict | None = None,
                    timeout: float = _HTTP_TIMEOUT) -> str:
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": _UA, **(headers or {})},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(400_000)
    return raw.decode("utf-8", "replace")


# Keep an immutable identity for the production transport. Test harnesses and
# callers deliberately replace ``_default_opener`` with an offline/instrumented
# transport; comparing against the mutable module global would mistake that
# injection for production and silently route around it.
_PRODUCTION_OPENER = _default_opener


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials bound to the explicitly configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_firecrawl_opener(url: str, data: bytes | None = None,
                              headers: dict | None = None,
                              timeout: float = _HTTP_TIMEOUT) -> str:
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": _UA, **(headers or {})},
        method="POST" if data else "GET")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(400_000)
    return raw.decode("utf-8", "replace")


def _http_url_has_host(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _firecrawl(query: str, limit: int, opener, *,
               allow_credentials: bool = True) -> list[dict]:
    """Firecrawl v2 search, authenticated when a key is configured.

    Firecrawl is the freshest evidence source in the search ladder, but it is
    optional.  A missing key still gets one keyless attempt; any HTTP, schema,
    or account failure is surfaced by :func:`web_search` as a named skip before
    the keyless fallbacks run.  No synthetic result is ever substituted.
    """
    custom_endpoint = (os.environ.get("FLEXFACTOR_FIRECRAWL_URL") or "").strip()
    endpoint = custom_endpoint or _FIRECRAWL_SEARCH_URL
    if not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("FLEXFACTOR_FIRECRAWL_URL must be an http(s) URL")

    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    # Never forward the general Firecrawl cloud credential to an arbitrary
    # operator-configured host. Custom/self-hosted endpoints have an explicitly
    # scoped credential; they can also be keyless. The official cloud endpoint
    # requires its documented bearer token, so fail locally rather than spend
    # an HTTP timeout on a request that cannot authenticate.
    if custom_endpoint:
        key = (os.environ.get("FLEXFACTOR_FIRECRAWL_API_KEY") or "").strip()
    else:
        key = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("FIRECRAWL_API_KEY is not set")
    if key and not allow_credentials:
        raise RuntimeError(
            "credentialed Firecrawl is disabled unless paid research is explicit"
        )
    if key and not endpoint.startswith("https://"):
        raise RuntimeError("credentialed Firecrawl endpoints require HTTPS")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps({
        "query": query,
        "limit": max(1, min(int(limit), 20)),
        "sources": ["web"],
        "safe": True,
    }).encode("utf-8")
    request = (_default_firecrawl_opener
               if opener is _PRODUCTION_OPENER else opener)
    root = json.loads(request(endpoint, payload, headers))
    if not isinstance(root, dict) or root.get("success") is not True:
        raise RuntimeError("Firecrawl v2 reported failure or invalid JSON shape")

    data = root.get("data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("web"), list):
        items = data["web"]
    elif isinstance(root.get("web"), list):
        items = root["web"]
    else:
        raise RuntimeError("Firecrawl v2 returned an unrecognized result shape")

    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Firecrawl v2 returned a non-object result")
        url = str(item.get("url") or "").strip()
        if not _http_url_has_host(url):
            raise RuntimeError("Firecrawl v2 returned a result without a valid URL")
        if url in seen:
            continue
        seen.add(url)
        title = str(item.get("title") or url).strip()
        snippet = str(item.get("description") or item.get("snippet") or
                      item.get("markdown") or "").strip()
        out.append({"title": title, "url": url, "snippet": snippet[:4000]})
        if len(out) >= limit:
            break
    return out


_DDG_AD_MARKERS = ("duckduckgo.com/y.js", "duckduckgo.com/duckduckgo-help-pages")


def _ddg_lite(query: str, limit: int, opener) -> list[dict]:
    """DuckDuckGo Lite - keyless, no account, returns organic results.

    (The `html.duckduckgo.com` endpoint answers 202 with a challenge page from
    this machine and yields no results; `lite` answers 200 with real ones. Do
    not "simplify" this back to the html endpoint.)
    """
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(query)
    body = opener(url)
    out: list[dict] = []
    for href, text in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        target = href
        if "uddg=" in href:  # DDG redirector: the real URL is the uddg param
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(
                href if href.startswith("http") else "https:" + href).query)
            target = (qs.get("uddg") or [""])[0]
        if not target.startswith("http"):
            continue
        if any(m in target for m in _DDG_AD_MARKERS):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if not title or len(title) < 3:
            continue
        out.append({"title": title, "url": target, "snippet": ""})
        if len(out) >= limit:
            break
    return out


def _wikipedia(query: str, limit: int, opener) -> list[dict]:
    """Last-resort keyless backend: encyclopaedic coverage of market products."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
           "&srlimit=%d&srsearch=%s" % (limit, urllib.parse.quote_plus(query)))
    data = json.loads(opener(url))
    out = []
    for hit in ((data.get("query") or {}).get("search") or [])[:limit]:
        title = hit.get("title") or ""
        out.append({
            "title": title,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            "snippet": html.unescape(re.sub(r"<[^>]+>", "", hit.get("snippet") or "")),
        })
    return out


def _searxng(query: str, limit: int, opener) -> list[dict]:
    """Self-hosted SearXNG, when the owner points at one (FLEXFACTOR_SEARXNG_URL)."""
    base = (os.environ.get("FLEXFACTOR_SEARXNG_URL") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("FLEXFACTOR_SEARXNG_URL is not set")
    url = f"{base}/search?format=json&q=" + urllib.parse.quote_plus(query)
    data = json.loads(opener(url))
    return [{"title": r.get("title") or "", "url": r.get("url") or "",
             "snippet": r.get("content") or ""}
            for r in (data.get("results") or [])[:limit] if r.get("url")]


# Ordered ladder. Firecrawl supplies current market evidence first (with or
# without an account key), then the owner-hosted/keyless fallbacks preserve an
# offline-capable path instead of turning a provider outage into fake coverage.
_WEB_BACKENDS = (("firecrawl", _firecrawl), ("searxng", _searxng),
                 ("duckduckgo", _ddg_lite),
                 ("wikipedia", _wikipedia))


def web_search(query: str, limit: int = 6, opener=None, *,
               allow_credentialed_firecrawl: bool = True
               ) -> tuple[list[dict], str, dict]:
    """(results, backend_used, skipped{backend: reason}).

    Tries each keyless backend in order and returns the FIRST that yields
    results. Every backend that failed or came back empty is named in
    `skipped` so the report can say which door was tried and what it said -
    "no reachable source" is a finding, not a blank.
    """
    opener = opener or _default_opener
    skipped: dict[str, str] = {}
    for name, fn in _WEB_BACKENDS:
        try:
            if name == "firecrawl":
                hits = fn(
                    query,
                    limit,
                    opener,
                    allow_credentials=allow_credentialed_firecrawl,
                )
            else:
                hits = fn(query, limit, opener)
        except Exception as ex:  # any transport/parse failure -> named skip
            skipped[name] = f"{type(ex).__name__}: {ex}"
            continue
        if hits:
            return hits, name, skipped
        skipped[name] = "reachable but returned 0 results"
    return [], "", skipped


def github_repo_search(query: str, limit: int = 5, opener=None) -> list[dict]:
    """Keyless GitHub repository search.

    This is the licence oracle for open-source competitors: the API reports
    `license.spdx_id` directly, which is what `license_reuse_mode` needs. A
    GITHUB_TOKEN/GH_TOKEN in the environment is used when present purely to
    raise the rate limit; it is never required.
    """
    opener = opener or _default_opener
    url = ("https://api.github.com/search/repositories?per_page=%d&sort=stars&q=%s"
           % (limit, urllib.parse.quote_plus(query)))
    headers = {"Accept": "application/vnd.github+json"}
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.loads(opener(url, None, headers))
    out = []
    for item in (data.get("items") or [])[:limit]:
        lic = item.get("license") or {}
        out.append({
            "name": item.get("full_name") or item.get("name") or "",
            "url": item.get("html_url") or "",
            "description": item.get("description") or "",
            "stars": item.get("stargazers_count"),
            "license": lic.get("spdx_id") or None,
            "source": "github",
        })
    return out


# --------------------------------------------------------------------------- #
# 3. Model schemas. Discovery names the field; extraction pulls ONE idea out of
#    each competitor and judges it against this program's purpose.
# --------------------------------------------------------------------------- #
DISCOVERY_SYSTEM = (
    "You are a product strategist naming the real competitors of a program. "
    "Name products and open-source projects that a user of THIS program could "
    "realistically use INSTEAD of it. Cover both commercial/market products and "
    "inspectable open-source implementations. Use only competitors you actually "
    "believe exist and can be found by a web search - never invent a plausible "
    "sounding name to fill the list; returning fewer real competitors is "
    "correct and expected. Respond with JSON only."
)

DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "competitors": {
            "type": "array",
            "description": "Real competing products/projects, best-known first.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Product or project name."},
                    "kind": {"type": "string", "enum": ["market", "oss"],
                             "description": "market = commercial product; oss = open-source project."},
                    "search_query": {"type": "string",
                                     "description": "A web search that would find this competitor."},
                },
                # Kept to three fields on purpose: the cheap judge tier drops
                # fields from long schemas, and a dropped field pushes the
                # bare-list salvage below its coverage threshold, which throws
                # away an answer that named the competitors correctly.
                "required": ["name", "kind", "search_query"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["competitors"],
    "additionalProperties": False,
}

IDEA_SYSTEM = (
    "You are a staff engineer extracting the single most valuable idea a "
    "competitor has that the audited program lacks, and then deciding - "
    "strictly - whether adopting it SERVES THE AUDITED PROGRAM'S OWN STATED "
    "PURPOSE. The purpose contract is the authority: an idea that would make "
    "this program more like a generic competitor without advancing its own "
    "stated job must be REJECTED (accept=false), no matter how good the idea "
    "is in the abstract. Ground every claim in the evidence supplied; if the "
    "evidence does not establish that the competitor has this capability, say "
    "so in `evidence_basis` and lower confidence. Never describe the "
    "competitor's source code - you have not read it. EVERY field is REQUIRED "
    "and must be short and concrete: an answer with an empty `idea_title`, "
    "`why_valuable` or `purpose_reason` is a FAILED answer and is discarded. "
    "Respond with JSON only."
)

IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "idea_title": {"type": "string", "description": "Short name of the adoptable idea."},
        "what_it_does": {"type": "string",
                         "description": "The competitor's capability, in behaviour terms only."},
        "why_valuable": {"type": "string",
                         "description": "Concretely, what adopting it would do for THIS program."},
        "evidence_basis": {"type": "string",
                           "description": "Which supplied evidence establishes the competitor has this."},
        "accept": {"type": "boolean",
                   "description": "true only if adopting this advances the program's OWN stated purpose."},
        "purpose_reason": {"type": "string",
                           "description": "Why it does or does not serve the stated purpose."},
        "acceptance_ref": {"type": "string",
                           "description": "The acceptance criterion it serves, or '' if none."},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"],
                     "description": "How badly the program needs this to do its job."},
        "code_fixable": {"type": "boolean",
                         "description": "true if a code change in this repo could deliver it."},
        "file": {"type": "string",
                 "description": "Repo-relative file the change would start in, or '' if unknown."},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"],
                       "description": "Confidence that the competitor really has this capability."},
    },
    "required": ["idea_title", "what_it_does", "why_valuable", "evidence_basis",
                 "accept", "purpose_reason", "acceptance_ref", "severity",
                 "code_fixable", "file", "confidence"],
    "additionalProperties": False,
}


def _ascii(text) -> str:
    """Console-safe rendering of MODEL- and REPO-derived text.

    Live 2026-08-16: a discovery answer containing U+2011 (non-breaking hyphen)
    was echoed into an error message, and `print` raised UnicodeEncodeError on
    this machine's cp1252 console - from INSIDE an except handler, so the
    exception escaped `research_competitors` entirely and would have failed the
    whole program's audit. Third-party text must never be printed raw.
    """
    return str(text).encode("ascii", "replace").decode("ascii")


def _fence(label: str, text: str) -> str:
    """Untrusted third-party text always arrives fenced and labelled."""
    body = (text or "").replace("```", "'''")
    return f"```{label}\n{body}\n```"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# Search-engine chrome that is not evidence of anything (DDG's own logo/help
# links came back as "evidence URLs" in the first live run).
_NON_EVIDENCE_HOSTS = ("duckduckgo.com", "lite.duckduckgo.com", "html.duckduckgo.com")


def _is_evidence_url(url: str | None) -> bool:
    if not url or not _http_url_has_host(url):
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    return host.lower() not in _NON_EVIDENCE_HOSTS


def _attributable_repo(name: str, repos: list[dict]) -> dict | None:
    """The GitHub repo that is plausibly THE COMPETITOR'S OWN, or None.

    This is the licence oracle, so it is the highest-consequence match in the
    module and it fails CLOSED. Live 2026-08-16, first run: searching "Logos
    Bible Software" (Faithlife's proprietary product) surfaced a third party's
    `robrawks/LogosBibleSoftwareMCP`, whose MIT licence was then attributed to
    Logos and produced `direct-code-reuse` for a closed-source commercial
    product. A repo merely NAMED after a product is not that product.

    So the OWNER must match the competitor name - the one signal that a
    repository actually belongs to the thing it is named after. Anything else is
    kept as evidence but leaves `source_available=False`, which resolves to
    clean-room-from-documented-behaviour: the honest answer for a product whose
    source we cannot identify.
    """
    want = _norm(name)
    if not want:
        return None
    for r in repos or []:
        owner = _norm(str(r.get("name") or "").split("/")[0])
        if not owner:
            continue
        if owner == want or owner in want or want in owner:
            return r
    return None


def _name_related(name: str, repo: dict) -> bool:
    """Is this repo at least NAMED after the competitor? Evidence-grade only -
    the licence oracle is `_attributable_repo`, which is strictly stronger."""
    want = _norm(name)
    if not want:
        return False
    full = _norm(str(repo.get("name") or ""))
    tail = _norm(str(repo.get("name") or "").split("/")[-1])
    return bool(tail) and (want in full or tail in want)


_IDEA_REQUIRED_TEXT = ("idea_title", "why_valuable")


def _normalize_idea(idea: dict, competitor_name: str) -> tuple[dict, str | None]:
    """(idea, failure_reason). An idea missing its substance is NOT an idea.

    Live 2026-08-16: the cheap judge tier returned objects carrying `accept` and
    a long `evidence_basis` but no `idea_title`, `why_valuable` or
    `purpose_reason` - and the report rendered them as "ACCEPTED - (none)". An
    empty accepted idea is worse than no idea: it is an unfalsifiable claim that
    something should change. Incomplete output is forced to accept=False and
    named as a skip.
    """
    out = dict(idea or {})
    missing = [k for k in _IDEA_REQUIRED_TEXT if not str(out.get(k) or "").strip()]
    # `confidence` is an enum in the schema but models return floats; normalize
    # rather than reject - it is descriptive, never load-bearing.
    if out.get("confidence") is not None:
        out["confidence"] = str(out["confidence"])
    if not missing:
        return out, None
    reason = ("model returned an incomplete idea (missing "
              + ", ".join(missing) + ") - forced to accept=False")
    out["accept"] = False
    out.setdefault("idea_title", "(incomplete - not acted on)")
    out["purpose_reason"] = f"NOT ACTED ON: {reason}. " + str(out.get("purpose_reason") or "")
    return out, f"{reason} for {competitor_name}"


# --------------------------------------------------------------------------- #
# 4. The orchestrator.
# --------------------------------------------------------------------------- #
def research_competitors(judge, program_name: str, purpose_blob: str,
                         stack=None, *, rr_search=None, rr_endpoint: str = "",
                         target: int = DEFAULT_TARGET, opener=None,
                         log=print, max_workers: int = 4,
                         file_list=None, author=None,
                         allow_credentialed_firecrawl: bool = False) -> dict:
    """Find competitors, extract one adoptable idea each, judge against purpose.

    `judge(system, prompt, schema) -> dict` is injected (flexfactor routes it to
    the cheap judge tier). `author(system, prompt, schema) -> dict` is the
    STRONG tier, used only for idea EXTRACTION: the cheap model reliably fills
    the prose fields but omits `severity`/`code_fixable`/`file`, so every idea
    was dropped at the severity floor and competitor research was effectively
    report-only (measured: 0 of 8 bridged). Extraction decides whether a
    competitor's best idea can actually be built, so it gets the model that can
    answer that; benefit scoring and purpose judging stay cheap. Falls back to
    `judge` when no author callable is supplied.
    `rr_search(query) -> [RankedResult]` is the existing
    Repo Rewards client, also injected - this module never speaks to Repo
    Rewards directly, so there is exactly one RR client in the codebase.

    Never raises: every failure becomes a named entry in `sources_skipped`.
    """
    opener = opener or _default_opener
    stack = stack or []
    research: dict = {
        "target": target, "competitors": [], "sources_used": [],
        "sources_skipped": {}, "rr_endpoint": rr_endpoint or "(not used)",
        "verified": 0, "unverified": 0, "accepted": 0, "rejected": 0,
        "coverage_note": "", "queries": [],
    }
    stack_txt = ", ".join(str(s) for s in stack) if stack else "(unknown)"

    # -- 4a. Who are the competitors? ---------------------------------------
    # ONE bounded retry, because the observed failure is an ENVELOPE failure,
    # not a capability failure: live 2026-08-16 the model answered with a bare
    # JSON ARRAY of competitors, which the structured-output checker rejected
    # ("expected a JSON object, got list") after its bare-list salvage scored
    # the element keys below threshold. The competitors were right there; only
    # the wrapper was missing. The retry says so in the prompt.
    base_prompt = (
        f"PROGRAM: {program_name}\nSTACK: {stack_txt}\n\n"
        "This is the program's purpose and the job it must do:\n"
        + _fence("purpose", (purpose_blob or "")[:8000]) + "\n\n"
        f"Name up to {target + 3} real competitors, best-known first. "
        "Include both commercial products and open-source projects.")
    named: list[dict] = []
    last_err: Exception | None = None
    for prompt in (base_prompt,
                   base_prompt + "\n\nReturn a JSON OBJECT whose single key is "
                                 "`competitors` holding the array - NOT a bare "
                                 "array. Every item needs `name`, `kind` "
                                 "(\"market\" or \"oss\") and `search_query`."):
        try:
            disc = judge(DISCOVERY_SYSTEM, prompt, DISCOVERY_SCHEMA)
            named = [c for c in (disc.get("competitors") or []) if c.get("name")]
        except Exception as ex:
            last_err = ex
            continue
        if named:
            last_err = None
            break
    if last_err is not None or not named:
        why = (f"{type(last_err).__name__}: {_ascii(last_err)}" if last_err
               else "model named no competitors")
        research["sources_skipped"]["model-discovery"] = why
        log(f"  competitor discovery failed (non-fatal): {why}")

    probe_only = False
    if not named:
        # No model names: fall back to a generic market query so the web/RR
        # backends still get a chance instead of the whole phase evaporating.
        # It is a PROBE, not a competitor: if nothing corroborates it, it is
        # dropped rather than reported as a competitor literally called
        # "<program> alternatives competitors" (seen live 2026-08-16).
        probe_only = True
        named = [{"name": "", "kind": "market",
                  "search_query": f"{program_name} alternatives competitors"}]

    # -- 4b. Corroborate each name from reachable sources --------------------
    merged: dict[str, dict] = {}
    web_skips: dict[str, str] = {}
    for cand in named[:target + 3]:
        name = (cand.get("name") or "").strip()
        query = (cand.get("search_query") or name or "").strip()
        if not query:
            continue
        research["queries"].append(query)
        hits, backend, skips = web_search(
            query,
            limit=4,
            opener=opener,
            allow_credentialed_firecrawl=allow_credentialed_firecrawl,
        )
        web_skips.update({k: v for k, v in skips.items() if k not in web_skips})
        if backend and f"web:{backend}" not in research["sources_used"]:
            research["sources_used"].append(f"web:{backend}")
        gh: list[dict] = []
        try:
            gh = github_repo_search(name or query, limit=3, opener=opener)
            if gh and "github" not in research["sources_used"]:
                research["sources_used"].append("github")
        except Exception as ex:
            research["sources_skipped"].setdefault(
                "github", f"{type(ex).__name__}: {ex}")

        evidence_urls = [h["url"] for h in hits[:3] if _is_evidence_url(h.get("url"))]
        repo = _attributable_repo(name, gh)
        # A GitHub hit that could NOT be attributed to the competitor is still
        # useful EVIDENCE - but only if it is at least NAMED after the
        # competitor, so a keyword-match like `Taoviqinvicible/Tools-termux`
        # (a real hit for "Bible.js", live 2026-08-16) is not listed as evidence
        # for a product it has nothing to do with. It never donates a licence
        # either way (see _attributable_repo).
        for r in gh[:2]:
            if r.get("url") and r["url"] not in evidence_urls and _name_related(name, r):
                evidence_urls.append(r["url"])
        if repo:
            evidence_urls.insert(0, repo["url"])
            evidence_urls = list(dict.fromkeys(evidence_urls))

        source_available = repo is not None
        spdx = (repo or {}).get("license")
        mode, reason = license_reuse_mode(spdx, source_available=source_available,
                                          compatible_fn=None)
        key = _norm(name) or _norm(query)
        merged[key] = {
            "name": name or (hits[0]["title"] if hits else query),
            # `kind` is inferred, not trusted: a weak judge model routinely omits
            # it, and the default must not claim a closed product is open source.
            "kind": ("oss" if repo else "market"),
            "why": cand.get("why") or "",
            "url": (repo or {}).get("url") or (evidence_urls[0] if evidence_urls else ""),
            "evidence_urls": evidence_urls[:4],
            "evidence_titles": [h.get("title", "") for h in hits[:3]],
            "evidence_snippets": [h.get("snippet", "") for h in hits[:3]],
            "repo_description": (repo or {}).get("description") or "",
            "stars": (repo or {}).get("stars"),
            "license": spdx or "UNKNOWN",
            "license_source": ("github-api" if repo else
                               "none (no repository could be attributed to this "
                               "competitor)"),
            "reuse_mode": mode,
            "reuse_reason": reason,
            "discovery_source": ("web+github" if (hits and repo) else
                                 "github" if repo else "web" if hits else "model"),
            "evidence_status": "verified" if evidence_urls else "unverified",
        }

    # -- 4c. Repo Rewards: open-source competitors with real metadata --------
    if rr_search is not None:
        rr_queries = [q for q in research["queries"][:3]] or [f"{program_name} alternative"]
        got_any = False
        for q in rr_queries:
            try:
                results = rr_search(q) or []
            except Exception as ex:
                research["sources_skipped"].setdefault(
                    "repo-rewards", f"{type(ex).__name__}: {ex}")
                break
            got_any = got_any or bool(results)
            for r in results[:4]:
                repo = (r or {}).get("repo") or {}
                full = repo.get("fullName") or ""
                if not full:
                    continue
                key = _norm(full.split("/")[-1])
                if key in merged:   # already found by name; keep the richer entry
                    merged[key].setdefault("evidence_urls", [])
                    if repo.get("htmlUrl") and repo["htmlUrl"] not in merged[key]["evidence_urls"]:
                        merged[key]["evidence_urls"].append(repo["htmlUrl"])
                    continue
                spdx = repo.get("licenseSpdx")
                mode, reason = license_reuse_mode(
                    spdx, source_available=True, compatible_fn=None)
                merged[key] = {
                    "name": full, "kind": "oss", "why": "",
                    "url": repo.get("htmlUrl") or "",
                    "evidence_urls": [u for u in [repo.get("htmlUrl")] if u],
                    "evidence_titles": [full],
                    "evidence_snippets": [repo.get("description") or ""],
                    "repo_description": repo.get("description") or "",
                    "stars": repo.get("stars"),
                    "license": spdx or "UNKNOWN",
                    "license_source": "repo-rewards",
                    "reuse_mode": mode, "reuse_reason": reason,
                    "discovery_source": "repo-rewards",
                    "evidence_status": "verified" if repo.get("htmlUrl") else "unverified",
                }
        if got_any and "repo-rewards" not in research["sources_used"]:
            research["sources_used"].append("repo-rewards")
        elif not got_any:
            research["sources_skipped"].setdefault(
                "repo-rewards", "reachable but returned 0 results")
    else:
        research["sources_skipped"].setdefault(
            "repo-rewards", "no Repo Rewards endpoint was reachable")

    for k, v in web_skips.items():
        research["sources_skipped"].setdefault(f"web:{k}", v)

    competitors = list(merged.values())
    if probe_only:
        # The generic probe only earns a place if a real source answered it.
        competitors = [c for c in competitors if c["evidence_status"] == "verified"]
    # Corroborated competitors first; then best-known (stars) first.
    competitors.sort(key=lambda c: (c["evidence_status"] != "verified",
                                    -(c.get("stars") or 0)))
    competitors = competitors[:target]

    if not competitors:
        research["coverage_note"] = coverage_note(0, target, 0)
        return research

    # -- 4d. One adoptable idea per competitor, judged against the purpose ---
    files_hint = ""
    if file_list:
        files_hint = ("\nFILES IN THIS REPO (choose `file` from these when the "
                      "change is code-fixable):\n"
                      + _fence("files", "\n".join(list(file_list)[:200])))

    def _extract(c: dict) -> dict:
        ev = "\n".join(filter(None, [
            f"URL: {c['url']}" if c.get("url") else "",
            f"LICENCE (from {c['license_source']}): {c['license']}",
            f"REUSE MODE ENFORCED FOR THIS CANDIDATE: {c['reuse_mode']} - {c['reuse_reason']}",
            f"DESCRIPTION: {c['repo_description']}" if c.get("repo_description") else "",
            "SEARCH EVIDENCE:",
            *[f"- {t} :: {u}" for t, u in zip(c.get("evidence_titles") or [],
                                              c.get("evidence_urls") or [])],
            *[s for s in (c.get("evidence_snippets") or []) if s],
        ]))
        prompt = (
            f"AUDITED PROGRAM: {program_name}\nSTACK: {stack_txt}\n\n"
            "The audited program's purpose contract (THE AUTHORITY):\n"
            + _fence("purpose", (purpose_blob or "")[:8000]) + "\n\n"
            f"COMPETITOR: {c['name']} ({c['kind']})\n"
            + _fence("competitor-evidence", ev) + files_hint + "\n\n"
            "Extract the single most valuable idea this competitor has that the "
            "audited program lacks, then decide whether adopting it serves the "
            "audited program's OWN stated purpose."
        )
        # ONE bounded retry when the first answer comes back without its
        # substance. Measured 2026-08-16 on the free judge tier: the model
        # returned `accept` plus a long `evidence_basis` and nothing else, so
        # without this every competitor rendered as "ACCEPTED - (none)". The
        # retry states the requirement in the prompt rather than the schema,
        # which is what the weak tier actually honours. It is bounded at one:
        # a model that ignores it twice is degraded honestly, not looped on.
        attempts = (prompt,
                    prompt + "\n\nYour previous answer omitted required fields. "
                             "Answer again and fill EVERY field, especially "
                             "`idea_title`, `what_it_does`, `why_valuable` and "
                             "`purpose_reason`. Keep each under 40 words.")
        last: dict = {}
        for text in attempts:
            try:
                last = (author or judge)(IDEA_SYSTEM, text, IDEA_SCHEMA)
            except Exception as ex:
                return {"error": f"{type(ex).__name__}: {ex}", "accept": False,
                        "idea_title": "(idea extraction failed)",
                        "purpose_reason": f"not judged: {ex}"}
            if not _normalize_idea(last, c["name"])[1]:
                return last
        return last

    workers = max(1, min(max_workers, len(competitors)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        ideas = list(pool.map(_extract, competitors))

    for c, raw in zip(competitors, ideas):
        # An idea missing its own substance is not an idea; it is forced to
        # accept=False and named, never rendered as "ACCEPTED - (none)".
        idea, incomplete = _normalize_idea(raw, c["name"])
        c["idea"] = idea
        if incomplete:
            research["sources_skipped"].setdefault(f"idea:{c['name']}", incomplete)
        # An unverified competitor can never be "accepted" for action: we have
        # no evidence the product exists, so its idea is a hypothesis about a
        # hypothesis. It is still reported.
        if c["evidence_status"] != "verified" and idea.get("accept"):
            idea["accept"] = False
            idea["purpose_reason"] = (
                "NOT ACTED ON: no reachable source corroborated this competitor. "
                + str(idea.get("purpose_reason") or ""))
        if idea.get("error"):
            research["sources_skipped"].setdefault(
                f"idea:{c['name']}", idea["error"])

    research["competitors"] = competitors
    research["verified"] = sum(1 for c in competitors if c["evidence_status"] == "verified")
    research["unverified"] = len(competitors) - research["verified"]
    research["accepted"] = sum(1 for c in competitors if (c.get("idea") or {}).get("accept"))
    research["rejected"] = len(competitors) - research["accepted"]
    research["coverage_note"] = coverage_note(
        research["verified"], target, research["unverified"])
    return research


# --------------------------------------------------------------------------- #
# 5. Bridging accepted ideas into the audit's finding stream.
# --------------------------------------------------------------------------- #
def competitor_findings(research: dict, max_findings: int = DEFAULT_FIX_STREAM_CAP,
                        severity_floor_rank: int = 0,
                        severity_rank=None,
                        file_exists=None,
                        acceptance_total: int = 0) -> list[tuple[str, dict]]:
    """[(rel, finding)] for the accepted, bridgeable, code-fixable ideas.

    Deliberately capped and filtered: a competitor idea is an OPINION about the
    market, so it may inform a bounded number of changes and never a rewrite
    spree.

    ACCOUNTING (2026-08-16). This docstring used to claim "everything it drops
    is still reported" and that was simply false: every filter below was a bare
    `continue`, so an idea the purpose contract had ACCEPTED could fail to reach
    the fix stream for five different reasons and leave no trace anywhere. The
    live SermonSmith run is the evidence - 8 corroborated competitors, 2
    accepted, **0 bridged**, and a report that said "2 accepted" without one
    word about where those two went. That is the portfolio's #1 recurring
    defect (find candidates, act on none, report success) reproduced inside the
    tool built to catch it.

    Every drop is now recorded in `research["bridge_ledger"]` with the reason
    and the competitor name, and the ledger enforces
    `candidates == bridged + sum(dropped) `. `report_lines` renders it and the
    caller prints it, so "accepted but not bridged" can never again be silent.
    The ledger is written onto `research` rather than returned so the existing
    return type (and every caller of it) is unchanged.
    """
    rank = severity_rank or {"low": 1, "medium": 2, "high": 3, "critical": 4}
    out: list[tuple[str, dict]] = []
    dropped: dict[str, list[str]] = {}
    candidates = list(research.get("competitors") or [])

    def _mark(c: dict, status: str, reason: str, *, bridged: bool = False) -> None:
        c["bridge_status"] = status
        c["bridge_reason"] = reason
        c["entered_fix_stream"] = bool(bridged)

    def _drop(reason: str, c: dict) -> None:
        _mark(c, "rejected", reason)
        dropped.setdefault(reason, []).append(str(c.get("name") or "(unnamed)"))

    def _seal() -> None:
        n_dropped = sum(len(v) for v in dropped.values())
        research["bridge_ledger"] = {
            "candidates": len(candidates),
            "bridged": len(out),
            "dropped": {k: sorted(v) for k, v in sorted(dropped.items())},
            "dropped_total": n_dropped,
            # The invariant the owner's "never silently skip work" rule demands.
            # False here means a code path discarded a candidate without saying
            # so - the exact defect this ledger exists to make impossible.
            "accounted": len(candidates) == len(out) + n_dropped,
        }

    if max_findings <= 0:
        # The cap is checked BEFORE the append, not after: a post-append check
        # let max_findings=0 still emit one finding, which is the difference
        # between "--competitor-fixes 0" meaning off and meaning "one anyway".
        for c in candidates:
            _drop("competitor-fixes cap is 0 (bridging disabled)", c)
        _seal()
        return out
    for c in candidates:
        idea = c.get("idea") or {}
        if len(out) >= max_findings:
            # Reaching the cap does not make the remaining ideas disappear; a
            # silent top-N truncation is the same dishonesty as a silent filter.
            _drop(f"over the --competitor-fixes cap of {max_findings}", c)
            continue
        if not idea.get("accept"):
            _drop("idea rejected by the purpose contract", c)
            continue
        if not may_bridge(c):
            _drop(f"not bridgeable (evidence={c.get('evidence_status')}, "
                  f"reuse_mode={c.get('reuse_mode')})", c)
            continue
        if not idea.get("code_fixable"):
            # The free judge tier omits this field entirely, which is how 0 of 8
            # bridged while the report said 2 accepted. Naming it is what makes
            # the model-tier lever visible instead of looking like "no ideas".
            _drop("idea is not code_fixable (or the model omitted the field)", c)
            continue
        rel = str(idea.get("file") or "").replace("\\", "/").strip()
        if not rel:
            _drop("no target file named for the idea", c)
            continue
        ref_txt = str(idea.get("acceptance_ref") or "").strip()
        if acceptance_total > 0:
            try:
                ref_num = int(ref_txt)
            except (TypeError, ValueError):
                ref_num = 0
            if not (1 <= ref_num <= int(acceptance_total)):
                _drop("accepted idea did not map to a valid acceptance criterion", c)
                continue
        if rank.get(str(idea.get("severity", "")).lower(), 0) < severity_floor_rank:
            _drop(f"severity {idea.get('severity') or '(missing)'!r} is below "
                  f"the --fix-severity floor", c)
            continue
        if file_exists is not None and not file_exists(rel):
            _drop(f"named file does not exist in the repo: {rel}", c)
            continue
        problem = (f"{idea.get('what_it_does')}\n\nWhy it matters here: "
                   f"{idea.get('why_valuable')}\n\nPurpose justification: "
                   f"{idea.get('purpose_reason')}\n\nREUSE MODE: {c['reuse_mode']} "
                   f"({c['reuse_reason']}). "
                   + ("You MAY consult the competitor's source."
                      if may_copy_source(c["reuse_mode"])
                      else "You MUST NOT copy or paraphrase the competitor's "
                           "source; implement independently from the described "
                           "behaviour only.")
                   + f"\n\nEvidence: {', '.join(c.get('evidence_urls') or []) or '(none)'}")
        _mark(c, "bridged", "entered the gated fix stream", bridged=True)
        out.append((rel, {
            "line": 0,
            "severity": str(idea.get("severity") or "medium").lower(),
            "category": "competitive-gap",
            "title": f"[competitor: {c['name']}] {idea.get('idea_title')}",
            "problem": problem,
            "fix": (idea.get("why_valuable") or
                    "Implement the accepted capability independently in this file."),
            # Compatibility aliases for report consumers predating the canonical
            # audit shape. The fixer uses problem/fix above.
            "detail": problem,
            "recommendation": idea.get("why_valuable") or "",
            "acceptance_ref": idea.get("acceptance_ref") or "",
            "source": "competitor-research",
        }))
        # No `break` at the cap: the loop must keep walking so the remaining
        # candidates land in the ledger as "over the cap" instead of vanishing.
    _seal()
    return out


# --------------------------------------------------------------------------- #
# 6. Report.
# --------------------------------------------------------------------------- #
def report_lines(research: dict) -> list[str]:
    """Markdown section for the audit report. Says what was NOT found, too."""
    if not research:
        return []
    L = ["## Competitor research", "",
         f"**Coverage:** {research.get('coverage_note', '')}", "",
         f"- **Sources used:** {', '.join(research.get('sources_used') or []) or 'NONE'}",
         f"- **Repo Rewards endpoint:** `{research.get('rr_endpoint', '')}`"]
    skipped = research.get("sources_skipped") or {}
    if skipped:
        L.append("- **Sources SKIPPED (named, not silent):**")
        for name, why in sorted(skipped.items()):
            L.append(f"  - `{name}` - {why}")
    L += ["", f"- **Ideas accepted as serving this program's purpose:** "
              f"{research.get('accepted', 0)} "
              f"(rejected {research.get('rejected', 0)} - the purpose contract, "
              "not the competitor, decides)", ""]
    ledger = research.get("bridge_ledger")
    if ledger:
        L += [f"- **Bridged into the fix stream:** {ledger.get('bridged', 0)} "
              f"of {ledger.get('candidates', 0)} candidate(s)"]
        for reason, names in (ledger.get("dropped") or {}).items():
            L.append(f"  - NOT bridged ({len(names)}): {', '.join(names)} - {reason}")
        if not ledger.get("accounted", False):
            L.append("  - **ACCOUNTING GAP: candidates != bridged + dropped. "
                     "A code path discarded a candidate without recording why. "
                     "This is a defect in FlexFactor itself - report it.**")
        L.append("")
    if not (research.get("competitors") or []):
        L += ["_No competitor could be corroborated from any reachable source. "
              "This is reported as a gap in the research, NOT as evidence that "
              "the program has no competitors._", ""]
        return L
    L += ["| Competitor | Kind | Licence | Reuse mode | Purpose mapping | Verdict | Fix stream | Adoptable idea |",
          "|---|---|---|---|---|---|---|---|"]
    for c in research["competitors"]:
        idea = c.get("idea") or {}
        verdict = ("ACCEPT" if idea.get("accept") else "reject")
        if c.get("evidence_status") != "verified":
            verdict = "UNVERIFIED"
        name = f"[{c['name']}]({c['url']})" if c.get("url") else c["name"]
        mapping = (f"acceptance #{idea.get('acceptance_ref')}"
                   if str(idea.get("acceptance_ref") or "").strip() else "purpose-only")
        if "bridge_status" in c or "entered_fix_stream" in c:
            bridge = ("ENTERED"
                      if c.get("entered_fix_stream") else
                      f"NOT entered - {c.get('bridge_reason', 'not bridged')}")
        else:
            bridge = "not evaluated for fix-stream entry"
        L.append(f"| {name} | {c.get('kind')} | `{c.get('license')}` "
                 f"| `{c.get('reuse_mode')}` | {mapping} | {verdict} "
                 f"| {bridge} | {idea.get('idea_title', '(none)')} |")
    L.append("")
    for c in research["competitors"]:
        idea = c.get("idea") or {}
        L += [f"### {c['name']}", "",
              f"- **Evidence:** " + (", ".join(f"<{u}>" for u in (c.get("evidence_urls") or []))
                                     or "**none - unverified, not acted on**"),
              f"- **Licence:** `{c.get('license')}` (via {c.get('license_source')})",
              f"- **Reuse mode:** `{c.get('reuse_mode')}` - {c.get('reuse_reason')}",
              f"- **Idea:** {idea.get('idea_title', '(none)')} - {idea.get('what_it_does', '')}",
              f"- **Value here:** {idea.get('why_valuable', '')}",
              f"- **Purpose / criterion mapping:** "
              + (f"acceptance #{idea.get('acceptance_ref')}"
                 if str(idea.get('acceptance_ref') or '').strip() else "purpose-only")
              + f" - {idea.get('purpose_reason', '')}",
              f"- **Purpose verdict:** {'ACCEPTED' if idea.get('accept') else 'REJECTED'} - "
              f"{idea.get('purpose_reason', '')}",
              f"- **Fix-stream decision:** "
              + ((f"{'ENTERED the gated fix stream' if c.get('entered_fix_stream') else 'DID NOT enter the fix stream'}"
                  + (f" - {c.get('bridge_reason')}" if c.get('bridge_reason') else ""))
                 if ("bridge_status" in c or "entered_fix_stream" in c)
                 else "not evaluated for fix-stream entry on this report path"),
              f"- **Evidence basis:** {idea.get('evidence_basis', '')} "
              f"(confidence {idea.get('confidence', '?')})", ""]
    return L
