"""flexfactor_directed.py — directed orchestration helpers + installer.

Owner order 2026-08-20: concurrent/rotated free backends must not wander.
Also filter non-coding catalog routes and skip generated/vendored failure paths.

Install into flexfactor.py with:

    try:
        import flexfactor_directed as _ff_directed
        _ff_directed.install(globals())
    except Exception:
        pass

Placed after imports so _route_unusable_reason / _existing_failure_path that
already exist get wrapped; missing symbols are defined fresh.
"""
from __future__ import annotations

import re

_UNFIT_CODE_PATTERNS = (
    r"prompt-?guard", r"llama-guard", r"nemoguard", r"moderation", r"rerank",
    r"content-?safety", r"topic-control", r"safety-guard",
    r"orpheus", r"\btts\b", r"whisper",
    r"moondream", r"kosmos", r"deplot", r"vila", r"nvclip", r"fuyu",
    r"clip-preview", r"stable-diffusion", r"imagen", r"flux", r"lyria",
    r"veo", r"riffusion", r"embed", r"retrieval", r"nomic-embed",
    r"vision-only", r"synthetic-video", r"ai-synthetic-video",
)

_DEFAULT_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "out", ".venv",
    "__pycache__", ".cache", "coverage", "vendor",
}


def unfit_for_code_reason(model_or_route_id: str) -> str:
    """Why a catalog route cannot do code review/authoring, or '' when it can."""
    low = str(model_or_route_id or "").lower()
    for pat in _UNFIT_CODE_PATTERNS:
        if re.search(pat, low):
            return f"non-coding model ({pat})"
    return ""


def is_skip_dir_path(rel: str, skip_dirs=None) -> bool:
    """True when a repo-relative path sits under a generated/vendored skip dir."""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p and p != "."]
    dirs = skip_dirs if skip_dirs is not None else _DEFAULT_SKIP_DIRS
    skip = {d.lower() for d in dirs}
    return any(p.lower() in skip for p in parts)


def directed_work_theme_block(theme: str, issue: str) -> str:
    """Stamp one shared theme+issue onto every model call for this program."""
    theme_s = " ".join(str(theme or "fulfill the program purpose").split())[:400]
    issue_s = " ".join(str(issue or "resolve the current verified failure").split())[:500]
    return (
        "DIRECTED WORK THEME (shared across every model on this run):\n"
        f"Theme: {theme_s}\n"
        f"Open issue (attack this — do not wander): {issue_s}\n"
        "Never treat node_modules/, dist/, build/, .next/, out/, or coverage/ "
        "as the fix target — edit the source that produced them.\n"
        "Every answer must advance THAT issue. Ignore unrelated polish.\n"
    )


def install(module_globals: dict) -> None:
    """Install directed helpers into a flexfactor module globals dict."""
    skip_dirs = module_globals.get("_SKIP_DIRS", _DEFAULT_SKIP_DIRS)

    module_globals["_UNFIT_CODE_PATTERNS"] = _UNFIT_CODE_PATTERNS
    module_globals["_unfit_for_code_reason"] = unfit_for_code_reason
    module_globals["_directed_work_theme_block"] = directed_work_theme_block

    def _is_skip(rel: str) -> bool:
        return is_skip_dir_path(rel, skip_dirs)

    module_globals["_is_skip_dir_path"] = _is_skip

    # Wrap existing _route_unusable_reason to also reject unfit non-coding routes.
    prior_route = module_globals.get("_route_unusable_reason")
    if callable(prior_route):
        def _route_unusable_reason(route, model_mode: str) -> str:
            why = prior_route(route, model_mode)
            if why:
                return why
            unfit = unfit_for_code_reason(
                getattr(route, "id", "") or getattr(route, "model", "")
            )
            return unfit

        module_globals["_route_unusable_reason"] = _route_unusable_reason
    else:
        module_globals["_route_unusable_reason"] = (
            lambda route, model_mode: unfit_for_code_reason(
                getattr(route, "id", "") or getattr(route, "model", "")
            )
        )

    # Wrap _existing_failure_path to drop skip-dir targets.
    prior_fail = module_globals.get("_existing_failure_path")
    if callable(prior_fail):
        def _existing_failure_path(project_dir: str, raw_path: str):
            hit = prior_fail(project_dir, raw_path)
            if hit is None:
                return None
            if _is_skip(hit):
                return None
            return hit

        module_globals["_existing_failure_path"] = _existing_failure_path
