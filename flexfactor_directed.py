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

import functools
import os
import re
import shutil
import tempfile
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


_POWERSHELL_EXTS = frozenset({".ps1", ".psm1", ".psd1"})


def _bounded_damerau_levenshtein(left: str, right: str, limit: int) -> int | None:
    """Return edit distance up to ``limit`` (adjacent transposes cost one).

    This is deliberately bounded: project-name recovery only needs to recognize
    one or two obvious typing errors. Anything farther away is not safe to guess.
    """
    a, b = str(left or ""), str(right or "")
    if abs(len(a) - len(b)) > limit:
        return None
    if a == b:
        return 0
    if not a or not b:
        distance = max(len(a), len(b))
        return distance if distance <= limit else None
    previous_previous: list[int] | None = None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + cost,
            )
            if (previous_previous is not None and i > 1 and j > 1
                    and ca == b[j - 2] and a[i - 2] == cb):
                value = min(value, previous_previous[j - 2] + 1)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return None
        previous_previous, previous = previous, current
    distance = previous[-1]
    return distance if distance <= limit else None


def typo_resolve_local_project(name_hints, roots, slugify) -> str | None:
    """Resolve only a unique, near-exact project-name typo.

    Exact/prefix lookup remains the caller's first choice. This recovery stage
    allows at most one edit for medium names and two for long names, prefers
    visible checkouts over hidden config folders, and refuses ties.
    """
    hints: list[str] = []
    for raw in name_hints or ():
        slug = slugify(str(raw or ""))
        compact = slug.replace("-", "")
        if len(compact) >= 5 and compact not in hints:
            hints.append(compact)
    if not hints:
        return None

    directories: list[str] = []
    seen: set[str] = set()
    for root in roots or ():
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            key = os.path.normcase(os.path.abspath(full))
            if key in seen:
                continue
            seen.add(key)
            directories.append(full)

    visible = [p for p in directories if not os.path.basename(p).startswith(".")]
    hidden = [p for p in directories if os.path.basename(p).startswith(".")]
    for tier in (visible, hidden):
        scored: list[tuple[int, str]] = []
        for path in tier:
            candidate = slugify(os.path.basename(path)).replace("-", "")
            if len(candidate) < 5:
                continue
            best: int | None = None
            for hint in hints:
                limit = 2 if max(len(hint), len(candidate)) >= 10 else 1
                distance = _bounded_damerau_levenshtein(hint, candidate, limit)
                if distance is not None and distance > 0:
                    best = distance if best is None else min(best, distance)
            if best is not None:
                scored.append((best, path))
        if not scored:
            continue
        minimum = min(score for score, _path in scored)
        winners = [path for score, path in scored if score == minimum]
        return winners[0] if len(winners) == 1 else None
    return None


