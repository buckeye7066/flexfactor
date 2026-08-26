from __future__ import annotations

import unittest

import ci_skip_policy as policy


class SkipPolicyTests(unittest.TestCase):
    def test_known_platform_skip_is_allowed(self):
        text = ("test_x (T.test_x) ... skipped "
                "'POSIX openat component-walk unavailable on this platform'")
        self.assertEqual(policy.verify("Windows", text), [])

    def test_capability_gain_may_remove_an_allowed_skip(self):
        self.assertEqual(policy.verify("Linux", "Ran 1 test\nOK"), [])

    def test_unknown_skip_fails(self):
        got = policy.verify("Linux", "test_x ... skipped 'network flaky today'")
        self.assertEqual(got, ["unapproved skip: network flaky today"])

    def test_duplicate_skip_beyond_limit_fails(self):
        line = "test_x ... skipped 'Windows junction test'"
        got = policy.verify("Linux", line + "\n" + line)
        self.assertEqual(len(got), 1)
        self.assertIn("exceeds 1", got[0])

    def test_unknown_runner_fails_closed(self):
        self.assertEqual(policy.verify("macOS", ""), ["unsupported runner OS: macOS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
