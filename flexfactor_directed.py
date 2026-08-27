"""flexfactor_directed.py — directed orchestration helpers + installer.

Owner order 2026-08-20: concurrent/rotated free backends must not wander.
Also filter non-coding catalog routes and skip generated/vendored failure paths.

This module is the SINGLE OWNER of the unfit-route patterns, the skip-dir
test and the directed work-theme block. flexfactor.py imports them directly
(hard import, part of the canonical runtime). `install()` remains an idempotent
compatibility/runtime hook for launchers and embedders that hold a foreign
namespace.
"""
from __future__ import annotations

import re
import threading

_UNFIT_CODE_PATTERNS = (
    r"prompt-?guard", r"llama-guard", r"nemoguard", r"moderation", r"rerank",
    r"content-?safety", r"topic-control", r"safety-guard",
    r"orpheus", r"\btts\b", r"whisper",
    r"moondream", r"kosmos", r"deplot", r"vila", r"nvclip", r"fuyu",
    r"llava", r"bakllava", r"minicpm-v", r"qwen.*-vl", r"pixtral",
    r"clip-preview", r"stable-diffusion", r"imagen", r"flux", r"lyria",
    r"veo", r"riffusion", r"embed", r"retrieval", r"nomic-embed",
    r"vision-only", r"synthetic-video", r"ai-synthetic-video",
    r"deep-research", r"antigravity", r"robotics-er", r"computer-use",
    r"nano-banana", r"omni-flash", r":batch$",
    r"realtime", r"deep-research",
)

_DEFAULT_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "out", ".venv",
    "__pycache__", ".cache", "coverage", "vendor",
}

_CAPACITY_LOCK = threading.Lock()
_CAPACITY_INSTALLED = False


def _ensure_capacity_runtime():
    """Install shared provider admission lazily, once the runtime is importable."""
    global _CAPACITY_INSTALLED
    if _CAPACITY_INSTALLED:
        try:
            import flexfactor_capacity as capacity
            return capacity
        except ImportError:
            return None
    with _CAPACITY_LOCK:
        if _CAPACITY_INSTALLED:
            import flexfactor_capacity as capacity
            return capacity
        try:
            import flexfactor_capacity as capacity
            capacity.install()
        except ImportError:
            return None
        _CAPACITY_INSTALLED = True
        return capacity


def unfit_for_code_reason(model_or_route_id: str) -> str:
    """Why a catalog route cannot do code review/authoring, or '' when it can.

    The first route-fitness pass is also the earliest safe runtime point to arm
    global provider capacity controls. That keeps flexfactor_rotation standalone
    while making every normal FlexFactor rotation path capacity-aware.
    """
    _ensure_capacity_runtime()
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
    """Install directed helpers and provider admission into flexfactor globals."""
    skip_dirs = module_globals.get("_SKIP_DIRS", _DEFAULT_SKIP_DIRS)

    if module_globals.get("_FLEXFACTOR_DIRECTED_INSTALLED"):
        return
    module_globals["_FLEXFACTOR_DIRECTED_INSTALLED"] = True
    capacity = _ensure_capacity_runtime()
    existing_unfit = module_globals.get("_unfit_for_code_reason")
    live_unfit = existing_unfit if callable(existing_unfit) else None

    def chosen_unfit(model_or_route_id: str) -> str:
        if live_unfit is not None:
            why = live_unfit(model_or_route_id)
            if why:
                return why
        return unfit_for_code_reason(model_or_route_id)

    if live_unfit is None:
        module_globals["_unfit_for_code_reason"] = unfit_for_code_reason
    module_globals.setdefault("_UNFIT_CODE_PATTERNS", _UNFIT_CODE_PATTERNS)
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
            return chosen_unfit(
                getattr(route, "id", "") or getattr(route, "model", "")
            )
        module_globals["_route_unusable_reason"] = _route_unusable_reason
    else:
        module_globals["_route_unusable_reason"] = (
            lambda route, model_mode: chosen_unfit(
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

    if capacity is not None:
        # Capacity-aware admission: the user may request six parallel programs,
        # but free mode only starts as many mutation lanes as healthy qualified
        # allowances can actually sustain. Remaining programs stay queued in
        # run_audit rather than stampeding the same provider account.
        prior_audit = module_globals.get("run_audit")
        if callable(prior_audit) and not getattr(prior_audit, "_capacity_wrapped", False):
            def run_audit(args):
                requested = max(1, int(getattr(args, "parallel", 1) or 1))
                model_mode = str(getattr(args, "model_mode", "free") or "free")
                admitted = capacity.recommended_program_parallelism(requested, model_mode)
                if admitted < requested:
                    print(f"  [capacity] provider admission: requested {requested} concurrent "
                          f"programs; starting {admitted} and queueing {requested - admitted} "
                          "until shared provider capacity is available.")
                    args.parallel = admitted
                return prior_audit(args)
            run_audit._capacity_wrapped = True
            module_globals["run_audit"] = run_audit

        # A partial run is not DONE. Preserve the final state as incomplete so
        # the dashboard cannot display a 100% review bar as production success.
        progress = module_globals.get("_PROGRESS")
        update = getattr(progress, "update", None)
        if callable(update) and not getattr(update, "_capacity_semantics", False):
            def progress_update(index, **kwargs):
                phase = str(kwargs.get("phase") or "")
                if kwargs.get("done") is True and phase.startswith("done - partial"):
                    kwargs["done"] = False
                    kwargs["phase"] = "review complete - repairs/verification pending"
                return update(index, **kwargs)
            progress_update._capacity_semantics = True
            progress.update = progress_update
