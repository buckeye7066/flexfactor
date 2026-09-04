from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import flexfactor as ff
import flexfactor_cmdpolicy as command_policy
import flexfactor_directed as directed


class RuntimeHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ff._ff_directed.install(vars(ff))

    def test_powershell_is_first_class_audited_source(self):
        for extension in directed._POWERSHELL_EXTS:
            self.assertIn(extension, ff._CODE_EXTS)
            self.assertEqual(ff._TREE_SITTER_LANGUAGE_BY_EXT.get(extension), "powershell")
        self.assertTrue(getattr(ff._prewrite_source_syntax_details, "_powershell_hardened", False))

    def test_unique_project_typo_resolves_but_ambiguity_fails_closed(self):
        original_roots = ff._PROJECT_ROOTS
        try:
            with tempfile.TemporaryDirectory() as root:
                wanted = os.path.join(root, "family-stewardship")
                os.mkdir(wanted)
                ff._PROJECT_ROOTS = [root]
                self.assertEqual(
                    os.path.normcase(ff._find_local_project("family stewarship")),
                    os.path.normcase(wanted),
                )

            with tempfile.TemporaryDirectory() as root:
                os.mkdir(os.path.join(root, "family-stewardship"))
                os.mkdir(os.path.join(root, "family-stewardshap"))
                ff._PROJECT_ROOTS = [root]
                self.assertIsNone(ff._find_local_project("family stewardshp"))
        finally:
            ff._PROJECT_ROOTS = original_roots

    def test_project_typo_distance_is_bounded(self):
        self.assertEqual(directed._bounded_damerau_levenshtein("stewardhsip", "stewardship", 2), 1)
        self.assertEqual(directed._bounded_damerau_levenshtein("stewarship", "stewardship", 2), 1)
        self.assertIsNone(directed._bounded_damerau_levenshtein("steward", "stewardship", 2))

    def test_powershell_resolver_never_uses_path_or_checkout_binary(self):
        with tempfile.TemporaryDirectory() as checkout:
            malicious = os.path.join(checkout, "powershell.exe")
            with open(malicious, "wb") as fh:
                fh.write(b"MZ")
            with mock.patch.dict(os.environ, {"PATH": checkout}, clear=False):
                resolved = directed._powershell_parser_executable(
                    platform_name="nt",
                    environ={"PATH": checkout, "SystemRoot": os.path.join(checkout, "missing")},
                )
        self.assertIsNone(resolved)

    def test_powershell_resolver_accepts_trusted_windows_system_location(self):
        with tempfile.TemporaryDirectory() as root:
            trusted = os.path.join(
                root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
            os.makedirs(os.path.dirname(trusted), exist_ok=True)
            with open(trusted, "wb") as fh:
                fh.write(b"MZ")
            resolved = directed._powershell_parser_executable(
                platform_name="nt", environ={"SystemRoot": root}
            )
        self.assertEqual(os.path.normcase(resolved or ""), os.path.normcase(os.path.realpath(trusted)))

    def test_powershell_parser_uses_parse_only_file_invocation(self):
        captured = {}

        def fake_run(command, cwd, timeout):
            captured["command"] = list(command)
            captured["cwd"] = cwd
            captured["timeout"] = timeout
            driver = command[command.index("-File") + 1]
            candidate = command[-1]
            self.assertTrue(os.path.isfile(driver))
            self.assertTrue(os.path.isfile(candidate))
            with open(driver, "r", encoding="utf-8") as fh:
                self.assertIn("Language.Parser]::ParseFile", fh.read())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        source = "$code = 4\nWrite-Output ${code}\n"
        with mock.patch.object(directed, "_powershell_parser_executable", return_value="powershell.exe"):
            result = directed.powershell_syntax_details(".", "repair.ps1", source, fake_run)

        self.assertEqual(result, (True, "PowerShell AST parse", source))
        self.assertIn("-File", captured["command"])
        self.assertNotIn("-Command", captured["command"])
        self.assertEqual(captured["timeout"], 60)
        self.assertNotIn("destructive", command_policy.classify_command(captured["command"]))

    def test_powershell_parse_error_blocks_mutation(self):
        def fake_run(command, cwd, timeout):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Variable reference is not valid. ':' was not followed by a valid variable name character.",
            )

        source = 'Write-Error "failure $code: details"\n'
        with mock.patch.object(directed, "_powershell_parser_executable", return_value="powershell.exe"):
            ok, note, parsed = directed.powershell_syntax_details(".", "repair.ps1", source, fake_run)
        self.assertFalse(ok)
        self.assertIn("Variable reference is not valid", note)
        self.assertIsNone(parsed)

    def test_missing_native_powershell_falls_back_instead_of_crashing(self):
        with mock.patch.object(directed, "_powershell_parser_executable", return_value=None):
            self.assertIsNone(
                directed.powershell_syntax_details(".", "repair.ps1", "Write-Output 'ok'\n", lambda *_a, **_k: None)
            )


if __name__ == "__main__":
    unittest.main()
