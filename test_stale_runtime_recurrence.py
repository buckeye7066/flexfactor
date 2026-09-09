"""Model/queue regressions retained from the stale-runtime incident.

Source update safety is covered with executable Git fixtures in
``test_source_app_update.py``. The current policy preserves local work and
requires an explicit Update choice; it never automatically stashes or resets.
"""
from __future__ import annotations

import inspect
import unittest

import flexfactor as ff
import flexfactor_rotation as rotation


class StaleRuntimeRecurrenceTests(unittest.TestCase):
    def test_program_understanding_shape_failure_still_rotates_inside_same_call(self):
        inference = inspect.getsource(ff._infer_purpose_contract)
        self.assertIn("structured_validated", inference)
        self.assertIn("validator=_validate_program_understanding_response", inference)
        provider = inspect.getsource(rotation.RotatingProvider._run)
        self.assertIn("_result_validator", provider)
        self.assertIn("result_validator(result)", provider)

    def test_current_queue_preflight_resolves_targets_before_work_when_dashboard_active(self):
        source = inspect.getsource(ff.run_audit)
        self.assertIn("resolved_targets", source)
        self.assertIn("if prompts or session_prompt or getattr(args, \"dashboard\", True)", source)
        self.assertLess(source.index("resolved_targets"), source.index("audit_one_program"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

