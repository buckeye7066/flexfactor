from __future__ import annotations

import unittest

import ci_skip_policy as policy


class SkipPolicyTests(unittest.TestCase):
    def test_known_platform_skip_is_allowed(self):
        text = ("test_x (T.test_x) ... skipped "
                "'POSIX openat component-walk unavailable on this platform'")
        self.assertEqual(policy.verify("Windows", text), [])

    def test_unittest_escaped_windows_catalog_path_is_allowed(self):
        text = ("setUpClass (T) ... skipped "
                r"'no live catalog at C:\\Users\\runneradmin\\AppData\\Local\\AITime\\routes.json'")
        self.assertEqual(policy.verify("Windows", text), [])

    def test_posix_race_schedule_is_an_explained_windows_skip(self):
        text = (
            "test_failed_create (T.test_failed_create) ... skipped "
            "'the replacement schedule is POSIX-specific'"
        )
        self.assertEqual(policy.verify("Windows", text), [])

    def test_capability_gain_may_remove_an_allowed_skip(self):
        self.assertEqual(policy.verify("Linux", "Ran 1 test\nOK"), [])

    def test_unknown_skip_fails(self):
        got = policy.verify("Linux", "test_x ... skipped 'network flaky today'")
        self.assertEqual(got, ["unapproved skip: network flaky today"])

    def test_cgroup_cases_are_opposite_platform_only(self):
        line = "test_cgroup ... skipped 'BLOCKED: Linux cgroup-v2 tests'"
        self.assertEqual(policy.verify("Windows", "\n".join([line] * 5)), [])
        self.assertTrue(policy.verify("Windows", "\n".join([line] * 6)))
        self.assertTrue(policy.verify("Linux", line))
        self.assertTrue(policy.verify("Linux", "test_cgroup ... skipped "
                        "'BLOCKED: opt-in delegated cgroup root and bwrap required'"))

    def test_duplicate_skip_beyond_limit_fails(self):
        line = "test_x ... skipped 'Windows junction test'"
        got = policy.verify("Linux", line + "\n" + line)
        self.assertEqual(len(got), 1)
        self.assertIn("exceeds 1", got[0])

    def test_bubblewrap_allows_only_existing_unreachable_trust_cases(self):
        trust = "test_trust ... skipped 'BLOCKED: host HAS a sufficient OS sandbox; trust basis not chosen on this host (strongest=bwrap, platform=linux)'"
        refusal = "test_refusal ... skipped 'BLOCKED: host HAS a sufficient OS sandbox; refusal path unreachable on this host (strongest=bwrap, platform=linux)'"
        self.assertEqual(policy.verify("Linux", "\n".join([trust, trust, refusal])), [])
        self.assertTrue(policy.verify("Linux", "\n".join([trust] * 3)))
        self.assertTrue(policy.verify("Linux", "\n".join([refusal] * 2)))
        self.assertTrue(policy.verify("Windows", trust))

    def test_unknown_runner_fails_closed(self):
        self.assertEqual(policy.verify("macOS", ""), ["unsupported runner OS: macOS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
