"""Secret/PII egress gate for FlexFactor's provider chokepoint.

Every string of REPO-DERIVED text FlexFactor sends to a cloud model goes
through the `AnthropicProvider`/`OpenAIProvider` methods `complete` / `grade`
/ `structured` (the `prompt`/`instruction` argument — `system` prompts are
FlexFactor-authored constants and carry no repo text). Those six call sites
gate their payload through `gate_text` before any network egress. On a
finding the call is REFUSED by default (fail closed) and surfaces as
`flexfactor_egress_blocked`; the owner can instead mask-and-send (`--redact`)
or send anyway (`--allow-sensitive`), or allow specific categories via:

  * env FLEXFACTOR_ALLOW_EGRESS="cloud_token,pii"   (comma-separated, or "all")
  * ~/.flexfactor/policy.json  {"allow_egress": ["cloud_token", ...]}

Design decisions (deliberate, documented):
  * The BLOCK tier is HIGH-CONFIDENCE patterns only: PEM private-key blocks,
    tokens with distinctive vendor prefixes (AKIA/ghp_/sk-ant-/xoxb-/AIza/
    glpat-/sk_live_/npm_...), JWTs, secret-ish env-file lines, quoted
    password/secret assignments, and SSN-shaped PII. Audits legitimately send
    code full of `token = "..."` fixtures, so generic assignments only trip
    when the value looks credential-like (>=8 chars, letters AND digits, no
    spaces) and is not an obvious placeholder. High-entropy-only strings
    (lockfile hashes, base64 test data) are deliberately NOT blocked —
    they would make audits of real repos unusable.
  * False positives fail in the SAFE direction: the call is refused with a
    clear remedy (--redact / --allow-sensitive / policy), never sent.
  * Findings never echo the secret: previews are masked to 4 chars.
  * Redaction replaces each matched span with [EGRESS-REDACTED:<category>]
    so the model still sees where something was and what kind it was.

This module is deliberately standalone (stdlib-only, no import of flexfactor)
so it is unit-testable in isolation, mirroring flexfactor_cmdpolicy.
"""
from __future__ import annotations

import json
import os
import re

# The full category vocabulary (for reports/telemetry/policy).
ALL_CATEGORIES = frozenset({
    "private_key", "cloud_token", "api_token", "password_assignment",
    "env_secret", "pii",
})

# Values that mean "this is documentation, not a live secret". Checked
# case-insensitively as substrings of the matched token/value.
_PLACEHOLDER_HINTS = (
    "xxxx", "example", "sample", "placeholder", "your-", "your_", "yourkey",
    "changeme", "change-me", "change_me", "dummy", "fake", "redacted",
    "insert", "<key>", "<token>", "<secret>", "todo", "1234567890",
    "abcdef123", "deadbeef",
)
# Values that are clearly code/config REFERENCES to a secret, not the secret.
_CODE_VALUE_HINTS = (
    "os.environ", "process.env", "getenv", "${", "{{", "%s", "%(", "f\"", "f'",
)


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    if any(h in low for h in _PLACEHOLDER_HINTS):
        return True
    if any(h in low for h in _CODE_VALUE_HINTS):
        return True
    # A single repeated character (aaaa..., ****...) is a mask, not a secret.
    stripped = set(low) - {"-", "_", "*", "."}
    return len(stripped) <= 1


def _value_looks_secret(value: str) -> bool:
    """Heuristic for GENERIC assignments only (the vendor-prefix patterns do
    not consult this): credential-like means >=8 chars, no whitespace, and a
    mix of letters and digits. Keeps `token = "flexfactor_policy_blocked"`
    style sentinels and prose values from blocking an audit."""
    if len(value) < 8 or any(c.isspace() for c in value):
        return False
    has_alpha = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return has_alpha and has_digit and not _is_placeholder(value)


