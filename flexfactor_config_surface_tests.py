#!/usr/bin/env python3
"""`.env.example` is the tool's configuration contract - keep it true.

FlexFactor's own readiness rubric has a `config_documented` gate ("Commit a
.env.example listing every required variable"), and it was FAILING on FlexFactor
itself until 2026-08-23. A template that exists but drifts is worse than none:
it is a document an operator will trust.

Two directions, both checked:
  * every variable the template lists must actually be read somewhere, or the
    template is fiction;
  * every variable the RUNTIME reads must be listed, or the template is a
    partial map with no way to tell which half you are holding.

    python flexfactor_config_surface_tests.py
"""
from __future__ import annotations

import glob
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, ".env.example")

# Read from the environment but NOT FlexFactor's own configuration: OS/toolchain
# variables the code merely consults. Documenting these as knobs would be the
# opposite error - implying the tool acts on something the OS owns.
NOT_OURS = {
    "PATH", "PATHEXT", "APPDATA", "LOCALAPPDATA", "COMPUTERNAME", "TMPDIR",
    "TEMP", "TMP", "USERPROFILE", "HOME", "COMSPEC", "SYSTEMROOT",
    "PYTHONIOENCODING", "PYTHONPATH", "VIRTUAL_ENV", "CI", "NODE_OPTIONS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",   # the machine's proxy config
    # Another program's state directory, honoured for interop, not a FlexFactor
    # setting - but it IS documented in the template anyway, which is allowed:
    # the check below is one-directional for this set.
    "PURPOSE_FOUNDRY_OBSIDIAN_INBOX",
}

RUNTIME_GLOBS = ("flexfactor.py", "flexfactor_*.py")
TEST_SUFFIXES = ("_tests.py",)


def documented() -> set:
    names = set()
    with open(TEMPLATE, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^([A-Z][A-Z_0-9]*)=", line.strip())
            if m:
                names.add(m.group(1))
    return names


def _runtime_sources() -> list:
    out = []
    for pattern in RUNTIME_GLOBS:
        for path in glob.glob(os.path.join(HERE, pattern)):
            base = os.path.basename(path)
            if base.startswith("test_") or base.endswith(TEST_SUFFIXES):
                continue
            out.append(path)
    return sorted(set(out))


def read_by_runtime() -> dict:
    """{VAR: file that reads it} across the runtime modules."""
    pattern = re.compile(
        r"""(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[)\s*["']([A-Z][A-Z_0-9]*)["']""")
    found = {}
    for path in _runtime_sources():
        with open(path, encoding="utf-8", errors="replace") as fh:
            for name in pattern.findall(fh.read()):
                found.setdefault(name, os.path.basename(path))
    return found


class ConfigSurfaceTests(unittest.TestCase):
    def test_the_template_exists_where_the_readiness_gate_looks_for_it(self):
        self.assertTrue(os.path.isfile(TEMPLATE), ".env.example must be tracked")

    def test_the_template_lists_no_variable_the_code_never_reads(self):
        listed = documented()
        self.assertTrue(listed, "the template must document something")
        sources = _runtime_sources() + [
            os.path.join(HERE, f) for f in os.listdir(HERE)
            if f.endswith(".py") and (f.startswith("test_") or f.endswith("_tests.py"))]
        blob = ""
        for path in sources:
            with open(path, encoding="utf-8", errors="replace") as fh:
                blob += fh.read()
        fiction = sorted(n for n in listed if n not in blob)
        self.assertEqual(fiction, [], f"documented but never read: {fiction}")

    def test_every_runtime_variable_is_documented(self):
        listed = documented()
        missing = sorted(f"{n} ({where})" for n, where in read_by_runtime().items()
                         if n not in listed and n not in NOT_OURS)
        self.assertEqual(missing, [],
                         "read by the runtime but absent from .env.example: "
                         f"{missing}. Add it there (with what it does and its "
                         "default) or add it to NOT_OURS if the OS owns it.")

    def test_the_template_ships_no_real_secret(self):
        # A template with a live key in it is the failure this whole gate exists
        # to prevent, and it would be committed.
        with open(TEMPLATE, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                m = re.match(r"^([A-Z][A-Z_0-9]*)=(.*)$", line.strip())
                if not m:
                    continue
                name, value = m.group(1), m.group(2).split("#")[0].strip()
                if not value:
                    continue
                self.assertFalse(
                    re.search(r"(sk-|ghp_|gho_|github_pat_|xox|AIza|-----BEGIN)", value),
                    f"{TEMPLATE}:{i} {name} looks like a real credential")
                self.assertLess(len(value), 60,
                                f"{TEMPLATE}:{i} {name} value is suspiciously long")

    def test_dotenv_is_ignored_but_the_template_is_not(self):
        # The gate that was failing. Assert the OUTCOME (what git will do), not
        # the presence of a line in .gitignore.
        import subprocess
        def ignored(path):
            cp = subprocess.run(["git", "-C", HERE, "check-ignore", "-q", path],
                                capture_output=True)
            return cp.returncode == 0
        self.assertTrue(ignored(".env"), ".env must be git-ignored")
        self.assertTrue(ignored(".env.local"), ".env.* must be git-ignored")
        self.assertFalse(ignored(".env.example"),
                         "the template itself must stay tracked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
