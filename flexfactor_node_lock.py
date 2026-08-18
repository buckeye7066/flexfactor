"""Workspace lockfile inheritance for Node monorepos.

npm/pnpm/yarn workspaces commit ONE lockfile at the workspace root.
Scoring each nested package.json as unpinned (SermonSmith apps/web,
GeneMap apps/web) is a false production-ready blocker: install at the
root already pins every workspace member. Never walk above project_dir.
"""
from __future__ import annotations

import os

_NODE_LOCK_CANDIDATES = (
    ("pnpm-lock.yaml", "pnpm", ["pnpm", "install"]),
    ("yarn.lock", "yarn", ["yarn", "install"]),
    ("bun.lockb", "bun", ["bun", "install"]),
    ("bun.lock", "bun", ["bun", "install"]),
    ("package-lock.json", "npm", ["npm", "install"]),
    ("npm-shrinkwrap.json", "npm", ["npm", "install"]),
)


def project_root_from_rel(package_dir: str, rel: str) -> str:
    """Absolute project root given a package directory and its repo-relative path."""
    package_dir = os.path.abspath(package_dir)
    rel_n = (rel or ".").replace("\\", "/").strip("/")
    if rel_n in ("", "."):
        return package_dir
    depth = rel_n.count("/") + 1
    return os.path.abspath(os.path.join(package_dir, *([os.pardir] * depth)))


def node_lock_at_or_above(package_dir: str, project_dir: str):
    """Find the Node lockfile that pins this package.

    Returns (manager, lockfile_name, install_argv, inherited, lock_dir).
    """
    package_dir = os.path.abspath(package_dir)
    project_dir = os.path.abspath(project_dir)
    here = package_dir
    while True:
        for fname, manager, install in _NODE_LOCK_CANDIDATES:
            if os.path.isfile(os.path.join(here, fname)):
                inherited = os.path.abspath(here) != package_dir
                return manager, fname, list(install), inherited, here
        if os.path.abspath(here) == project_dir:
            break
        parent = os.path.dirname(here)
        if parent == here:
            break
        try:
            if os.path.commonpath([parent, project_dir]) != project_dir:
                break
        except ValueError:
            break
        here = parent
    return "npm", None, ["npm", "install"], False, package_dir


def install_workspace_lock_inheritance(eng) -> None:
    """Patch a loaded prodready engine so nested workspace packages inherit
    the ancestor lockfile and do not run their own install."""
    orig_detect_node = eng._detect_node
    orig_current = eng._current_lockfile

    def _detect_node(root: str, rel: str):
        tc = orig_detect_node(root, rel)
        if tc is None:
            return None
        project_dir = project_root_from_rel(root, rel)
        manager, lockfile, install, inherited, lock_dir = node_lock_at_or_above(
            root, project_dir)
        tc.manager = manager
        tc.lockfile = lockfile
        tc.install = [] if inherited else [install]
        tc.deps_installed = (
            os.path.isdir(os.path.join(root, "node_modules"))
            or os.path.isdir(os.path.join(lock_dir, "node_modules")))
        return tc

    def _current_lockfile(project_dir: str, tc):
        found = orig_current(project_dir, tc)
        if found:
            return found
        if getattr(tc, "ecosystem", None) == "node":
            root = os.path.join(project_dir, tc.root)
            _mgr, fname, _install, _inh, _lock_dir = node_lock_at_or_above(
                root, project_dir)
            if fname:
                return fname
        return None

    eng._detect_node = _detect_node
    eng._current_lockfile = _current_lockfile
    detectors = list(eng._DETECTORS)
    for i, fn in enumerate(detectors):
        if fn is orig_detect_node or getattr(fn, "__name__", "") == "_detect_node":
            detectors[i] = _detect_node
            break
    else:
        detectors[0] = _detect_node
    eng._DETECTORS = tuple(detectors)
