from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from live_cursor_aitime_proof import LiveProofFailure, run_live_proof


class _CursorHandler(BaseHTTPRequestHandler):
    token = "cursor-live-unit-secret"
    allow_unauthenticated = False
    wrong_sentinel = False

    def log_message(self, *_args):
        return

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size) or b"{}")
        authorized = self.headers.get("Authorization") == "Bearer " + self.token
        if not authorized and not self.allow_unauthenticated:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"authentication required"}')
            return
        prompt = payload.get("messages", [{}])[-1].get("content", "")
        match = re.search(r"FLEXFACTOR_LIVE_OK:[0-9a-z]+", prompt)
        content = match.group(0) if match else "unexpected"
        if self.wrong_sentinel and authorized:
            content = "wrong"
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LiveCursorAITimeProofTests(unittest.TestCase):
    def setUp(self):
        _CursorHandler.allow_unauthenticated = False
        _CursorHandler.wrong_sentinel = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CursorHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = Path(self.tmp.name) / "routes.json"
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self._write_catalog()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _write_catalog(self, *, api="cursor", base_url=None, generated_at=None):
        self.catalog.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
                    "routes": [
                        {
                            "id": "cursor-live/cursor-small",
                            "backend": "cursor",
                            "api": api,
                            "base_url": base_url or self.base_url,
                            "model": "cursor-small",
                            "wire_model": "cursor-small",
                            "pool": "cursor:subscription",
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _env(self):
        return {
            "AI_ROTATE_CATALOG": str(self.catalog),
            "FLEXFACTOR_CURSOR_BASE_URL": self.base_url,
            "FLEXFACTOR_CURSOR_API_KEY": _CursorHandler.token,
            "FLEXFACTOR_CURSOR_ROUTE_ID": "cursor-live/cursor-small",
        }

    def test_rejects_unauth_then_proves_authenticated_exact_inference(self):
        result = run_live_proof(self._env(), nonce="unitnonce")
        self.assertTrue(result["live"])
        self.assertEqual(result["authentication"]["unauthenticated_status"], 401)
        self.assertEqual(result["authentication"]["authenticated_status"], 200)
        self.assertTrue(result["authentication"]["bearer_boundary_proven"])
        self.assertTrue(result["inference"]["exact_sentinel"])
        rendered = json.dumps(result)
        self.assertNotIn(_CursorHandler.token, rendered)
        self.assertNotIn(self.base_url, rendered)

    def test_missing_authentication_fails_before_inference(self):
        env = self._env()
        env.pop("FLEXFACTOR_CURSOR_API_KEY")
        with self.assertRaisesRegex(LiveProofFailure, "FLEXFACTOR_CURSOR_API_KEY"):
            run_live_proof(env, nonce="unitnonce")

    def test_endpoint_that_allows_unauthenticated_inference_fails(self):
        _CursorHandler.allow_unauthenticated = True
        with self.assertRaisesRegex(LiveProofFailure, "authentication boundary not proven"):
            run_live_proof(self._env(), nonce="unitnonce")

    def test_wrong_authenticated_completion_fails(self):
        _CursorHandler.wrong_sentinel = True
        with self.assertRaisesRegex(LiveProofFailure, "exact sentinel"):
            run_live_proof(self._env(), nonce="unitnonce")

    def test_route_and_endpoint_must_match(self):
        self._write_catalog(base_url="https://different.invalid/v1")
        with self.assertRaisesRegex(LiveProofFailure, "does not match"):
            run_live_proof(self._env(), nonce="unitnonce")

    def test_remote_plain_http_endpoint_is_rejected(self):
        env = self._env()
        env["FLEXFACTOR_CURSOR_BASE_URL"] = "http://cursor.example/v1"
        with self.assertRaisesRegex(LiveProofFailure, "must use HTTPS"):
            run_live_proof(env, nonce="unitnonce")

    def test_stale_catalog_fails(self):
        old = datetime.fromtimestamp(time.time() - 90_000, timezone.utc).isoformat()
        self._write_catalog(generated_at=old)
        with self.assertRaisesRegex(LiveProofFailure, "catalog is stale"):
            run_live_proof(self._env(), nonce="unitnonce")


if __name__ == "__main__":
    unittest.main(verbosity=2)