# (category, compiled pattern, value_group_or_None)
# value_group: if set, the placeholder/secret-likeness filters run on that
# group; if None, the whole match is checked against the placeholder filter
# only (vendor prefixes are already high-confidence).
_PATTERNS: list[tuple[str, re.Pattern, int | None]] = [
    ("private_key",
     re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), None),
    # Vendor-distinctive token shapes. \b keeps `task-...`/`risk-...` safe.
    ("cloud_token", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("cloud_token", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), None),
    ("cloud_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), None),
    ("cloud_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), None),
    ("cloud_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), None),
    ("cloud_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), None),
    ("cloud_token", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b"), None),
    ("cloud_token", re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"), None),
    ("api_token", re.compile(r"\bsk-ant-[A-Za-z0-9\-]{20,}\b"), None),
    ("api_token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b"), None),
    # JWT: three dot-joined base64url segments starting with the {"..."} header.
    ("api_token",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
     None),
    # Quoted assignment to a secret-ish name; group(1) = the value. No leading
    # \b: `_` is a word char, so \b would miss `db_password = "..."` entirely.
    ("password_assignment",
     re.compile(r"(?i)(?:password|passwd|pwd|secret|api_?key|access_?key|"
                r"auth_?token|token)\s*[:=]\s*[\"']([^\"']{8,})[\"']"), 1),
    # .env-style line; group(1) = the value.
    ("env_secret",
     re.compile(r"(?m)^\s*[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)"
                r"\s*=\s*[\"']?([^\s\"']{8,})[\"']?\s*$"), 1),
    # SSN shape (3-2-4 with dashes; phone numbers are 3-3-4 and don't match).
    ("pii", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
]


def scan_text(text: str) -> list[dict]:
    """Scan text for egress findings. Pure + deterministic.

    Returns [{"category", "start", "end", "line", "preview"}, ...] sorted by
    position. Previews are MASKED (4 chars + "***") — a finding must never
    re-leak the secret into a report or log."""
    findings: list[dict] = []
    if not text:
        return findings
    for category, pattern, value_group in _PATTERNS:
        for m in pattern.finditer(text):
            token = m.group(value_group) if value_group else m.group(0)
            if value_group is not None:
                if not _value_looks_secret(token):
                    continue
            elif _is_placeholder(token):
                continue
            findings.append({
                "category": category,
                "start": m.start(),
                "end": m.end(),
                "line": text.count("\n", 0, m.start()) + 1,
                "preview": m.group(0)[:4] + "***",
            })
    findings.sort(key=lambda f: (f["start"], f["end"]))
    return findings


def redact_text(text: str, findings: list[dict] | None = None) -> tuple[str, list[dict]]:
    """Replace every finding's span with [EGRESS-REDACTED:<category>].

    Spans are replaced back-to-front so earlier offsets stay valid; overlapping
    spans are merged into the earliest-starting replacement."""
    if findings is None:
        findings = scan_text(text)
    out = text
    last_start = None
    for f in sorted(findings, key=lambda f: (f["start"], f["end"]), reverse=True):
        end = f["end"]
        if last_start is not None and end > last_start:
            end = last_start  # clip overlap with the finding to our right
        if end <= f["start"]:
            continue
        out = out[:f["start"]] + f"[EGRESS-REDACTED:{f['category']}]" + out[end:]
        last_start = f["start"]
    return out, findings


def _load_policy_allow() -> set[str]:
    """Categories the owner has explicitly allowed to egress. Env wins; the
    policy file supplements. "all" allows every category."""
    allow: set[str] = set()
    env = os.environ.get("FLEXFACTOR_ALLOW_EGRESS", "")
    allow |= {t.strip().lower() for t in env.split(",") if t.strip()}
    path = os.path.join(os.path.expanduser("~"), ".flexfactor", "policy.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("allow_egress")
        if raw is True:
            allow.add("all")
        for t in (raw if isinstance(raw, list) else []):
            allow.add(str(t).strip().lower())
    except (OSError, ValueError):
        pass  # no/unreadable policy file -> nothing extra allowed (fail closed)
    if "all" in allow:
        return set(ALL_CATEGORIES)
    return allow & ALL_CATEGORIES


def gate_text(text: str, mode: str = "block",
              allow: set[str] | None = None) -> tuple[str, str, list[dict]]:
    """Gate a payload before cloud egress: (action, text_to_send, findings).

    action: "clean"    - no findings, text unchanged.
            "allowed"  - findings exist but mode/policy permits sending as-is.
            "redacted" - findings masked; the REDACTED text is returned.
            "blocked"  - refused; text_to_send is "" and MUST NOT be sent.
    An unknown mode blocks (fail closed), never passes through."""
    findings = scan_text(text)
    if not findings:
        return "clean", text, findings
    if mode == "allow":
        return "allowed", text, findings
    effective_allow = _load_policy_allow() if allow is None else allow
    if {f["category"] for f in findings} <= effective_allow:
        return "allowed", text, findings
    if mode == "redact":
        redacted, _ = redact_text(text, findings)
        return "redacted", redacted, findings
    return "blocked", "", findings
