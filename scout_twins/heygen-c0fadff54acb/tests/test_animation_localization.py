from __future__ import annotations

import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.animation import TimelineCompiler
from avatar_twin.localization import RuleTranslationProvider, localize_project
from avatar_twin.models import ValidationError, VideoProject
from avatar_twin.planning import ProjectWorkflow


class AnimationLocalizationTests(unittest.TestCase):
    def setUp(self):
        source = VideoProject.from_dict({
            "title": "Studio welcome",
            "script": "Welcome to the studio. Create your music performance!",
            "target_duration_s": 6,
            "brand": {"glossary": {"Avatar Studio": "Avatar Studio"}},
        })
        self.project = ProjectWorkflow().plan(source)

    def test_compiler_produces_editable_behavior_timeline(self):
        timeline = TimelineCompiler(fps=12).compile(self.project)
        self.assertTrue(timeline["words"])
        self.assertTrue(timeline["visemes"])
        self.assertTrue(timeline["motions"])
        self.assertTrue(timeline["captions"])
        self.assertEqual(len(self.project.scenes), len(timeline["scenes"]))
        self.assertLessEqual(timeline["visemes"][-1]["end_s"], timeline["duration_s"])

    def test_localization_is_reviewable_and_preserves_glossary(self):
        localized = localize_project(self.project, "es", RuleTranslationProvider())
        self.assertEqual("es", localized.language)
        self.assertEqual("awaiting_review", localized.status)
        combined = " ".join(scene.script for scene in localized.scenes)
        self.assertIn("studio", combined.lower())
        self.assertIn("bienvenido", combined.lower())
        self.assertTrue(localized.metadata["localization"]["review_required"])
        self.assertEqual("es", localized.voice.language)
        self.assertEqual("", localized.narration_audio_path)

    def test_unsupported_local_translation_fails_instead_of_faking(self):
        with self.assertRaisesRegex(ValidationError, "does not cover"):
            localize_project(self.project, "ja", RuleTranslationProvider())


if __name__ == "__main__":
    unittest.main()
