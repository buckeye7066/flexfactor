from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.library import AvatarLibrary, TemplateStore, VoiceCatalog
from avatar_twin.models import AvatarProfile, ValidationError, VideoProject
from avatar_twin.planning import HttpVideoAgentPlanner

from tests.support import approved_project, create_media


class LibraryAndAgentTests(unittest.TestCase):
    def test_voice_catalog_filters_language_accent_and_style(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voices.json"
            path.write_text(json.dumps({"voices": [
                {"id": "en_us_warm", "language": "en", "locale": "en-US", "accent": "US", "style": "warm"},
                {"id": "en_gb_news", "language": "en", "locale": "en-GB", "accent": "British", "style": "news"},
                {"id": "es_mx_warm", "language": "es", "locale": "es-MX", "accent": "Mexican", "style": "warm"},
            ]}), encoding="utf-8")
            catalog = VoiceCatalog.load(path)
            self.assertEqual(
                ["en_gb_news"],
                [voice.id for voice in catalog.search(language="en", accent="british", style="news")],
            )

    def test_avatar_library_detects_identity_asset_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            profile = approved_project().avatar
            library = AvatarLibrary(root / "library", root)
            record = library.register(profile, look_name="stage")
            self.assertEqual("stage", record["look_id"])
            self.assertEqual(profile.id, library.get(profile.id, "stage").id)
            (root / "face.ppm").write_bytes(b"changed")
            with self.assertRaisesRegex(ValidationError, "changed after consented enrollment"):
                library.get(profile.id, "stage")

    def test_template_reuses_voice_background_brand_and_layout(self):
        with TemporaryDirectory() as directory:
            project = approved_project()
            project.voice.accent = "Australian"
            project.voice.style = "friendly"
            project.background.kind = "color"
            project.background.color = "#123456"
            project.scenes[0].layout = "presenter_right"
            store = TemplateStore(Path(directory))
            store.save("friendly-demo", project)
            source = approved_project()
            applied = store.apply("friendly-demo", source)
            self.assertEqual("Australian", applied.voice.accent)
            self.assertEqual("#123456", applied.background.color)
            self.assertEqual("presenter_right", applied.scenes[0].layout)

    def test_video_agent_contract_ignores_invented_asset_paths(self):
        source = VideoProject.from_dict({
            "title": "Brief",
            "prompt": "Make an upbeat product introduction.",
        })
        planned = HttpVideoAgentPlanner._apply(source, {
            "title": "Agent plan",
            "scenes": [{
                "duration_s": 4,
                "script": "Welcome to the product.",
                "layout": "presenter_left",
                "media_path": "/etc/passwd",
            }],
        }, "plan")
        self.assertEqual("awaiting_review", planned.status)
        self.assertEqual("", planned.scenes[0].media_path)
        self.assertEqual("configured_http_video_agent", planned.metadata["planner"]["kind"])


if __name__ == "__main__":
    unittest.main()
