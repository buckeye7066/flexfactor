"""Unit tests for the truthful trusted-repo execution boundary."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import flexfactor_trust as trust


class TrustBoundaryTests(unittest.TestCase):
    def test_containment_claim_denies_os_sandbox(self):
        claim = trust.containment_claim().lower()
        self.assertIn("not", claim)
        self.assertIn("sandbox", claim)

    def test_unknown_tree_refused_without_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Force empty rules via env
            old = os.environ.pop("FLEXFACTOR_TRUSTED_REPOS", None)
            try:
                # Point policy at a missing path by clearing env and using a
                # non-matching decision against default policy file — monkeypatch
                # load to empty.
                real = trust.load_trusted_repo_rules
                trust.load_trusted_repo_rules = lambda: ([], "test:empty")
                try:
                    d = trust.trust_decision(tmp)
                finally:
                    trust.load_trusted_repo_rules = real
                self.assertFalse(d.allowed)
                self.assertIn("trusted_repos", d.reason)
            finally:
                if old is not None:
                    os.environ["FLEXFACTOR_TRUSTED_REPOS"] = old

    def test_trusted_prefix_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FLEXFACTOR_TRUSTED_REPOS"] = tmp
            try:
                child = os.path.join(tmp, "proj")
                os.makedirs(child)
                d = trust.trust_decision(child)
                self.assertTrue(d.allowed)
                self.assertEqual(d.matched_rule, tmp)
            finally:
                os.environ.pop("FLEXFACTOR_TRUSTED_REPOS", None)

    def test_allow_untrusted_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("FLEXFACTOR_TRUSTED_REPOS", None)
            d = trust.trust_decision(tmp, allow_untrusted=True)
            self.assertTrue(d.allowed)
            self.assertIn("trust-repo", d.reason)

    def test_frozen_npm_ci_when_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "package-lock.json"), "w", encoding="utf-8").write("{}")
            lock = trust.lockfile_for_ecosystem(tmp, "node")
            self.assertEqual(lock, "package-lock.json")
            argv = trust.frozen_install_argv("node", "npm", lock, allow_scripts=False)
            self.assertEqual(argv[:2], ["npm", "ci"])
            self.assertIn("--ignore-scripts", argv)

    def test_policy_json_trusted_repos(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = os.path.join(home, ".flexfactor")
            os.makedirs(cfg)
            with tempfile.TemporaryDirectory() as proj:
                with open(os.path.join(cfg, "policy.json"), "w", encoding="utf-8") as fh:
                    json.dump({"trusted_repos": [proj]}, fh)
                real_home = os.path.expanduser("~")
                # Patch POLICY_PATH
                old_path = trust.POLICY_PATH
                trust.POLICY_PATH = os.path.join(cfg, "policy.json")
                os.environ.pop("FLEXFACTOR_TRUSTED_REPOS", None)
                try:
                    d = trust.trust_decision(proj)
                    self.assertTrue(d.allowed, d.reason)
                finally:
                    trust.POLICY_PATH = old_path
                    _ = real_home


if __name__ == "__main__":
    unittest.main()
