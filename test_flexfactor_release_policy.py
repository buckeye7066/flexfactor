"""Regression coverage for release language and competitor transports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import flexfactor_competitors as competitors
import flexfactor_release_policy as policy


def _encode_utf16_be(value: str) -> bytes:
    little_endian = value.encode("utf-16-le")
    return b"".join(
        little_endian[index : index + 2][::-1]
        for index in range(0, len(little_endian), 2)
    )


def _encode_utf32(value: str, byte_order: str) -> bytes:
    return b"".join(
        ord(character).to_bytes(4, byte_order) for character in value
    )


class ReleaseLanguageDecoderTests(unittest.TestCase):
    def test_every_bomless_unicode_byte_order_is_scanned(self):
        fragment = "manual " + "approval"
        encoded = {
            "utf16le": fragment.encode("utf-16-le"),
            "utf16be": _encode_utf16_be(fragment),
            "utf32le": _encode_utf32(fragment, "little"),
            "utf32be": _encode_utf32(fragment, "big"),
        }
        for label, raw in encoded.items():
            with self.subTest(label=label):
                self.assertIn("manual_gate", policy.matching_labels(raw))

    def test_wrapped_and_separated_phrase_is_detected(self):
        raw = ("manual" + "\n_-" + "approval").encode()
        self.assertIn("manual_gate", policy.matching_labels(raw))

    def test_third_person_completion_phrase_is_detected(self):
        raw = ("owner signs" + " off").encode()
        self.assertIn(
            "organizational_gate_third_person", policy.matching_labels(raw)
        )

    def test_replacement_heavy_binary_is_not_scanned_as_text(self):
        raw = (b"\xff" * 128) + ("manual " + "approval").encode()
        self.assertEqual(policy.decode_text_candidates(raw), ())
        self.assertEqual(policy.matching_labels(raw), ())

    def test_jsx_and_static_string_boundaries_are_detected(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            f"<span>{first}</span><span>{second}</span>",
            f'"{first}" + " " + "{second}"',
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "component.jsx"),
                )

    def test_phrase_substring_inside_words_is_not_detected(self):
        raw = "Assign officer duties".encode()
        self.assertEqual(policy.matching_labels(raw), ())

    def test_sparse_tracked_blob_is_read_from_the_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            tracked = root / "tracked.md"
            tracked.write_text("manual " + "approval", encoding="utf-8")
            subprocess.run(
                ["git", "-C", directory, "add", "tracked.md"], check=True
            )
            tracked.unlink()
            findings = policy.scan_repository(root)
        self.assertIn("prohibited:manual_gate:tracked.md", findings)

    def test_present_tracked_file_uses_staged_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            tracked = root / "tracked.md"
            tracked.write_text("manual " + "approval", encoding="utf-8")
            subprocess.run(
                ["git", "-C", directory, "add", "tracked.md"], check=True
            )
            tracked.write_text("ordinary workspace text", encoding="utf-8")
            findings = policy.scan_repository(root)
        self.assertIn("prohibited:manual_gate:tracked.md", findings)

    def test_empty_exact_git_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            findings = policy.scan_repository(directory)
        self.assertEqual(
            findings, ["infrastructure:exact Git index contains no files"]
        )

    def test_current_repository_passes_the_binding_policy(self):
        root = Path(__file__).resolve().parent
        self.assertEqual(policy.scan_repository(root), [])


class FirecrawlTransportTests(unittest.TestCase):
    @staticmethod
    def _fixture() -> str:
        return json.dumps(
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Documented competitor",
                            "url": "https://example.com/product",
                            "description": "official product documentation",
                        }
                    ]
                },
            }
        )

    def test_production_transport_uses_the_no_redirect_opener(self):
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "test-key"}), \
             mock.patch.object(
                 competitors,
                 "_default_firecrawl_opener",
                 return_value=self._fixture(),
             ) as safe_opener:
            hits = competitors._firecrawl(
                "competitors", 5, competitors._PRODUCTION_OPENER
            )
        self.assertTrue(hits)
        safe_opener.assert_called_once()

    def test_injected_default_transport_remains_injected(self):
        calls: list[str] = []

        def offline_transport(url, data=None, headers=None, timeout=None):
            calls.append(url)
            return self._fixture()

        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "test-key"}), \
             mock.patch.object(
                 competitors, "_default_opener", offline_transport
             ), mock.patch.object(
                 competitors,
                 "_default_firecrawl_opener",
                 side_effect=AssertionError("injected transport was bypassed"),
             ):
            hits, backend, skipped = competitors.web_search("competitors")

        self.assertTrue(hits)
        self.assertEqual(backend, "firecrawl")
        self.assertEqual(skipped, {})
        self.assertEqual(calls, ["https://api.firecrawl.dev/v2/search"])


if __name__ == "__main__":
    unittest.main()
