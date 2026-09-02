from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.live import HttpLiveAvatarProvider, LiveSessionStore
from avatar_twin.models import ValidationError


class _Response:
    status = 201

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.value).encode()


class LiveAvatarTests(unittest.TestCase):
    def test_real_provider_contract_returns_secure_bidirectional_session(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return _Response({
                "session_id": "provider-session-1",
                "join_url": "https://rooms.example/session-1",
                "expires_at": "2026-09-02T22:00:00Z",
            })

        provider = HttpLiveAvatarProvider(
            "https://live-worker.example", "secret", opener=opener)
        session = provider.create(
            avatar_id="avatar_1", context="Answer from the approved knowledge base.",
            voice_id="voice_1", language="en-US")
        self.assertEqual("ready", session.state)
        self.assertEqual("https://rooms.example/session-1", session.join_url)
        body = json.loads(requests[0][0].data)
        self.assertTrue(body["bidirectional_audio"])
        self.assertEqual("webrtc", body["transport"])
        self.assertEqual("Bearer secret", requests[0][0].headers["Authorization"])

    def test_session_store_never_persists_join_url(self):
        provider = HttpLiveAvatarProvider(
            "https://live-worker.example",
            opener=lambda *_args, **_kwargs: _Response({
                "session_id": "provider-session-2",
                "join_url": "wss://rooms.example/session-2?token=sensitive",
            }))
        session = provider.create(avatar_id="avatar_2", context="Be helpful.")
        with TemporaryDirectory() as directory:
            store = LiveSessionStore(directory)
            store.save(session)
            raw = next(Path(directory).glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn("token=sensitive", raw)
            self.assertNotIn("join_url", raw)
            self.assertEqual("provider-session-2", store.list()[0]["provider_session_id"])

    def test_provider_rejects_insecure_endpoint_or_join_url(self):
        with self.assertRaises(ValidationError):
            HttpLiveAvatarProvider("http://live-worker.example")
        provider = HttpLiveAvatarProvider(
            "https://live-worker.example",
            opener=lambda *_args, **_kwargs: _Response({
                "session_id": "provider-session-3", "join_url": "javascript:alert(1)",
            }))
        with self.assertRaisesRegex(ValidationError, "join_url"):
            provider.create(avatar_id="avatar_3", context="Be helpful.")


if __name__ == "__main__":
    unittest.main()
