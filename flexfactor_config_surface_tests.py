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
    cloud = os.path.join(HERE, "cloud")
    if os.path.isdir(cloud):
        for base, _, files in os.walk(cloud):
            if os.path.basename(base) in {"node_modules", "test"}:
                continue
            for name in files:
                if name.endswith((".js", ".mjs", ".cjs")):
                    out.append(os.path.join(base, name))
    return sorted(set(out))


def read_by_runtime() -> dict:
    """{VAR: file that reads it} across the runtime modules.

    TWO patterns, because one of them is how a variable escapes this gate.
    The literal form - os.environ.get("NAME") - is the obvious one. The other is
    a module constant holding the name:

        READONLY_URL_ENV = "FLEXFACTOR_READONLY_DATABASE_URL"
        ...
        (env if env is not None else os.environ).get(READONLY_URL_ENV)

    which the literal scan cannot see at all, so an entire module's worth of
    configuration could be added with none of it documented and this test would
    still pass. Measured 2026-08-28: flexfactor_prodevidence.py reads four
    variables that way and the gate reported nothing missing.

    The constant form is matched by VALUE - a module-level string that spells a
    FLEXFACTOR_/FF_/AI_ variable name - rather than by tracing the reference.
    That is deliberately an over-approximation: the worst it can do is ask for a
    line of documentation about a name the project chose to define."""
    literal = re.compile(
        r"""(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[)\s*["']([A-Z][A-Z_0-9]*)["']""")
    javascript_dot = re.compile(r"""process\.env\.([A-Z][A-Z_0-9]*)""")
    javascript_index = re.compile(
        r"""process\.env\[\s*["']([A-Z][A-Z_0-9]*)["']\s*\]""")
    named_constant = re.compile(
        r"""^\s*[A-Z][A-Z_0-9]*\s*(?::\s*str\s*)?=\s*["']((?:FLEXFACTOR|FF|AI)_[A-Z_0-9]*)["']""",
        re.M)
    found = {}
    for path in _runtime_sources():
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for pattern in (literal, named_constant):
            for name in pattern.findall(text):
                found.setdefault(name, os.path.basename(path))
        for pattern in (javascript_dot, javascript_index):
            for name in pattern.findall(text):
                found.setdefault(name, os.path.relpath(path, HERE))
    return found


class ConfigSurfaceTests(unittest.TestCase):
    def test_the_template_exists_where_the_readiness_gate_looks_for_it(self):
        self.assertTrue(os.path.isfile(TEMPLATE), ".env.example must be tracked")

    def test_the_template_lists_no_variable_the_code_never_reads(self):
        listed = documented()
        self.assertTrue(listed, "the template must document something")
        # The runtime is not only Python: flexfactor_assets/*.js is PACKAGE DATA
        # driven by flexfactor_journeys, and it reads its own knobs. Scanning
        # .py alone made a real, documented variable look like fiction.
        sources = _runtime_sources() + [
            os.path.join(HERE, f) for f in os.listdir(HERE)
            if f.endswith(".py") and (f.startswith("test_") or f.endswith("_tests.py"))]
        assets = os.path.join(HERE, "flexfactor_assets")
        if os.path.isdir(assets):
            sources += [os.path.join(assets, f) for f in os.listdir(assets)
                        if f.endswith((".js", ".mjs", ".cjs"))]
        # ...nor only Python and JavaScript. The .ps1 launchers ARE the runtime
        # on this platform - they are what a desktop shortcut starts - and they
        # read their own variables. A launcher knob documented in the template
        # would have been reported as fiction purely because no .py mentions it.
        sources += [os.path.join(HERE, f) for f in os.listdir(HERE)
                    if f.endswith(".ps1")]
        scripts = os.path.join(HERE, "scripts")
        if os.path.isdir(scripts):
            sources += [os.path.join(scripts, f) for f in os.listdir(scripts)
                        if f.endswith(".ps1")]
        blob = ""
        for path in sources:
            with open(path, encoding="utf-8", errors="replace") as fh:
                blob += fh.read()
        fiction = sorted(n for n in listed if n not in blob)
        self.assertEqual(fiction, [], f"documented but never read: {fiction}")

    def test_a_launcher_only_variable_is_not_called_fiction(self):
        """The .ps1 launchers are the runtime a desktop shortcut starts.

        FLEXFACTOR_PYTHON is read only by scripts\\flexfactor_python.ps1 - no
        Python file mentions it - so before the launchers were scanned, the
        template documenting it would have failed as documented-but-never-read.
        The variable is real: it is how a host with several Pythons pins the one
        FlexFactor runs, and how the launcher-parity suite reaches its stub."""
        self.assertIn("FLEXFACTOR_PYTHON", documented())
        resolver = os.path.join(HERE, "scripts", "flexfactor_python.ps1")
        self.assertTrue(os.path.isfile(resolver))
        with open(resolver, encoding="utf-8") as fh:
            self.assertIn("FLEXFACTOR_PYTHON", fh.read())

    def test_every_runtime_variable_is_documented(self):
        listed = documented()
        missing = sorted(f"{n} ({where})" for n, where in read_by_runtime().items()
                         if n not in listed and n not in NOT_OURS)
        self.assertEqual(missing, [],
                         "read by the runtime but absent from .env.example: "
                         f"{missing}. Add it there (with what it does and its "
                         "default) or add it to NOT_OURS if the OS owns it.")

    def test_the_gate_sees_a_variable_read_through_a_module_constant(self):
        """The check that could not fail for a whole class of variable.

        flexfactor_prodevidence.py names its four connection variables in module
        constants and reads them through those, which the literal scan cannot
        see - so they were invisible to the gate that exists to notice exactly
        this. Asserting on real names keeps the widened detector honest: if
        someone narrows it back, this fails."""
        found = read_by_runtime()
        for name in ("FLEXFACTOR_READONLY_DATABASE_URL",
                     "FLEXFACTOR_READONLY_STATEMENT_TIMEOUT_MS",
                     "FLEXFACTOR_DB_CONNECT_TIMEOUT_S"):
            self.assertIn(name, found,
                          f"{name} is read through a module constant and the "
                          "gate must still see it")

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
