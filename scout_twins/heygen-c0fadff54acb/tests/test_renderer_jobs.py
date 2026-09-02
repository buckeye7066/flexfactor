from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import shutil
import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.jobs import JobStore, RenderQueue
from avatar_twin.models import ValidationError
from avatar_twin.renderer import RenderEngine

from tests.support import approved_project, create_media, fixture_config


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
class VerifiedRendererTests(unittest.TestCase):
    def test_model_backed_pipeline_creates_probed_mp4_and_receipts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            artifact = RenderEngine(
                root,
                runtime_config=fixture_config(),
                allow_test_backends=True,
            ).render(approved_project(), root / "out")
            self.assertEqual("completed_verified", artifact["status"])
            self.assertEqual("fixture", artifact["provider"])
            self.assertTrue(artifact["provider_receipts"])
            self.assertTrue(artifact["final_probe"]["has_audio"])
            self.assertGreater(artifact["final_probe"]["video_frames"], 2)
            self.assertTrue((root / "out" / "video.mp4").is_file())
            persisted = json.loads((root / "out" / "artifact.json").read_text(encoding="utf-8"))
            self.assertEqual("model-backed avatar video generated and independently probed", persisted["claim"])
            self.assertIn("<video", (root / "out" / "preview.html").read_text(encoding="utf-8"))

    def test_background_brand_layout_and_music_change_the_video_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            project = approved_project()
            project.output_resolution = "480p"
            project.background.kind = "color"
            project.background.color = "#004422"
            project.brand.primary_color = "#ff5500"
            project.brand.logo_path = "face.ppm"
            project.background_music_path = "master.wav"
            project.background_music_volume = 0.08
            project.scenes[0].layout = "presenter_right"
            project.scenes[0].media_path = "face.ppm"
            project.scenes[0].media_position = "left"
            project.scenes[0].media_scale = 0.2
            project.scenes[0].title_text = "Verified scene title"
            project.scenes[0].title_position = "top"
            artifact = RenderEngine(
                root,
                runtime_config=fixture_config(),
                allow_test_backends=True,
            ).render(project, root / "out")
            self.assertEqual("color", artifact["composition"]["background"]["kind"])
            self.assertEqual("presenter_right", artifact["composition"]["layouts"][0]["layout"])
            self.assertEqual(854, artifact["final_probe"]["width"])
            self.assertEqual(480, artifact["final_probe"]["height"])
            self.assertIsNotNone(artifact["assembly"]["background_music_sha256"])
            compose_argv = artifact["composition"]["receipts"][0]["argv"]
            self.assertTrue(any("0x004422" in item for item in compose_argv))
            filters = compose_argv[compose_argv.index("-filter_complex") + 1]
            self.assertIn("scene_media", filters)
            self.assertIn("drawtext", filters)

    def test_test_backend_is_rejected_by_production_renderer(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            with self.assertRaisesRegex(ValidationError, "test-only"):
                RenderEngine(root, runtime_config=fixture_config()).render(
                    approved_project(), root / "out"
                )

    def test_missing_runtime_fails_instead_of_generating_cartoon_or_tones(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            with self.assertRaisesRegex(ValidationError, "no avatar runtime"):
                RenderEngine(root).render(approved_project(), root / "out")
            self.assertFalse((root / "out" / "video.mp4").exists())

    def test_static_video_cannot_claim_avatar_animation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            with self.assertRaisesRegex(ValidationError, "temporal visual change"):
                RenderEngine(
                    root,
                    runtime_config=fixture_config(static=True),
                    allow_test_backends=True,
                ).render(approved_project(), root / "out")

    def test_persisted_job_completes_only_after_artifact_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            create_media(root)
            store = JobStore(root / "workspace")
            queue = RenderQueue(
                store,
                RenderEngine(root, runtime_config=fixture_config(), allow_test_backends=True),
                workers=1,
            )
            try:
                submitted = queue.submit(approved_project())
                final = queue.wait(submitted.id, timeout=30)
                self.assertEqual("completed", final.state, final.error)
                self.assertEqual("completed_verified", final.artifact["status"])
                self.assertTrue(Path(final.artifact["video_uri"]).is_file())
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
