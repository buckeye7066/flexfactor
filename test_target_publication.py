"""Behavioral proof for FlexFactor target-repository publication."""
from __future__ import annotations

import inspect
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import flexfactor as ff


def _run(*argv: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _quality_rows(overrides: dict[str, str] | None = None) -> dict:
    states = {
        "tests": "pass",
        "secrets": "pass",
        "inventory": "pass",
        "rescan": "pass",
        "blast-radius": "pass",
        "independent-final-review": "pass",
        # Whole-product completeness gates intentionally remain open.
        "function-coverage": "fail",
        "behavior": "blocked",
        "build": "fail",  # a repaired red baseline; final build is rerun at push
    }
    states.update(overrides or {})
    return {
        "gates": [
            {"id": gate_id, "status": status}
            for gate_id, status in states.items()
        ]
    }


def _product_rows(overrides: dict[str, str] | None = None) -> dict:
    states = {
        "no-blind-competitor-copying": "pass",
        "selected-capabilities-delivered": "pass",
        # Remaining purpose work must not strand a safe repair.
        "purpose-fulfilled": "fail",
        "competitive-coverage": "blocked",
    }
    states.update(overrides or {})
    return {
        "ready": False,
        "gates": [
            {"id": gate_id, "status": status}
            for gate_id, status in states.items()
        ],
    }


class IncrementalPublicationSafetyTests(unittest.TestCase):
    def test_completeness_gaps_do_not_strand_a_safe_verified_edit(self):
        ready, reason = ff._publication_safety_ready(
            _quality_rows(), _product_rows()
        )
        self.assertTrue(ready, reason)
        self.assertIn("completeness gaps may remain", reason)

    def test_every_candidate_safety_gate_is_fail_closed(self):
        cases = [
            (_quality_rows({"tests": "fail"}), _product_rows(), "tests"),
            (_quality_rows({"secrets": "blocked"}), _product_rows(), "secrets"),
            (_quality_rows(), _product_rows({
                "no-blind-competitor-copying": "fail"
            }), "no-blind-competitor-copying"),
            (_quality_rows(), {"gates": []}, "selected-capabilities-delivered"),
        ]
        for quality, product, expected in cases:
            with self.subTest(expected=expected):
                ready, reason = ff._publication_safety_ready(quality, product)
                self.assertFalse(ready)
                self.assertIn(expected, reason)

    def test_audit_finalization_uses_candidate_safety_not_full_convergence(self):
        source = inspect.getsource(ff.audit_one_program)
        finalization = source.split("# FINAL PUBLICATION GATE.", 1)[1]
        finalization = finalization.split("if evidence is not None:", 1)[0]
        self.assertIn("_publication_safety_ready(", finalization)
        self.assertIn("prepublication_complete = publication_safe", finalization)
        self.assertNotIn("prepublication_complete = bool(\n            converged",
                         finalization)

    def test_exact_verified_commit_is_really_pushed_to_remote_main(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin.git"
            target = root / "target"
            _run("git", "init", "--bare", "-q", "-b", "main", str(origin))
            _run("git", "init", "-q", "-b", "main", str(target))
            _run("git", "config", "user.name", "FlexFactor Test", cwd=target)
            _run("git", "config", "user.email", "test@example.invalid", cwd=target)
            (target / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            _run("git", "add", "app.py", cwd=target)
            _run("git", "commit", "-qm", "baseline", cwd=target)
            _run("git", "remote", "add", "origin", str(origin), cwd=target)
            _run("git", "push", "-q", "-u", "origin", "main", cwd=target)

            (target / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            _run("git", "add", "app.py", cwd=target)
            _run("git", "commit", "-qm", "verified repair", cwd=target)
            candidate = _run("git", "rev-parse", "HEAD", cwd=target)

            args = types.SimpleNamespace(push=True, merge=True)
            with mock.patch.object(
                ff, "_publication_gate", return_value=(True, "green")
            ), mock.patch.object(
                ff, "_wip_publish_guard", return_value=(True, "")
            ):
                result = ff._publish_verified_head(
                    str(target), "main", args, {}, candidate
                )

            self.assertTrue(result["complete"], result)
            self.assertEqual("main", result["default_branch"])
            self.assertEqual(
                candidate,
                _run("git", "--git-dir", str(origin), "rev-parse", "main"),
            )


if __name__ == "__main__":
    unittest.main()
