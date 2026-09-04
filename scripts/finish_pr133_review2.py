#!/usr/bin/env python3
"""Close the two remaining exact-head PR #133 review findings, then self-remove."""
from __future__ import annotations

import ast
import re
from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# P2 #1: typo recovery must consume the SAME generic suffix vocabulary as the
# core lookup. Keep a complete standalone default for callers outside flexfactor.
directed = Path("flexfactor_directed.py")
replace_once(
    directed,
    "def typo_resolve_local_project(name_hints, roots, slugify) -> str | None:\n",
    "def typo_resolve_local_project(name_hints, roots, slugify, generic_tokens=None) -> str | None:\n",
    "typo resolver signature",
)
replace_once(
    directed,
    '    generic_suffixes = {"repo", "repository", "project", "program", "app", "application"}\n',
    '    generic_suffixes = set(generic_tokens or {"repo", "repository", "project", "program", "app", "application", "source", "src", "main", "master", "dev", "prod", "code", "github"})\n',
    "canonical generic suffix fallback",
)
replace_once(
    directed,
    '''            return typo_resolve_local_project(\n                name_hints, module_globals.get("_PROJECT_ROOTS", ()),\n                module_globals.get("_slugify", lambda value: str(value).lower()),\n            )\n''',
    '''            return typo_resolve_local_project(\n                name_hints, module_globals.get("_PROJECT_ROOTS", ()),\n                module_globals.get("_slugify", lambda value: str(value).lower()),\n                module_globals.get("_GENERIC_NAME_TOKENS", ()),\n            )\n''',
    "canonical generic suffix injection",
)

# P2 #2: expand the canonical publication-failure source regex to every newly
# first-class PowerShell source extension. Locate the AST assignment so this
# patch cannot accidentally edit a different regex.
core_path = Path("flexfactor.py")
core = core_path.read_text(encoding="utf-8")
tree = ast.parse(core)
assignment = None
for node in ast.walk(tree):
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        continue
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if any(isinstance(target, ast.Name) and target.id == "_FAILURE_SOURCE_RE" for target in targets):
        assignment = node
        break
if assignment is None or not getattr(assignment, "end_lineno", None):
    raise SystemExit("_FAILURE_SOURCE_RE assignment not found")
lines = core.splitlines(keepends=True)
start = assignment.lineno - 1
end = assignment.end_lineno
segment = "".join(lines[start:end])
if all(ext in segment for ext in ("ps1", "psm1", "psd1")):
    patched_segment = segment
else:
    candidates = []
    for match in re.finditer(r"\\\.\(\?:([A-Za-z0-9_|-]+)\)", segment):
        parts = match.group(1).split("|")
        if "py" in parts and any(item in parts for item in ("js", "jsx", "ts", "tsx")):
            candidates.append(match)
    if len(candidates) != 1:
        raise SystemExit(
            "Could not uniquely locate source-extension alternation inside _FAILURE_SOURCE_RE; "
            f"found {len(candidates)} candidates:\n{segment}"
        )
    match = candidates[0]
    parts = match.group(1).split("|")
    for ext in ("ps1", "psm1", "psd1"):
        if ext not in parts:
            parts.append(ext)
    patched = r"\.(?:" + "|".join(parts) + ")"
    patched_segment = segment[:match.start()] + patched + segment[match.end():]
    lines[start:end] = [patched_segment]
    core = "".join(lines)
    ast.parse(core)
    core_path.write_text(core, encoding="utf-8", newline="\n")

# Focused regressions pin the exact newly reported cases.
test_path = Path("flexfactor_runtime_hardening_tests.py")
tests = test_path.read_text(encoding="utf-8")
marker = '\n\nif __name__ == "__main__":\n'
addition = r'''
    def test_canonical_generic_suffixes_survive_typo_recovery(self):
        original_roots = ff._PROJECT_ROOTS
        try:
            with tempfile.TemporaryDirectory() as root:
                wanted = os.path.join(root, "GrantFlow")
                os.mkdir(wanted)
                ff._PROJECT_ROOTS = [root]
                for suffix in ("Source", "Src", "Main", "Master", "Dev", "Prod", "Code", "Github"):
                    self.assertEqual(
                        os.path.normcase(ff._find_local_project(f"GrantFlwo {suffix}")),
                        os.path.normcase(wanted),
                        suffix,
                    )
        finally:
            ff._PROJECT_ROOTS = original_roots

    def test_publication_failure_regex_recognizes_every_powershell_source_type(self):
        for extension in directed._POWERSHELL_EXTS:
            match = ff._FAILURE_SOURCE_RE.search(f"FAILED src/repair{extension}:12: parser failure")
            self.assertIsNotNone(match, extension)
            self.assertTrue(match.group("path").endswith(extension), (extension, match.group("path")))
'''
if "test_canonical_generic_suffixes_survive_typo_recovery" not in tests:
    if marker not in tests:
        raise SystemExit("test insertion anchor not found")
    tests = tests.replace(marker, "\n" + addition + marker, 1)
test_path.write_text(tests, encoding="utf-8", newline="\n")

print("remaining PR #133 review findings patched")
