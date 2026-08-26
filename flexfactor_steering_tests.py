"""Executable contract for authenticated operator steering."""
import os
import tempfile
import unittest
import flexfactor_steering as fs

class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.project = os.path.join(self.root, "Target App")
        os.makedirs(self.project)
    def tearDown(self):
        self.tmp.cleanup()
    def test_submit_claim_context_and_complete(self):
        item = fs.submit("Target", self.project, "Add a printable report and test the download.", root=self.root)
        self.assertEqual("pending", item["status"])
        context, active, new = fs.refresh_context("PURPOSE", "Target", self.project, "run-1", root=self.root)
        self.assertEqual([item["id"]], active)
        self.assertEqual(active, new)
        self.assertIn("printable report", context)
        context2, active2, new2 = fs.refresh_context(context, "Target", self.project, "run-1", root=self.root)
        self.assertEqual(active, active2)
        self.assertEqual([], new2)
        self.assertEqual(1, context2.count(item["id"]))
        fs.finish("Target", self.project, "run-1", active, completed=True, detail="verified", root=self.root)
        self.assertEqual("completed", fs.list_comments("Target", self.project, root=self.root)[0]["status"])
        context3, active3, _ = fs.refresh_context(context2, "Target", self.project, "run-2", root=self.root)
        self.assertEqual([], active3)
        self.assertNotIn(fs._BEGIN, context3)
    def test_interrupted_run_comment_is_reclaimed(self):
        item = fs.submit("Target", self.project, "Keep the existing login.", root=self.root)
        _, ids1, _ = fs.refresh_context("", "Target", self.project, "dead-run", root=self.root)
        _, ids2, new2 = fs.refresh_context("", "Target", self.project, "replacement-run", root=self.root)
        self.assertEqual(ids1, ids2)
        self.assertEqual([item["id"]], new2)
    def test_project_scope_is_exact(self):
        fs.submit("Target", self.project, "Only app one", root=self.root)
        other = os.path.join(self.root, "Other")
        os.makedirs(other)
        self.assertEqual([], fs.list_comments("Target", other, root=self.root))
        self.assertEqual([], fs.list_comments("Other", self.project, root=self.root))
    def test_validation(self):
        for value in ("", "x" * (fs.MAX_COMMENT_CHARS + 1), "bad\x00text"):
            with self.assertRaises(ValueError):
                fs.submit("Target", self.project, value, root=self.root)
    def test_context_replaces_prior_snapshot(self):
        combined = fs.merge_context("BASE", fs.steering_block([{"id": "one", "comment": "first"}]))
        refreshed = fs.merge_context(combined, fs.steering_block([{"id": "two", "comment": "second"}]))
        self.assertIn("BASE", refreshed)
        self.assertNotIn("[one]", refreshed)
        self.assertEqual(1, refreshed.count(fs._BEGIN))
        self.assertIn("[two]", refreshed)

if __name__ == "__main__":
    unittest.main()
