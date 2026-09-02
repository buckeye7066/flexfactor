from __future__ import annotations

import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.models import ValidationError, VideoProject
from avatar_twin.planning import ProjectWorkflow


def sample_project(**updates):
    value = {
        "title": "Demo",
        "script": "Welcome to the studio. Create a synchronized music performance now!",
        "target_duration_s": 8,
        "aspect_ratio": "16:9",
    }
    value.update(updates)
    return VideoProject.from_dict(value)


class PlanningTests(unittest.TestCase):
    def test_plan_revise_approve_tiles_timeline(self):
        workflow = ProjectWorkflow()
        planned = workflow.plan(sample_project())
        self.assertEqual("awaiting_review", planned.status)
        self.assertGreaterEqual(len(planned.scenes), 2)
        for left, right in zip(planned.scenes, planned.scenes[1:]):
            self.assertAlmostEqual(left.end_s, right.start_s, places=3)
        revised = workflow.revise(planned, "Make it portrait, shorter, more energetic, captions on")
        self.assertEqual("9:16", revised.aspect_ratio)
        self.assertTrue(revised.captions_enabled)
        self.assertTrue(all(scene.expression == "enthusiastic" for scene in revised.scenes))
        approved = workflow.approve(revised)
        approved.validate(require_approved=True)

    def test_photo_avatar_requires_consent(self):
        project = sample_project(avatar={"kind": "photo", "image_path": "person.png"})
        with self.assertRaisesRegex(ValidationError, "consent"):
            project.validate()

    def test_photo_avatar_accepts_explicit_consent_record(self):
        project = sample_project(avatar={
            "kind": "photo", "image_path": "person.png",
            "consent": {
                "subject_name": "Test Subject", "granted": True,
                "recorded_at": "2026-09-02T00:00:00Z",
                "permitted_uses": ["avatar_video"],
            },
        })
        project.validate()

    def test_render_cannot_skip_approval(self):
        planned = ProjectWorkflow().plan(sample_project())
        with self.assertRaisesRegex(ValidationError, "approved"):
            planned.validate(require_approved=True)


if __name__ == "__main__":
    unittest.main()
