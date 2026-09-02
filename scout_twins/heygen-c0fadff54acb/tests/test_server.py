from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import ProxyHandler, Request, build_opener
import json
import shutil
import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.server import AvatarStudioApp, make_handler

from tests.support import create_media, fixture_config


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
class ServerTests(unittest.TestCase):
    def test_upload_plan_approve_and_verified_async_render(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = create_media(root)
            app = AvatarStudioApp(
                root / "workspace",
                root,
                runtime_config=fixture_config(),
                allow_test_backends=True,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            # The isolated Scout verifier intentionally installs a dead outbound
            # proxy.  This test talks only to its own loopback server, so bypass
            # ambient proxy settings explicitly instead of weakening isolation.
            opener = build_opener(ProxyHandler({}))

            def get(path):
                with opener.open(base + path, timeout=20) as response:
                    return response.status, json.loads(response.read())

            def post(path, value):
                request = Request(
                    base + path,
                    data=json.dumps(value).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with opener.open(request, timeout=20) as response:
                    return response.status, json.loads(response.read())

            def upload(path: Path, kind: str, content_type: str):
                request = Request(
                    base + f"/api/assets?kind={kind}",
                    data=path.read_bytes(),
                    method="POST",
                    headers={"Content-Type": content_type, "X-Filename": path.name},
                )
                with opener.open(request, timeout=20) as response:
                    return json.loads(response.read())

            try:
                _, health = get("/health")
                self.assertTrue(health["runtime"]["ready"], health)
                avatar = upload(media["face"], "avatar_image", "image/x-portable-pixmap")
                audio = upload(media["audio"], "audio", "audio/wav")
                project = {
                    "title": "API verified demo",
                    "script": "Welcome to the avatar studio.",
                    "target_duration_s": 2,
                    "output_resolution": "480p",
                    "avatar": {
                        "kind": "photo",
                        "image_path": avatar["path"],
                        "style": "talking_head",
                        "consent": {
                            "granted": True,
                            "subject_name": "Fixture Subject",
                            "recorded_at": "2026-09-02T00:00:00Z",
                            "permitted_uses": ["avatar_video"],
                        },
                    },
                    "narration_audio_path": audio["path"],
                }
                _, planned = post("/api/plan", {"project": project})
                _, approved = post("/api/approve", {"project": planned})
                status, job = post("/api/jobs", {"project": approved})
                self.assertEqual(202, status)
                final = app.queue.wait(job["id"], timeout=30)
                self.assertEqual("completed", final.state, final.error)
                self.assertEqual("completed_verified", final.artifact["status"])
                with opener.open(base + f"/outputs/{job['id']}/preview.html", timeout=20) as response:
                    self.assertIn(b"<video", response.read())
                with opener.open(base + f"/outputs/{job['id']}/video.mp4", timeout=20) as response:
                    self.assertGreater(int(response.headers["Content-Length"]), 1000)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                app.close()


if __name__ == "__main__":
    unittest.main()
