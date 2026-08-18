"""Workspace lockfile inheritance — SermonSmith/GeneMap class.

Run:  python flexfactor_node_lock_tests.py

A nested package.json with no lockfile of its own is still pinned when
the workspace root has package-lock.json / pnpm-lock.yaml. Demanding a
per-package lockfile is a false production-ready fail.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

import flexfactor_prodready as pr


class _RepoFixture:
    def __init__(self, files: dict):
        self.files = files
        self._tmp = None

    def __enter__(self) -> str:
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        for rel, body in self.files.items():
            path = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path) or root, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return root

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


def _fake_run(results=None):
    results = results or {}

    def run(cmd, cwd, timeout=None, **kw):
        rc = results.get(cmd[0], 0)
        return subprocess.CompletedProcess(cmd, rc, "", "")
    return run


class WorkspaceLockInheritanceTests(unittest.TestCase):
    def test_workspace_packages_inherit_the_root_lockfile(self):
        cases = (
            ("package-lock.json", "npm"),
            ("pnpm-lock.yaml", "pnpm"),
        )
        for lock, expected in cases:
            with _RepoFixture({
                    "package.json": '{"private":true,"workspaces":["apps/*"]}',
                    lock: "",
                    "apps/web/package.json": '{"name":"web","scripts":{"test":"vitest"}}',
            }) as root:
                chains = pr.detect_toolchains(root)
                by_root = {t.root: t for t in chains if t.ecosystem == "node"}
                self.assertIn(".", by_root)
                self.assertIn("apps/web", by_root)
                web = by_root["apps/web"]
                self.assertEqual(web.manager, expected, f"for {lock}")
                self.assertEqual(web.lockfile, lock)
                self.assertEqual(
                    web.install, [],
                    "workspace members must not run their own install")
                gates = {g.id: g for g in
                         pr.assess_readiness(root, chains, _fake_run({"git": 1}))}
                self.assertEqual(gates["deps_pinned"].status, "pass",
                                 gates["deps_pinned"].evidence)

    def test_nested_package_without_any_lockfile_is_still_unpinned(self):
        with _RepoFixture({
                "package.json": '{"private":true}',
                "apps/web/package.json": '{"name":"web"}',
        }) as root:
            chains = pr.detect_toolchains(root)
            gates = {g.id: g for g in
                     pr.assess_readiness(root, chains, _fake_run({"git": 1}))}
            self.assertEqual(gates["deps_pinned"].status, "fail")
            self.assertIn("no lockfile", gates["deps_pinned"].evidence)


if __name__ == "__main__":
    unittest.main()
