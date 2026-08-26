"""Entry-point parity + clean-install tests (acceptance tests A, B, C).

Every supported launch path must be THE SAME runtime: same version, modes,
safety-module wiring and exit-code semantics. A guard that exists on one path
only is not a system guarantee. These tests drive the real processes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

ENTRY_POINTS = {
    "source-script": [PY, os.path.join(HERE, "flexfactor.py")],
    "module": [PY, "-m", "flexfactor"],
    "run-shim": [PY, os.path.join(HERE, "flexfactor_run.py")],
}

LAUNCHERS = ("flexfactor_launch.ps1", "flexfactor_audit_launch.ps1",
             "flexfactor_scout_launch.ps1", "flexfactor_glimmer_launch.ps1")

PARITY_KEYS = ("tool_version", "modes", "wired", "exit_codes")

MODES = ("refactor", "scout", "audit", "prodready", "policy")


def _manifest(argv0: list[str], cwd: str = HERE, env: dict | None = None) -> dict:
    cp = subprocess.run(argv0 + ["--runtime-manifest"], cwd=cwd, env=env,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=300)
    assert cp.returncode == 0, (argv0, cp.returncode, cp.stderr[-2000:])
    return json.loads(cp.stdout)


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_launcher(name: str, launcher_args: list[str], answers: list[str],
                  extra_env: dict[str, str]) -> list[str]:
    """Execute a launcher while replacing only its external side effects."""
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        raise unittest.SkipTest(
            "no PowerShell on this host: launcher execution UNVERIFIED (blocked, not passed)")
    answer_load = "\n".join(
        f"$global:FlexFactorTestAnswers.Enqueue({_ps_literal(answer)})"
        for answer in answers)
    invoke = " ".join(_ps_literal(arg) for arg in launcher_args)
    harness = f"""