def _powershell_parser_executable() -> str | None:
    """Prefer Windows PowerShell 5.1 where present, then PowerShell Core."""
    names = (("powershell.exe", "powershell", "pwsh.exe", "pwsh")
             if os.name == "nt" else ("pwsh", "powershell"))
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def powershell_syntax_details(project_dir: str, path: str, source: str, run):
    """Parse PowerShell source without executing it.

    The parser driver is owner-authored and is invoked through ``-File`` rather
    than ``-Command`` so the subprocess policy cannot be bypassed by inline shell
    text. The candidate is only passed to PowerShell's AST parser.
    """
    executable = _powershell_parser_executable()
    if not executable:
        return None
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext not in _POWERSHELL_EXTS:
        return None
    try:
        encoded = str(source).encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        return False, f"source is not UTF-8 encodable: {exc}", None
    driver_source = r'''param([Parameter(Mandatory=$true)][string]$Candidate)
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($Candidate, [ref]$tokens, [ref]$errors) | Out-Null
if ($null -ne $errors -and $errors.Count -gt 0) {
    foreach ($parseError in $errors) {
        [Console]::Error.WriteLine($parseError.Message)
    }
    exit 1
}
exit 0
'''
    try:
        with tempfile.TemporaryDirectory(prefix="flexfactor-ps-parse-") as temp_dir:
            candidate = os.path.join(temp_dir, "candidate" + ext)
            driver = os.path.join(temp_dir, "parse-only.ps1")
            with open(candidate, "wb") as fh:
                fh.write(encoded)
            with open(driver, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(driver_source)
            result = run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass",
                 "-File", driver, candidate],
                project_dir, timeout=60,
            )
    except OSError as exc:
        return None, f"PowerShell syntax preflight could not start: {exc}", None
    if getattr(result, "flexfactor_launch_error", False):
        return None
    ok = getattr(result, "returncode", 1) == 0
    output = str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    return (ok, output or "PowerShell AST parse", source if ok else None)


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

    # Source-type parity: PowerShell launchers are first-class code, not opaque
    # text. Tree-sitter provides the cross-platform fallback; Windows also gets
    # the native 5.1 parser through the pre-write hook below.
    code_exts = module_globals.get("_CODE_EXTS")
    tree_languages = module_globals.get("_TREE_SITTER_LANGUAGE_BY_EXT")
    if isinstance(code_exts, set) and isinstance(tree_languages, dict):
        for extension in _POWERSHELL_EXTS:
            code_exts.add(extension)
            tree_languages[extension] = "powershell"

    # tree-sitter-language-pack's PowerShell grammar emits an ERROR node for an
    # empty file even though an empty .ps1/.psm1/.psd1 is valid PowerShell. Keep
    # the bundled parser for real source, but normalize that grammar edge case so
    # the syntax contract matches the language rather than the parser quirk.
    prior_tree_sitter = module_globals.get("_tree_sitter_source_syntax_ok")
    if (callable(prior_tree_sitter)
            and not getattr(prior_tree_sitter, "_powershell_empty_hardened", False)):
        @functools.wraps(prior_tree_sitter)
        def _tree_sitter_source_syntax_ok(extension, source):
            ext = str(extension or "").lower()
            if ext in _POWERSHELL_EXTS:
                try:
                    empty = not source or not source.strip()
                except (AttributeError, TypeError):
                    empty = False
                if empty:
                    return True, "Tree-sitter powershell: empty source is syntactically valid"
            return prior_tree_sitter(extension, source)
        _tree_sitter_source_syntax_ok._powershell_empty_hardened = True
        module_globals["_tree_sitter_source_syntax_ok"] = _tree_sitter_source_syntax_ok

    # Preserve precise local checkout matching, then recover only a unique
    # one/two-edit typo. Ambiguous names still fail closed.
    prior_find_project = module_globals.get("_find_local_project")
    if callable(prior_find_project) and not getattr(prior_find_project, "_typo_hardened", False):
        @functools.wraps(prior_find_project)
        def _find_local_project(*name_hints):
            exact_or_prefix = prior_find_project(*name_hints)
            if exact_or_prefix:
                return exact_or_prefix
            return typo_resolve_local_project(
                name_hints, module_globals.get("_PROJECT_ROOTS", ()),
                module_globals.get("_slugify", lambda value: str(value).lower()),
            )
        _find_local_project._typo_hardened = True
        module_globals["_find_local_project"] = _find_local_project

    # PowerShell model edits get the native AST parser when available before
    # the generic Tree-sitter gate. `$code:`-style Windows PowerShell 5.1 parser
    # failures are therefore rejected before owner files are touched.
    prior_prewrite = module_globals.get("_prewrite_source_syntax_details")
    run = module_globals.get("_run")
    if (callable(prior_prewrite) and callable(run)
            and not getattr(prior_prewrite, "_powershell_hardened", False)):
        @functools.wraps(prior_prewrite)
        def _prewrite_source_syntax_details(project_dir, path, source, stack, *, allow_empty=False):
            ext = os.path.splitext(str(path or ""))[1].lower()
            if ext in _POWERSHELL_EXTS:
                # Preserve the shared empty-source refusal that the generic prewrite
                # gate enforces for every language when allow_empty is False.
                text = str(source or "")
                if not allow_empty and not text.strip():
                    return False, "empty whole-file response", None
                native = powershell_syntax_details(project_dir, path, source, run)
                if native is not None:
                    return native
            return prior_prewrite(
                project_dir, path, source, stack, allow_empty=allow_empty,
            )
        _prewrite_source_syntax_details._powershell_hardened = True
        module_globals["_prewrite_source_syntax_details"] = _prewrite_source_syntax_details

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
            @functools.wraps(prior_audit)
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
        #
        # THE LABEL HAS TO SAY THE PROGRAM STOPPED (2026-08-30). `done=False` is
        # right and stays - a partial run must never be counted as success - but
        # the old wording, "review complete - repairs/verification pending",
        # reads as WORK IN PROGRESS, and with done=False the dashboard renders it
        # exactly like a program that is still grinding. Measured live: three of
        # five programs (SermonSmith, IPlay, reporewards) had finished partial
        # hours earlier - final readiness and audit report written at 22:59,
        # 22:46 and 21:28, checkpoints untouched since - and the owner was
        # watching the panel believing all five were still running, waiting on
        # runs that would never move again.
        #
        # Trading a false "success" for a false "in progress" is not a fix; it
        # is the same lie pointed the other way, and this one costs the owner
        # their night. The phase now says STOPPED, so the panel distinguishes
        # "ended without finishing" from "still working" - which is the whole
        # question a progress display exists to answer.
        progress = module_globals.get("_PROGRESS")
        update = getattr(progress, "update", None)
        if callable(update) and not getattr(update, "_capacity_semantics", False):
            def progress_update(index, **kwargs):
                phase = str(kwargs.get("phase") or "")
                if kwargs.get("done") is True and phase.startswith("done - partial"):
                    kwargs["done"] = False
                    kwargs["phase"] = ("STOPPED (incomplete) - repairs/"
                                       "verification pending")
                    # AND A TERMINAL SIGNAL LIVENESS CONSUMERS CAN READ.
                    # Relabelling the phase was not enough: the phone dashboard
                    # classifies liveness from `done` plus the FILE's freshness,
                    # and status.json stays fresh while ANY sibling program is
                    # still working - so a stopped program kept a green LIVE
                    # pill directly beside the STOPPED phase, for hours, with
                    # the two contradicting each other. `done` must stay False
                    # (this run was NOT a success), so the terminal fact needs
                    # its own field rather than being inferred from `done`.
                    kwargs["stopped"] = True
                return update(index, **kwargs)
            progress_update._capacity_semantics = True
            progress.update = progress_update
