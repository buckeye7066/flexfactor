"""Subscription subprocesses cannot inherit metered provider credentials."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
from providers.cli_provider import _recursion_guard_env

class SubscriptionEnvironmentTests(unittest.TestCase):
    def test_subscription_keys_are_isolated_without_mutating_parent(self):
        original = {"OPENAI_API_KEY": "test-openai", "CODEX_API_KEY": "test-codex",
                    "ANTHROPIC_API_KEY": "test-anthropic", "ANTHROPIC_AUTH_TOKEN": "test-proxy",
                    "ANTHROPIC_BASE_URL": "https://example.invalid",
                    "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": os.environ.get("PATH", "")}
        with patch.dict(os.environ, original, clear=True):
            for api in ("codex-cli", "claude-code"):
                env = _recursion_guard_env(api)
                for name in original:
                    if name != "PATH":
                        self.assertNotIn(name, env)
                self.assertEqual(env["PATH"], original["PATH"])
                self.assertEqual(dict(os.environ), original)

    def test_mixed_case_environment_is_filtered_without_changing_parent(self):
        fixture = {"OpenAI_Api_Key": "fixture", "anthropic_api_key": "fixture",
                   "CodeX_Api_Key": "fixture", "Anthropic_Auth_Token": "fixture",
                   "openai_base_url": "https://example.invalid",
                   "Claude_Code_Use_Bedrock": "1", "anthropic_profile": "fixture",
                   "PATH": "fixture-path", "CLAUDE_CODE_OAUTH_TOKEN": "fixture-plan"}
        with patch.object(os, "environ", fixture.copy()):
            for api in ("codex-cli", "claude-code"):
                child = _recursion_guard_env(api)
                for name in fixture:
                    if name not in ("PATH", "CLAUDE_CODE_OAUTH_TOKEN"):
                        self.assertNotIn(name, child)
                self.assertEqual(child["PATH"], fixture["PATH"])
                self.assertEqual(child["CLAUDE_CODE_OAUTH_TOKEN"], "fixture-plan")
                self.assertEqual(os.environ, fixture)
            untouched = _recursion_guard_env("copilot-cli")
            for key, value in fixture.items():
                self.assertEqual(untouched[key], value)

if __name__ == "__main__":
    unittest.main()
