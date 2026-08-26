"""Captured vulnerable removal behavior from flexfactor_wip before 658b9d3.

Test fixture only. It intentionally lacks the repository-control-directory
refusal added by that commit so the regression test proves the guard is
load-bearing. Nothing in the runtime imports this module.
"""
import os


def _unquote_porcelain_path(raw: str) -> str:
    """Decode the path form used by the captured implementation."""
    rel = raw.strip()
    if rel.startswith('"') and rel.endswith('"') and len(rel) >= 2:
        body = rel[1:-1]
        try:
            rel = body.encode("latin-1", "backslashreplace").decode(
                "unicode_escape").encode("latin-1").decode("utf-8")
        except Exception:
            rel = body
    return rel[:-1] if rel.endswith("/") else rel


def _remove_captured_untracked(project_dir: str, untracked: list[str]) -> list[str]:
    """Pre-fix implementation retained solely as executable regression evidence."""
    failed: list[str] = []
    dirs: set[str] = set()
    for raw in untracked:
        rel = _unquote_porcelain_path(raw)
        if not rel:
            continue
        full = os.path.join(project_dir, rel.replace("/", os.sep))
        try:
            if os.path.islink(full) or os.path.isfile(full):
                os.unlink(full)
            elif os.path.isdir(full):
                for root, child_dirs, files in os.walk(full, topdown=False):
                    for filename in files:
                        os.unlink(os.path.join(root, filename))
                    for child_dir in child_dirs:
                        os.rmdir(os.path.join(root, child_dir))
                os.rmdir(full)
        except OSError:
            failed.append(rel)
            continue
        parent = os.path.dirname(full)
        while parent and os.path.abspath(parent) != os.path.abspath(project_dir):
            dirs.add(parent)
            parent = os.path.dirname(parent)
    for directory in sorted(dirs, key=len, reverse=True):
        try:
            if os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
        except OSError:
            pass
    return failed
