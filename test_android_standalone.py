#!/usr/bin/env python3
"""Repository-level invariants for the independent Android app."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
ANDROID = ROOT / "android" / "app" / "src" / "main"


class StandaloneAndroidInvariants(unittest.TestCase):
    def test_launcher_declares_no_termux_runtime_permission(self):
        manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.assertNotIn("com.termux", manifest.lower())
        self.assertNotIn("RUN_COMMAND", manifest)

    def test_activity_exposes_every_original_mode(self):
        activity = (ANDROID / "java" / "com" / "firer" / "console" /
                    "flexfactor" / "MainActivity.java").read_text(encoding="utf-8")
        for label in (
            "1 · Refactor a file",
            "2 · Scout improvements",
            "3 · Audit and repair",
            "4 · Make production ready",
        ):
            self.assertIn(label, activity)

    def test_android_network_policy_is_https_only(self):
        policy = (ANDROID / "res" / "xml" /
                  "network_security_config.xml").read_text(encoding="utf-8")
        self.assertIn('cleartextTrafficPermitted="false"', policy)
        self.assertNotIn('cleartextTrafficPermitted="true"', policy)
        self.assertNotIn("localhost", policy)
        self.assertNotIn("127.0.0.1", policy)

    def test_activity_has_no_loopback_or_shell_engine(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ANDROID / "java" / "com" / "firer" / "console" /
                         "flexfactor").glob("*.java")
        )
        for forbidden in ("127.0.0.1", "localhost:8765", "EngineRecoveryScript",
                          "TERMUX_PACKAGE", "RUN_COMMAND_STDIN"):
            self.assertNotIn(forbidden, source)

    def test_mobile_workflow_is_present_and_requires_protected_secrets(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "mobile-run.yml").read_text(encoding="utf-8")
        for mode in ("refactor", "scout", "audit", "prodready"):
            self.assertIn(mode, workflow)
        self.assertIn("secrets.FLEXFACTOR_MOBILE_GITHUB_TOKEN", workflow)
        self.assertIn("secrets.FLEXFACTOR_MOBILE_GITHUB_TOKEN || github.token", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("secrets.OPENAI_API_KEY", workflow)
        self.assertIn("qwen2.5-coder:7b", workflow)
        self.assertIn('--judge-model "$FLEXFACTOR_PHONE_MODEL"', workflow)
        self.assertIn("ollama serve", workflow)
        self.assertIn("88e0d36bd90121595e5516c84f6ab61b546368fbd2d825b4aae70999c949649d", workflow)
        self.assertIn('--provider "$PROVIDER"', workflow)
        self.assertNotIn("${{ inputs.github_token }}", workflow)
        self.assertNotIn("${{ inputs.openai", workflow.lower())
        self.assertNotIn("inputs.target_repository }} ·", workflow)
        self.assertNotIn("find target -type f -name '*.bak'", workflow)
        self.assertIn("args+=(--apply --yes)", workflow)
        self.assertIn("target/*_repo_rewards_report.md", workflow)
        self.assertIn("target/*_audit_report.md", workflow)
        self.assertIn("target/*_readiness.md", workflow)
        summary = workflow.split("- name: Write the phone-readable run summary", 1)[1]
        summary = summary.split("- name: Upload exact-run result", 1)[0]
        self.assertNotIn("${{ inputs.", summary)

    def test_repository_picker_paginates_and_rejects_private_targets(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        self.assertIn('per_page=100&page=" + page', api)
        self.assertIn('row.optBoolean("private", true)', api)

    def test_credential_switch_validates_before_write_and_removes_old_openai_secret(self):
        api = (ANDROID / "java" / "com" / "firer" / "console" /
               "flexfactor" / "GitHubApi.java").read_text(encoding="utf-8")
        configure = api.split("ConfigurationResult configure", 1)[1]
        configure = configure.split("List<Repository> repositories", 1)[0]
        self.assertLess(configure.index("verifyOpenAi(provider)"),
                        configure.index('putRepositorySecret(token, "FLEXFACTOR_MOBILE_GITHUB_TOKEN"'))
        self.assertIn('deleteRepositorySecretIfPresent(token, "OPENAI_API_KEY")', configure)
        self.assertIn("result.status != 204 && result.status != 404", api)

    def test_android_release_gate_proves_the_default_hosted_provider(self):
        workflow = (ROOT / ".github" / "workflows" /
                    "android-client.yml").read_text(encoding="utf-8")
        self.assertIn("qwen2.5-coder:7b", workflow)
        self.assertIn("ollama serve", workflow)
        self.assertIn("sha256sum --check --strict", workflow)
        self.assertIn("FLEXFACTOR_READY", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