$ErrorActionPreference = 'Stop'
$global:FlexFactorTestAnswers = [System.Collections.Generic.Queue[string]]::new()
{answer_load}
function global:Read-Host {{
    param([string]$Prompt)
    if ($global:FlexFactorTestAnswers.Count -gt 0) {{
        return $global:FlexFactorTestAnswers.Dequeue()
    }}
    return ''
}}
function global:Invoke-WebRequest {{
    param([string]$Uri, [int]$TimeoutSec, [switch]$UseBasicParsing)
    return [pscustomobject]@{{
        StatusCode = 200
        Content = '{{"models":[{{"name":"muse-glimmer:30b"}}]}}'
    }}
}}
function global:python {{
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$PythonArgs)
    Write-Output ('FLEXFACTOR_TEST_PYTHON_ARGS=' + ($PythonArgs -join [char]31))
    $global:LASTEXITCODE = 0
}}
& {_ps_literal(os.path.join(HERE, name))} {invoke}
"""
    env = dict(os.environ)
    env.update(extra_env)
    cp = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", harness],
                        cwd=HERE, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    marker = "FLEXFACTOR_TEST_PYTHON_ARGS="
    rows = [line for line in cp.stdout.splitlines() if line.startswith(marker)]
    if cp.returncode != 0 or len(rows) != 1:
        raise AssertionError((name, cp.returncode, cp.stdout[-3000:], cp.stderr[-3000:]))
    return rows[0][len(marker):].split(chr(31))


class EntryPointParityTests(unittest.TestCase):
    def test_every_source_entry_point_reports_one_runtime(self):
        manifests = {name: _manifest(argv) for name, argv in ENTRY_POINTS.items()}
        base = manifests["source-script"]
        for name, got in manifests.items():
            for key in PARITY_KEYS:
                self.assertEqual(got[key], base[key], f"{name}: {key} differs")
            self.assertEqual(os.path.normcase(got["module_file"]),
                             os.path.normcase(base["module_file"]), name)

    def test_every_safety_module_is_importable_from_the_checkout(self):
        m = _manifest(ENTRY_POINTS["source-script"])
        missing = [k for k, v in m["modules"].items() if not v["importable"]]
        self.assertEqual(missing, [], m["modules"])

    def test_every_module_the_runtime_imports_is_in_the_wheel(self):
        """A module imported at runtime but missing from `py-modules` is a
        feature that silently does not exist in an installed FlexFactor.

        Found live 2026-08-23: `flexfactor_errors` - the per-run error ledger,
        and everything the dashboard's error box reads - was imported by
        `_start_error_ledger` and absent from the wheel, so a packaged run
        printed "[errors] ledger unavailable: No module named ..." and reported
        nothing. The CI import step could not catch it: it imports the LIST,
        so the list is its own oracle. This compares the list against what the
        source actually imports.
        """
        import re as _re, tomllib
        with open(os.path.join(HERE, "pyproject.toml"), "rb") as fh:
            packaged = set(tomllib.load(fh)["tool"]["setuptools"]["py-modules"])
        pattern = _re.compile(r"^\s*(?:import|from)\s+(flexfactor_[a-z_0-9]+)", _re.M)
        needed = set()
        for name in os.listdir(HERE):
            if (not name.startswith("flexfactor") or not name.endswith(".py")
                    or name.endswith("_tests.py")):
                continue
            with open(os.path.join(HERE, name), encoding="utf-8", errors="replace") as fh:
                for mod in pattern.findall(fh.read()):
                    if os.path.isfile(os.path.join(HERE, mod + ".py")):
                        needed.add(mod)
        missing = sorted(needed - packaged)
        self.assertEqual(missing, [], f"imported by the runtime, absent from "
                                      f"pyproject py-modules: {missing}")

    def test_directed_orchestration_is_wired_without_the_shim(self):
        m = _manifest(ENTRY_POINTS["source-script"])
        self.assertTrue(m["wired"]["directed"])
        self.assertTrue(m["wired"]["command_policy"])
        self.assertTrue(m["wired"]["egress"])

    def test_mode_help_is_identical_across_entry_points(self):
        for mode in MODES:
            outs = {}
            for name, argv in ENTRY_POINTS.items():
                cp = subprocess.run(argv + [mode, "--help"], cwd=HERE,
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=300)
                self.assertEqual(cp.returncode, 0, (name, mode, cp.stderr[-1000:]))
                # argparse derives prog from argv[0]; normalise that one token.
                outs[name] = (cp.stdout.replace("flexfactor_run.py", "flexfactor.py")
                              .replace("__main__.py", "flexfactor.py"))
            self.assertEqual(len(set(outs.values())), 1, mode)

    def test_usage_error_exit_code_is_two_everywhere(self):
        for name, argv in ENTRY_POINTS.items():
            cp = subprocess.run(argv + ["audit", "--no-such-flag"], cwd=HERE,
                                capture_output=True, text=True, timeout=300)
            self.assertEqual(cp.returncode, 2, name)

    def test_launchers_resolve_the_shim_next_to_themselves(self):
        for name in LAUNCHERS:
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertRegex(
                src,
                re.compile(r"Join-Path\s+\$PSScriptRoot\s+['\"]flexfactor_run\.py['\"]"),
                name,
            )
            self.assertNotIn(r"C:\Users\firer\flexfactor\flexfactor_run.py", src, name)
            # ASCII-only launcher constraint (WinPS 5.1 without a BOM mangles UTF-8).
            src.encode("ascii")

    @unittest.skipUnless(shutil.which("powershell") or shutil.which("pwsh"),
                         "no PowerShell on this host: launcher parse UNVERIFIED (blocked, not passed)")
    def test_launchers_parse_under_powershell(self):
        ps = shutil.which("pwsh") or shutil.which("powershell")
        for name in LAUNCHERS:
            path = os.path.join(HERE, name).replace("'", "''")
            script = ("$t=$null;$e=$null;[System.Management.Automation.Language.Parser]"
                      "::ParseFile('" + path + "',[ref]$t,[ref]$e)|Out-Null;"
                      "if($e.Count -ne 0){$e|Out-String|Write-Error;exit 1};exit 0")
            cp = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=120)
            self.assertEqual(cp.returncode, 0, (name, cp.stderr[-1000:]))

    def test_launchers_execute_and_forward_the_expected_arguments(self):
        """Run real PowerShell control flow; stub only network/model processes."""
        script = os.path.join(HERE, "flexfactor_run.py")
        target = "portfolio-target"
        cases = (
            ("flexfactor_launch.ps1", [target], ["2", "anthropic", ""],
             {"ANTHROPIC_API_KEY": "test-placeholder", "OPENAI_API_KEY": ""},
             [script, "scout", "--program", target, "--provider", "anthropic"]),
            ("flexfactor_audit_launch.ps1", [target], ["n", ""],
             {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "",
              "OPENAI_API_KEY": "test-placeholder"},
             [script, "audit", "--provider", "openai", "--program", target,
              "--apply", "--yes", "--allow-dirty", "--auto-clean"]),
            ("flexfactor_scout_launch.ps1", [target], ["openai", "YES", "report", ""],
             {"OPENAI_API_KEY": "test-placeholder",
              "FLEXFACTOR_REPO_REWARDS_URL": "http://127.0.0.1:3000"},
             [script, "scout", "--program", target, "--provider", "openai",
              "--repo-rewards-url", "http://127.0.0.1:3000", "--no-auto-start",
              "--allow-remote-program-context"]),
            ("flexfactor_glimmer_launch.ps1", ["audit", "--program", target], [], {},
             [script, "audit", "--program", target, "--provider", "ollama",
              "--model", "muse-glimmer:30b"]),
        )
        for name, args, answers, env, expected in cases:
            with self.subTest(launcher=name):
                self.assertEqual(_run_launcher(name, args, answers, env), expected)


class CleanInstallTests(unittest.TestCase):
    """Build the wheel, install it in a FRESH venv, run the installed command
    from a directory OUTSIDE the checkout. This is the only proof that the
    packaged artifact is the same runtime as the source tree."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ff-install-")
        wheel_dir = os.path.join(cls.tmp, "wheel")
        cp = subprocess.run([PY, "-m", "pip", "wheel", HERE, "--no-deps", "-q",
                             "--wheel-dir", wheel_dir], capture_output=True,
                            text=True, timeout=600)
        assert cp.returncode == 0, cp.stderr[-3000:]
        wheels = [f for f in os.listdir(wheel_dir) if f.endswith(".whl")]
        assert len(wheels) == 1, wheels
        cls.wheel = os.path.join(wheel_dir, wheels[0])
        cls.venv = os.path.join(cls.tmp, "venv")
        venv.EnvBuilder(with_pip=True, clear=True).create(cls.venv)
        bindir = "Scripts" if os.name == "nt" else "bin"
        exe = ".exe" if os.name == "nt" else ""
        cls.vpy = os.path.join(cls.venv, bindir, "python" + exe)
        cls.console = os.path.join(cls.venv, bindir, "flexfactor" + exe)
        cp = subprocess.run([cls.vpy, "-m", "pip", "install", "-q", "--no-deps", cls.wheel],
                            capture_output=True, text=True, timeout=600)
        assert cp.returncode == 0, cp.stderr[-3000:]
        cls.outside = os.path.join(cls.tmp, "outside")
        os.makedirs(cls.outside)
        # A clean venv must not see the checkout through PYTHONPATH.
        cls.env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_installed_console_script_is_the_same_runtime(self):
        installed = _manifest([self.console], cwd=self.outside, env=self.env)
        source = _manifest(ENTRY_POINTS["source-script"])
        for key in PARITY_KEYS:
            self.assertEqual(installed[key], source[key], key)
        self.assertIn("site-packages", installed["module_file"].replace("\\", "/"))
        missing = [k for k, v in installed["modules"].items() if not v["importable"]]
        self.assertEqual(missing, [], "wheel omits runtime modules: %s" % installed["modules"])

    def test_installed_python_m_matches_console_script(self):
        a = _manifest([self.console], cwd=self.outside, env=self.env)
        b = _manifest([self.vpy, "-m", "flexfactor"], cwd=self.outside, env=self.env)
        for key in PARITY_KEYS:
            self.assertEqual(a[key], b[key], key)

    def test_every_mode_help_works_from_the_installed_artifact(self):
        for mode in MODES:
            cp = subprocess.run([self.console, mode, "--help"], cwd=self.outside,
                                env=self.env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(cp.returncode, 0, (mode, cp.stderr[-1500:]))

    def test_the_error_ledger_loads_from_the_wheel(self):
        # The owner-facing half of the check above: the ledger and its readers
        # (which the dashboards use) must work in an installed FlexFactor.
        cp = subprocess.run(
            [self.vpy, "-c", "import flexfactor_errors as e; "
                             "print(len(e.SIGNATURES), e.headline([]))"],
            cwd=self.outside, env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stderr[-1500:])
        self.assertIn("no errors recorded", cp.stdout)

    def test_prodready_engine_loads_from_the_wheel(self):
        cp = subprocess.run([self.vpy, "-c",
                             "import flexfactor_prodready as p; print(len(p._DETECTORS))"],
                            cwd=self.outside, env=self.env, capture_output=True,
                            text=True, timeout=300)
        self.assertEqual(cp.returncode, 0, cp.stderr[-1500:])
        self.assertGreater(int(cp.stdout.strip()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
