"""Startup regressions: schema-dropping proxies and durable early refusals."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import flexfactor as ff
import flexfactor_runstate as state


class StartupSchemaTests(unittest.TestCase):
    def test_proxy_that_discards_output_config_still_receives_schema(self):
        provider = object.__new__(ff.AnthropicProvider)
        provider.client = object()
        provider.model = "proxy-model"
        provider.meter = None
        captured = []
        system = [{"type": "text", "text": "Establish program understanding."}]

        def transport(_client, **kwargs):
            # Simulate a proxy passing only messages/system to its model.
            captured.append(kwargs)
            instruction = kwargs["system"][-1]["text"]
            schema = json.loads(instruction.split("\n", 1)[1])
            self.assertEqual(schema, ff.PROGRAM_UNDERSTANDING_SCHEMA)
            body = {key: (["README.md"] if key != "purpose" else "Send invoices")
                    for key in schema["required"]}
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))])

        with patch.object(ff, "_stream_with_deadline", side_effect=transport), \
             patch.object(ff, "_fallback_hold_active", return_value=False):
            message = provider._stream_structured(
                model="proxy-model", max_tokens=6000, system=system,
                messages=[{"role": "user", "content": "Repository evidence"}],
                fmt={"format": {"type": "json_schema", "schema": ff.PROGRAM_UNDERSTANDING_SCHEMA}})
        data = ff._validate_program_understanding_response(json.loads(message.content[0].text))
        self.assertEqual(data["primary_users"], ["README.md"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(system), 1, "cached caller instructions must not be mutated")

    def test_missing_contract_fields_are_still_rejected(self):
        with self.assertRaises(ff.StructuredOutputShapeError):
            ff._validate_program_understanding_response({"purpose": "Send invoices"})


class StartupStateTests(unittest.TestCase):
    def test_setup_refusal_finalizes_checkpoint_and_dashboard(self):
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
            checkpoint = state.new_run(root, program="demo", project_dir=root,
                                       mode="audit", policy="test", tool="test")
            progress = Mock()
            events = Mock()
            evidence = SimpleNamespace(EventLedger=Mock(return_value=events),
                                       build_repository_index=Mock(return_value={"totals": {}}))
            replacements = {
                "resolve_program_input": Mock(return_value=("demo", "")),
                "resolve_project_dir": Mock(return_value=root),
                "_acquire_audit_lock": Mock(return_value="test-lock"),
                "_release_audit_lock": Mock(), "_load_brain": Mock(return_value={}),
                "_resume_recover": Mock(return_value=(None, {}, {}, 0)),
                "_resume_checkpoint_for": Mock(return_value=checkpoint),
                "_start_error_ledger": Mock(return_value=None),
                "_evidence_module": Mock(return_value=evidence),
                "_detect_stack": Mock(return_value={"config_refused": True}),
                "_inventory_project": Mock(return_value={"total_entries": 0, "category_counts": {}}),
                "_PROGRESS": progress, "ConsoleMeter": Mock(), "_ledger": Mock(),
                "_restore_wip_if_active": Mock(), "_revoke_run_trust": Mock(),
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(ff, name, value))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            result = ff.audit_one_program(root, SimpleNamespace(max_cost=1), 1, 1, 0)
            stored = json.loads(Path(checkpoint.path).read_text(encoding="utf8"))
            self.assertIn("package.json unreadable", result["error"])
            self.assertEqual(stored["status"], "interrupted")
            self.assertEqual(stored["error"], result["error"])
            progress.update.assert_called_with(1, phase="error", done=True, error=result["error"])
            events.emit.assert_called_with("run.incomplete", complete=False, stop_reason=result["error"])


if __name__ == "__main__":
    unittest.main()
