#!/usr/bin/env python3
"""Tests for repo-relative source lookup (refactor mode, option 1).

The owner typed `backend/crawler-os/contract.js` and got "File not found" for a
file that exists. These pin the three resolution tiers, and - because refactor
mode WRITES IN PLACE - that an ambiguous match is never resolved silently.
"""
from __future__ import annotations

import os
import tempfile
import unittest

import flexfactor_locate as loc


def _mkproj(root, name, rel, body="x\n", git=True):
    proj = os.path.join(root, name)
    full = os.path.join(proj, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(body)
    if git:
        os.makedirs(os.path.join(proj, ".git"), exist_ok=True)
    return proj, full


class CanonRelTests(unittest.TestCase):
    def test_backslashes_and_dot_segments_normalize(self):
        self.assertEqual(loc.canon_rel(r".\backend\a\b.js"), "backend/a/b.js")

    def test_dot_prefixed_directory_survives(self):
        """`lstrip('./')` would turn this into 'github/workflows/x.yml'."""
        self.assertEqual(loc.canon_rel(".github/workflows/x.yml"),
                         ".github/workflows/x.yml")


class LocalResolutionTests(unittest.TestCase):
    def test_the_owners_exact_case_resolves(self):
        d = tempfile.mkdtemp()
        _, full = _mkproj(d, "GrantFlow", "backend/crawler-os/contract.js")
        res = loc.resolve_source_file("backend/crawler-os/contract.js",
                                      roots=[d], search_github=False)
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(full))
        self.assertEqual(res["method"], "local-project")
        self.assertFalse(res["ambiguous"])

    def test_a_path_that_includes_the_repo_folder_resolves(self):
        d = tempfile.mkdtemp()
        _, full = _mkproj(d, "GrantFlow", "backend/crawler-os/contract.js")
        res = loc.resolve_source_file("GrantFlow/backend/crawler-os/contract.js",
                                      roots=[d], search_github=False)
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(full))

    def test_an_existing_path_is_returned_untouched(self):
        d = tempfile.mkdtemp()
        _, full = _mkproj(d, "P", "a/b.js")
        res = loc.resolve_source_file(full, roots=[d], search_github=False)
        self.assertEqual(res["method"], "as-given")

    def test_two_projects_with_the_same_path_are_AMBIGUOUS_not_silent(self):
        """Refactor writes in place - a silent wrong pick edits the wrong repo."""
        d = tempfile.mkdtemp()
        _, f1 = _mkproj(d, "AlphaRepo", "src/util.js")
        _, f2 = _mkproj(d, "BetaRepo", "src/util.js")
        os.utime(f2, (10_000_000, 10_000_000))   # older
        res = loc.resolve_source_file("src/util.js", roots=[d],
                                      search_github=False)
        self.assertTrue(res["ambiguous"])
        self.assertEqual(len(res["candidates"]), 2)
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(f1))
        self.assertTrue(any("most recently modified" in n for n in res["notes"]))

    def test_github_breaks_the_tie_toward_the_REAL_checkout(self):
        """Measured live: 11 worktrees held the path and recency chose a scratch
        one (`gf-pkg-wt`) over `GrantFlow`. The repo name must win."""
        d = tempfile.mkdtemp()
        rel = "backend/crawler-os/contract.js"
        _, real = _mkproj(d, "GrantFlow", rel)
        _, scratch = _mkproj(d, "gf-pkg-wt", rel)
        os.utime(scratch, None)                       # scratch is NEWER
        os.utime(real, (10_000_000, 10_000_000))

        def fake_run(cmd, cwd=None, timeout=loc.TIMEOUT_S):
            return 0, "buckeye7066/GrantFlow\n"

        res = loc.resolve_source_file(rel, roots=[d], run=fake_run)
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(real))
        self.assertTrue(res["ambiguous"])

    def test_without_github_the_tie_falls_back_to_recency(self):
        d = tempfile.mkdtemp()
        rel = "backend/crawler-os/contract.js"
        _mkproj(d, "GrantFlow", rel)
        _, scratch = _mkproj(d, "gf-pkg-wt", rel)
        os.utime(scratch, None)
        res = loc.resolve_source_file(rel, roots=[d], search_github=False)
        self.assertEqual(os.path.normcase(res["path"]), os.path.normcase(scratch))
        self.assertTrue(any("most recently modified" in n for n in res["notes"]))

    def test_node_modules_is_never_scanned_as_a_project(self):
        d = tempfile.mkdtemp()
        _mkproj(d, "node_modules", "src/util.js", git=False)
        res = loc.resolve_source_file("src/util.js", roots=[d],
                                      search_github=False)
        self.assertIsNone(res["path"])


