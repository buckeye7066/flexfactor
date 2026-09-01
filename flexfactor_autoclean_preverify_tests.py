#!/usr/bin/env python3
"""Regression for pre-bootstrap autoclean verification semantics.

Old behaviour: autoclean's verify step could label the sweep RED solely because
dependencies were not yet installed. That permanently mislabels a clean source
baseline before bootstrap and reads as a source failure.

New contract: before bootstrap, autoclean returns UNVERIFIED with a clear
pre-bootstrap note; the main publication gate still runs later post-bootstrap.
"""
from __future__ import annotations

import unittest

import flexfactor as ff  # type: ignore


class PreBootstrapVerifySemanticsTests(unittest.TestCase):
    def test_missing_deps_before_bootstrap_returns_UNVERIFIED_with_note(self):
        stack = {
            "verification_is_real": False,
            "verification_note": "dependencies not installed for node:. - build gate would false-fail",
        }
        ok, note = ff._autoclean_preverify("/nonexistent", stack)  # path unused
        self.assertIsNone(ok, "pre-bootstrap autoclean must not label the sweep RED")
        self.assertIn("pre-bootstrap", note.lower())
        self.assertIn("dependencies not installed", note.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

