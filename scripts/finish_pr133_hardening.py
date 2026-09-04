#!/usr/bin/env python3
"""Apply the remaining reviewed PR #133 hardening edits, then remove itself."""
from __future__ import annotations

import ast
from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    if text.count(old) != 1:
        raise SystemExit(f"{label}: expected exactly one patch anchor, found {text.count(old)}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# 1) Direct flexfactor.main()/run_cli callers must receive the same hardening as
# launcher/installed CLI callers. Insert into each top-level entrypoint once.
core_path = Path("flexfactor.py")
core = core_path.read_text(encoding="utf-8")
tree = ast.parse(core)
lines = core.splitlines(keepends=True)
inserts: list[tuple[int, str]] = []
for node in tree.body:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    if node.name not in {"main", "run_cli"}:
        continue
    segment = ast.get_source_segment(core, node) or ""
    if "_runtime_directed.install(globals())" in segment:
        continue
    body_index = 0
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(getattr(node.body[0], "value", None), ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        body_index = 1
    if body_index >= len(node.body):
        raise SystemExit(f"flexfactor.{node.name}: no executable body anchor")
    insert_before = node.body[body_index].lineno - 1
    indent = " " * (node.col_offset + 4)
    payload = (
        indent + "# Arm canonical runtime hardening for direct callers too.\n"
        + indent + "import flexfactor_directed as _runtime_directed\n"
        + indent + "_runtime_directed.install(globals())\n"
    )
    inserts.append((insert_before, payload))
for insert_before, payload in sorted(inserts, reverse=True):
    lines.insert(insert_before, payload)
patched_core = "".join(lines)
ast.parse(patched_core)
core_path.write_text(patched_core, encoding="utf-8", newline="\n")

# 2) Evidence inventory must classify every supported PowerShell source type as
# source, otherwise changed .psm1/.psd1 files can be falsely treated as assets.
replace_once(
    Path("flexfactor_evidence.py"),
    '    ".h", ".hpp", ".sql", ".sh", ".ps1",\n',
    '    ".h", ".hpp", ".sql", ".sh", ".ps1", ".psm1", ".psd1",\n',
    "PowerShell evidence source parity",
)

# 3) Typo fallback must keep generic suffix normalization used by the precise
# lookup, e.g. GrantFlwo Repo should still find a unique GrantFlow checkout.
replace_once(
    Path("flexfactor_directed.py"),
    '''    hints: list[str] = []\n    for raw in name_hints or ():\n        slug = slugify(str(raw or ""))\n        compact = slug.replace("-", "")\n        if len(compact) >= 5 and compact not in hints:\n            hints.append(compact)\n''',
    '''    hints: list[str] = []\n    generic_suffixes = {"repo", "repository", "project", "program", "app", "application"}\n    for raw in name_hints or ():\n        slug = slugify(str(raw or ""))\n        variants = [slug]\n        parts = [part for part in slug.split("-") if part]\n        while parts and parts[-1] in generic_suffixes:\n            parts = parts[:-1]\n            if parts:\n                variants.append("-".join(parts))\n        for variant in variants:\n            compact = variant.replace("-", "")\n            if len(compact) >= 5 and compact not in hints:\n                hints.append(compact)\n''',
    "generic-suffix typo recovery",
)

# 4) Add focused regressions for the exact review findings. Existing security
# resolver tests remain in place.
test_path = Path("flexfactor_runtime_hardening_tests.py")
tests = test_path.read_text(encoding="utf-8")
if "import flexfactor_evidence as evidence\n" not in tests:
    tests = tests.replace(
        "import flexfactor_directed as directed\n",
        "import flexfactor_directed as directed\nimport flexfactor_evidence as evidence\n",
        1,
    )
marker = '\n\nif __name__ == "__main__":\n'
addition = r'''
    def test_direct_main_entry_arms_hardening_without_test_installer(self):
        import inspect
        source = inspect.getsource(ff.main)
        self.assertIn("_runtime_directed.install(globals())", source)

    def test_all_powershell_extensions_are_evidence_sources(self):
        self.assertTrue(directed._POWERSHELL_EXTS <= evidence.SOURCE_EXTENSIONS)

    def test_generic_suffix_does_not_defeat_unique_typo_recovery(self):
        original_roots = ff._PROJECT_ROOTS
        try:
            with tempfile.TemporaryDirectory() as root:
                wanted = os.path.join(root, "GrantFlow")
                os.mkdir(wanted)
                ff._PROJECT_ROOTS = [root]
                for hint in ("GrantFlwo Repo", "GrantFlwo Project"):
                    self.assertEqual(
                        os.path.normcase(ff._find_local_project(hint)),
                        os.path.normcase(wanted),
                    )
        finally:
            ff._PROJECT_ROOTS = original_roots

    def test_empty_powershell_whole_file_response_is_rejected_before_parser(self):
        with mock.patch.object(directed, "powershell_syntax_details") as parser:
            ok, note, parsed = ff._prewrite_source_syntax_details(
                ".", "repair.ps1", "", [], allow_empty=False,
            )
        self.assertFalse(ok)
        self.assertIn("empty whole-file response", note)
        self.assertIsNone(parsed)
        parser.assert_not_called()
'''
if "test_direct_main_entry_arms_hardening_without_test_installer" not in tests:
    if marker not in tests:
        raise SystemExit("runtime hardening test insertion anchor missing")
    tests = tests.replace(marker, "\n" + addition + marker, 1)
test_path.write_text(tests, encoding="utf-8", newline="\n")

# This script is deliberately temporary. The workflow removes both temporary
# patch artifacts in the same verified commit after this script succeeds.
print("PR #133 reviewed hardening patches applied")