class GitHubResolutionTests(unittest.TestCase):
    def test_query_uses_owner_filename_and_directory(self):
        q = loc._search_query("backend/crawler-os/contract.js", "buckeye7066")
        self.assertIn("user:buckeye7066", q)
        self.assertIn("filename:contract.js", q)
        self.assertIn("path:backend/crawler-os", q)

    def test_github_hit_clones_the_repo_and_resolves_inside_it(self):
        """The tier that only GitHub can answer: the repo isn't on disk yet."""
        root = tempfile.mkdtemp()
        rel = "backend/crawler-os/contract.js"
        created = {}

        def fake_run(cmd, cwd=None, timeout=loc.TIMEOUT_S):
            if cmd[:2] == ["gh", "api"]:
                return 0, "buckeye7066/GrantFlow\n"
            if cmd[:3] == ["gh", "repo", "clone"]:
                _, full = _mkproj(root, "GrantFlow", rel)
                created["path"] = full
                return 0, ""
            return 1, "unexpected " + " ".join(cmd)

        res = loc.resolve_source_file(rel, roots=[root], run=fake_run)
        self.assertEqual(os.path.normcase(res["path"]),
                         os.path.normcase(created["path"]))
        self.assertEqual(res["method"], "github:buckeye7066/GrantFlow")
        self.assertTrue(any("cloned buckeye7066/GrantFlow" in n
                            for n in res["notes"]), res["notes"])

    def test_a_FAILED_lookup_is_never_reported_as_no_such_file(self):
        def fake_run(cmd, cwd=None, timeout=loc.TIMEOUT_S):
            return 127, "gh: not found"

        res = loc.resolve_source_file("a/b.js", roots=[tempfile.mkdtemp()],
                                      run=fake_run)
        self.assertIsNone(res["path"])
        self.assertTrue(any("GitHub lookup failed" in n for n in res["notes"]),
                        res["notes"])

    def test_multiple_repos_are_reported_as_ambiguous(self):
        def fake_run(cmd, cwd=None, timeout=loc.TIMEOUT_S):
            if cmd[:2] == ["gh", "api"]:
                return 0, "buckeye7066/RepoOne\nbuckeye7066/RepoTwo\n"
            return 1, "clone refused (test)"

        res = loc.resolve_source_file("src/util.js",
                                      roots=[tempfile.mkdtemp()], run=fake_run)
        self.assertTrue(res["ambiguous"])
        self.assertEqual(res["repos"],
                         ["buckeye7066/RepoOne", "buckeye7066/RepoTwo"])
        self.assertTrue(any("2 repos contain" in n for n in res["notes"]))

    def test_no_matching_repo_says_so_plainly(self):
        def fake_run(cmd, cwd=None, timeout=loc.TIMEOUT_S):
            return 0, ""

        res = loc.resolve_source_file("nope/missing.js",
                                      roots=[tempfile.mkdtemp()], run=fake_run)
        self.assertIsNone(res["path"])
        self.assertTrue(any("no repo under" in n for n in res["notes"]))

    def test_clone_is_attempted_when_the_repo_is_not_on_disk(self):
        calls = []

        def fake_run(cmd, cwd=None, timeout=loc.TIMEOUT_S):
            calls.append(cmd)
            if cmd[:2] == ["gh", "api"]:
                return 0, "buckeye7066/Ghost\n"
            return 1, "clone refused (test)"

        res = loc.resolve_source_file("src/util.js",
                                      roots=[tempfile.mkdtemp()], run=fake_run)
        self.assertIsNone(res["path"])
        self.assertTrue(any(c[:3] == ["gh", "repo", "clone"] for c in calls))
        self.assertTrue(any("clone of" in n for n in res["notes"]))


class FormatTests(unittest.TestCase):
    def test_every_note_survives_into_the_printed_line(self):
        res = {"path": "/x/y.js", "method": "local-project", "candidates": [],
               "notes": ["something worth knowing"], "ambiguous": False}
        self.assertIn("something worth knowing", loc.format_resolution("y.js", res))


if __name__ == "__main__":
    unittest.main(verbosity=2)
