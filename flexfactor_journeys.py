"""FlexFactor journey engine helpers.

Thin, stdlib-only glue between ``flexfactor.py`` and the standalone Playwright
explorer ``flexfactor_assets/flexfactor_explorer.js`` (package data).

    script = explorer_script_path()
    env = {**os.environ, **journey_env(roles, isolated, viewports, max_pages)}
    proc = subprocess.run(["node", script, base_url, artifacts], env=env, ...)
    result = parse_result(proc.stdout + proc.stderr)
    ok, reasons = completeness(result)
"""
from __future__ import annotations

import json
import os
from collections import Counter

RESULT_MARKER = "FLEXFACTOR_E2E_RESULT="
EXPLORER_BASENAME = "flexfactor_explorer.js"
DEFAULT_VIEWPORTS = ("1280x800", "390x844")
DEFAULT_MAX_PAGES = 500


def explorer_script_path() -> str:
    """Absolute path to ``flexfactor_explorer.js``.

    Resolves next to this module first (source checkout); then via
    ``importlib.resources`` so an installed wheel that ships the .js as package
    data also works. Raises ``FileNotFoundError`` naming every location tried.
    """
    tried = []
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "flexfactor_assets", EXPLORER_BASENAME)
    tried.append(here)
    if os.path.isfile(here):
        return here
    try:  # installed wheel: shipped as package data of flexfactor_assets
        from importlib import resources
        candidate = resources.files("flexfactor_assets").joinpath(EXPLORER_BASENAME)
        tried.append(str(candidate))
        if candidate.is_file():
            return str(candidate)
    except Exception:  # pragma: no cover - resources API differences
        pass
    raise FileNotFoundError(f"{EXPLORER_BASENAME} not found; tried: {tried}")


def journey_env(roles: list | None, isolated: bool, viewports: list[str] | None,
                max_pages: int | None) -> dict:
    """Build the FLEXFACTOR_E2E_* environment for the explorer.

    ``roles`` is a list of ``{name, cookies?, localStorage?, login?}`` dicts;
    ``anonymous`` is always implicit in the explorer, so it is not required here.
    Returns ONLY the explorer variables (merge over ``os.environ`` yourself).
    """
    env: dict[str, str] = {}
    env["FLEXFACTOR_E2E_ISOLATED"] = "1" if isolated else "0"
    env["FLEXFACTOR_E2E_MAX_PAGES"] = str(int(max_pages) if max_pages else DEFAULT_MAX_PAGES)
    vps = [str(v).strip() for v in (viewports or DEFAULT_VIEWPORTS) if str(v).strip()]
    for v in vps:
        w, _, h = v.partition("x")
        if not (w.isdigit() and h.isdigit()):
            raise ValueError(f"viewport must look like WIDTHxHEIGHT, got {v!r}")
    env["FLEXFACTOR_E2E_VIEWPORTS"] = ",".join(vps)
    if roles:
        for r in roles:
            if not isinstance(r, dict) or not str(r.get("name", "")).strip():
                raise ValueError(f"every role needs a non-empty name: {r!r}")
        env["FLEXFACTOR_E2E_ROLES"] = json.dumps(list(roles), separators=(",", ":"))
    return env


def parse_result(stdout: str) -> dict | None:
    """Extract the ``FLEXFACTOR_E2E_RESULT=`` JSON line from explorer output.

    Tolerates other output before/after and returns the LAST parseable marker
    line; ``None`` when no marker line parses.
    """
    found = None
    for line in (stdout or "").splitlines():
        if RESULT_MARKER not in line:
            continue
        payload = line.split(RESULT_MARKER, 1)[1].strip()
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found


def journey_matrix_summary(result: dict) -> dict:
    """Counts of journeys by kind / role / status plus the named gaps."""
    result = result or {}
    journeys = list(result.get("journeys") or [])
    by_kind: dict[str, Counter] = {}
    by_role: dict[str, Counter] = {}
    by_status: Counter = Counter()
    for j in journeys:
        status = str(j.get("status") or "unknown")
        by_status[status] += 1
        by_kind.setdefault(str(j.get("kind") or "unknown"), Counter())[status] += 1
        by_role.setdefault(str(j.get("role") or "unknown"), Counter())[status] += 1
    matrix = list(result.get("authorization_matrix") or [])
    authz = Counter(f"{m.get('role')}:{m.get('outcome')}" for m in matrix)
    return {
        "total": len(journeys),
        "by_status": dict(by_status),
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
        "by_role": {k: dict(v) for k, v in by_role.items()},
        "authorization": dict(authz),
        "findings": Counter(str(f.get("kind")) for f in (result.get("findings") or [])),
        "incomplete_reasons": list(result.get("incomplete_reasons") or []),
        "named_skips": list(result.get("skipped") or []),
        "errors": len(result.get("errors") or []),
        "complete": bool(result.get("complete") is True),
    }


def completeness(result: dict | None) -> tuple[bool, list[str]]:
    """``(True, [])`` only when every discovered route/control/form/journey was
    exercised and nothing was skipped, capped, timed out or errored.
    Slow pages and accessibility violations are findings (evidence), NOT gaps.
    Otherwise ``(False, reasons)`` with every gap named (never silent)."""
    reasons: list[str] = []
    if not result:
        return False, ["no FLEXFACTOR_E2E_RESULT payload from explorer"]
    reasons.extend(str(r) for r in (result.get("incomplete_reasons") or []))
    reasons.extend(f"timeout: {t}" for t in (result.get("timeouts") or []))
    reasons.extend(f"skipped: {s}" for s in (result.get("skipped") or []))
    for j in result.get("journeys") or []:
        if j.get("status") == "failed":
            reasons.append(f"failed journey {j.get('id')} {j.get('kind')} {j.get('target')}: {j.get('reason') or 'no reason recorded'}")
        elif j.get("status") == "skipped" and not any(str(j.get("reason") or "") == s for s in (result.get("skipped") or [])):
            reasons.append(f"skipped journey {j.get('id')} {j.get('kind')} {j.get('target')}: {j.get('reason') or 'no reason recorded'}")
    for f in result.get("formEvidence") or []:
        if f.get("status") in ("constraints-executed", "blocked-destructive") and not f.get("reason"):
            reasons.append(f"form {f.get('action') or f.get('url')} was not submitted")
    errs = list(result.get("errors") or [])
    if errs:
        reasons.append(f"{len(errs)} explorer error(s): {errs[0]}")
    if int(result.get("pages") or 0) <= 0:
        reasons.append("no pages visited")
    if result.get("complete") is not True and not reasons:
        reasons.append("explorer reported complete=false without a named reason")
    return (not reasons) and result.get("complete") is True, reasons
