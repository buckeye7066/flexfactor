"""Regression coverage for release language and competitor transports."""
from __future__ import annotations

import codecs
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

    def test_known_source_retains_malformed_utf8_candidate(self):
        raw = ("manual " + "approval").encode() + (b"\xff" * 4)
        self.assertIn(
            "manual_gate",
            policy.matching_labels(raw, "page.html"),
        )

    def test_lightly_malformed_bom_text_is_still_scanned(self):
        fragment = "manual " + "approval"
        prefix = "ordinary text " * 20
        encoded = {
            "utf8": codecs.BOM_UTF8 + prefix.encode() + b"\xff" + fragment.encode(),
            "utf16le": (
                codecs.BOM_UTF16_LE
                + prefix.encode("utf-16-le")
                + b"\x00\xd8"
                + fragment.encode("utf-16-le")
            ),
            "utf32le": (
                codecs.BOM_UTF32_LE
                + prefix.encode("utf-32-le")
                + b"\x00\xd8\x00\x00"
                + fragment.encode("utf-32-le")
            ),
        }
        for label, raw in encoded.items():
            with self.subTest(label=label):
                self.assertIn("manual_gate", policy.matching_labels(raw))

    def test_lightly_malformed_bomless_unicode_is_still_scanned(self):
        fragment = "manual " + "approval"
        prefix = "ordinary text " * 20
        encoded = {
            "utf16le": (
                prefix.encode("utf-16-le")
                + b"\x00\xd8"
                + fragment.encode("utf-16-le")
            ),
            "utf32le": (
                prefix.encode("utf-32-le")
                + b"\x00\xd8\x00\x00"
                + fragment.encode("utf-32-le")
            ),
        }
        for label, raw in encoded.items():
            with self.subTest(label=label):
                self.assertIn("manual_gate", policy.matching_labels(raw))

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

    def test_html_character_references_are_decoded_before_matching(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            f"{first}&#32;{second}",
            f"<span>{first}</span>&nbsp;<span>{second}</span>",
            f"<span>{first}&nbsp{second}</span>",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "page.html"),
                )

    def test_jsx_whitespace_and_markdown_inline_html_are_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (f"<span>{first}</span>{{' '}}<span>{second}</span>", "view.jsx"),
            (f"<span>{first}</span> <span>{second}</span>", "README.md"),
            (f"{first} **{second}**", "README.md"),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path, source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), relative_path),
                )

    def test_comments_and_unicode_escapes_cannot_split_rendered_copy(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (f"<p>{first}<!-- split --> {second}</p>", "page.html"),
            (
                f"<span>{first} </span>{{/* note */}}<span>{second}</span>",
                "view.jsx",
            ),
            (f'"{first}" + "\\u0020" + "{second}"', "copy.js"),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), relative_path),
                )

    def test_quoted_javascript_comment_does_not_break_literal_chain(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        source = f'"{first} " + /* "decoy" */ "{second}"'
        self.assertIn(
            "manual_gate",
            policy.matching_labels(source.encode(), "copy.js"),
        )

    def test_plain_javascript_comparison_is_not_treated_as_markup(self):
        first = "".join(map(chr, (115, 105, 103, 110)))
        second = "".join(map(chr, (111, 102, 102)))
        source = f"{first} < threshold > {second}"
        self.assertEqual(policy.matching_labels(source.encode(), "logic.js"), ())

    def test_constant_templates_and_json_escapes_are_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            ("`" + first + ' ${""}' + second + "`", "copy.js"),
            (
                "`"
                + first
                + ' ${/* split */ " " /* split */ + ""}'
                + second
                + "`",
                "copy.js",
            ),
            (
                "`"
                + first
                + ' ${((/* split */ " " + ""))}'
                + second
                + "`",
                "copy.js",
            ),
            (
                "`"
                + first
                + ' ${" " /* "hidden" */ + ""}'
                + second
                + "`",
                "copy.js",
            ),
            (
                "`" + first + ' ${(``)}' + second + "`",
                "copy.js",
            ),
            (
                f'"{first} " + ("{second}")',
                "copy.js",
            ),
            (f'{{"copy":"{first}\\u0020{second}"}}', "copy.json"),
            (f'"\\u{{000006d}}{first[1:]} {second}"', "copy.js"),
            (f'"{first[:1]}\\{first[1:]} {second}"', "copy.js"),
            (
                f'"{first}\\{chr(0x2028)} {second}"',
                "copy.js",
            ),
            (
                f'"{first}" /* split */ + " " /* split */ + "{second}"',
                "copy.js",
            ),
            (
                '"\\155' + first[1:] + " " + second + '"',
                "copy.js",
            ),
            (
                f'"{first}" // split{chr(0x2028)} + " " + "{second}"',
                "copy.js",
            ),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), relative_path),
                )

        dynamic_template = "`" + first + " ${separator}" + second + "`"
        self.assertNotIn(
            "manual_gate",
            policy.matching_labels(dynamic_template.encode(), "copy.js"),
        )

        compact = "".join(map(chr, (115, 105, 103, 110, 111, 102, 102)))
        preserved_backslash = (
            "`"
            + compact[:2]
            + '${"\\\\'
            + compact[2]
            + '"}'
            + compact[3:]
            + "`"
        )
        self.assertNotIn(
            "organizational_gate_compact",
            policy.matching_labels(preserved_backslash.encode(), "copy.js"),
        )

    def test_nfkc_and_default_ignorables_are_normalized(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        fullwidth_first = "".join(
            chr(ord(character) + 0xFEE0) for character in first
        )
        fullwidth_second = "".join(
            chr(ord(character) + 0xFEE0) for character in second
        )
        sources = (
            first[:4] + chr(0xFE0F) + first[4:] + " " + second,
            fullwidth_first + " " + fullwidth_second,
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "copy.html"),
                )

    def test_markdown_references_and_css_generated_content_are_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (
                f"{first} [{second}][policy]\n\n"
                "[policy]: https://example.test/policy",
                "README.md",
            ),
            (
                f'.status::after {{ content: "{first}\\20 {second}"; }}',
                "style.css",
            ),
            (f"{first}\\\n{second}", "README.md"),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), relative_path),
                )

    def test_css_terminators_inside_strings_do_not_truncate_content(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        for embedded in (";", "}"):
            source = (
                f'.status::after {{ content: "x{embedded}{first}\\20 '
                f'{second}"; }}'
            )
            with self.subTest(embedded=embedded):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "style.css"),
                )

    def test_markdown_reference_identity_preserves_punctuation(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        unresolved = (
            f"{first} [{second}][foo_bar]\n\n"
            "[foo-bar]: https://example.test"
        )
        resolved = (
            f"{first} [{second}][foo_bar]\n\n"
            "[foo_bar]: https://example.test"
        )
        self.assertNotIn(
            "manual_gate",
            policy.matching_labels(unresolved.encode(), "README.md"),
        )
        self.assertIn(
            "manual_gate",
            policy.matching_labels(resolved.encode(), "README.md"),
        )

    def test_projection_keeps_distinct_rendered_branches_separate(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        compact = "".join(map(chr, (115, 105, 103, 110, 111, 102, 102)))
        sources = (
            (
                f'<div data-policy="{first}&#32;{second}"></div>',
                "page.html",
            ),
            (
                f'<div aria-label="{first}&nbsp{second}"></div>',
                "page.html",
            ),
            (
                "`<span>"
                + compact[:4]
                + "</span><span>"
                + compact[4:]
                + "</span>`",
                "README.md",
            ),
            (
                f'.status::after {{ content: "{compact[:4]}" / "{compact[4:]}"; }}',
                "style.css",
            ),
            (
                f'.status::after {{ content: "{first}/*note*/ {second}"; }}',
                "style.css",
            ),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path):
                self.assertEqual(
                    policy.matching_labels(source.encode(), relative_path),
                    (),
                )

    def test_cross_format_runtime_copy_is_projected(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (f'<p>{{"{first} "}}{second}</p>', "view.jsx"),
            (
                f'<input type="button" value="{first}&#32;{second}">',
                "page.html",
            ),
            (
                '<button onclick="this.textContent='
                f"'{first}'+' {second}'"
                '">Go</button>',
                "page.html",
            ),
            (
                '.x::after { content: url("icon.svg") / '
                f'"{first}\\20 {second}"; }}',
                "style.css",
            ),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path, source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), relative_path),
                )

    def test_non_rendering_runtime_syntax_stays_distinct(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (
                f'const pattern = /"{first} " + "{second}"/;',
                "copy.js",
            ),
            (
                f'.x::after {{ content: url("{first}\\20 {second}"); }}',
                "style.css",
            ),
            (
                f"<textarea>{first}<span> {second}</textarea>",
                "page.html",
            ),
            (f"`{first}&#32;{second}`", "README.md"),
            (
                f"const value = String.raw`{first}\\u0020{second}`;",
                "copy.js",
            ),
            (
                f'<input type="text" value="{first}&#32;{second}">',
                "page.html",
            ),
            (
                f'function copy() {{ "{first} " }}\n{second}();',
                "view.tsx",
            ),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path, source=source):
                self.assertEqual(
                    policy.matching_labels(source.encode(), relative_path),
                    (),
                )

    def test_executable_inline_script_copy_is_projected(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (
                '<span id="copy"></span><script>'
                'document.querySelector("#copy").textContent = '
                f'"{first}" + "\\u0020{second}";'
                "</script>"
            ),
            (
                '<script type="text&#47;javascript">'
                f'out.textContent = "{first}" + " {second}";'
                "</script>"
            ),
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "page.html"),
                )

    def test_attributes_embedded_css_and_python_literals_are_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (
                f'<input aria-label="{first}&#32;{second}">',
                "page.html",
            ),
            (
                f'<style>.status::after {{ content: "{first}\\20 {second}"; }}</style>',
                "page.html",
            ),
            (
                f'message = ("{first} " "{second}")',
                "messages.py",
            ),
            (
                f'message = ("{first} "\n# static copy\n"{second}")',
                "messages.py",
            ),
            (
                f'message = f"{first}\\x20{second}"',
                "messages.py",
            ),
            (
                f'message = "{first} " + "{second}"',
                "messages.py",
            ),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), relative_path),
                )

    def test_constant_f_string_replacement_field_is_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        source = f'message = f"{first}' + "{' '}" + f'{second}"'
        self.assertIn(
            "manual_gate",
            policy.matching_labels(source.encode(), "messages.py"),
        )

    def test_grouped_jsx_static_whitespace_is_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            f'<span>{first}</span>{{" " + ""}}<span>{second}</span>',
            f'<span>{first}</span>{{((" " + ""))}}<span>{second}</span>',
            f"<span>{first}</span>{{null}}<span> {second}</span>",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "view.jsx"),
                )

    def test_quoted_angle_bracket_in_html_attribute_is_parsed(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        source = f"<span>{first} </span><span title='>'>{second}</span>"
        self.assertIn(
            "manual_gate",
            policy.matching_labels(source.encode(), "page.html"),
        )

    def test_standalone_svg_visible_text_is_rendered(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        source = f"<text>{first}<tspan> {second}</tspan></text>"
        self.assertIn(
            "manual_gate",
            policy.matching_labels(source.encode(), "graphic.svg"),
        )

    def test_non_rendered_element_bodies_do_not_split_visible_copy(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        for element in ("script", "style", "template"):
            source = (
                f"<span>{first} </span>"
                f"<{element}>ignored()</{element}>"
                f"<span>{second}</span>"
            )
            with self.subTest(element=element):
                self.assertIn(
                    "manual_gate",
                    policy.matching_labels(source.encode(), "page.html"),
                )

    def test_literal_markdown_and_separate_python_statements_stay_distinct(self):
        first = "".join(map(chr, (109, 97, 110, 117, 97, 108)))
        second = "".join(map(chr, (97, 112, 112, 114, 111, 118, 97, 108)))
        sources = (
            (f"{first} * {second}", "README.md"),
            (f'first = "{first} "\nsecond = "{second}"', "messages.py"),
            (f'"{first[:1]}\\\\{first[1:]} {second}"', "copy.js"),
        )
        for source, relative_path in sources:
            with self.subTest(relative_path=relative_path):
                self.assertEqual(
                    policy.matching_labels(source.encode(), relative_path),
                    (),
                )

    def test_utf8_text_with_a_nul_remains_a_candidate(self):
        raw = ("manual " + "approval").encode() + b"\0"
        self.assertIn("manual_gate", policy.matching_labels(raw, "page.html"))

    def test_plural_variants_are_detected(self):
        self.assertIn(
            "manual_gate_plural",
            policy.matching_labels(("manual " + "approvals").encode()),
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

    def test_git_probe_failure_with_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            failed_probe = subprocess.CompletedProcess(
                args=["git"],
                returncode=128,
                stdout="",
                stderr="unsafe repository",
            )
            with mock.patch.object(
                policy.subprocess,
                "run",
                return_value=failed_probe,
            ):
                findings = policy.scan_repository(directory)
        self.assertEqual(
            findings,
            [
                "infrastructure:Git metadata exists but the exact index "
                "cannot be probed"
            ],
        )

    def test_exported_tree_scans_output_named_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "dist"
            output.mkdir()
            tracked_copy = output / "copy.html"
            tracked_copy.write_text("manual " + "approval", encoding="utf-8")
            findings = policy.scan_repository(root)
        self.assertIn("prohibited:manual_gate:dist/copy.html", findings)

    def test_invalid_or_unenumerable_exported_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            self.assertEqual(
                policy.scan_repository(missing),
                ["infrastructure:exported root cannot be enumerated"],
            )
            exported_file = root / "tree.txt"
            exported_file.write_text("ordinary", encoding="utf-8")
            self.assertEqual(
                policy.scan_repository(exported_file),
                ["infrastructure:exported root is not a directory"],
            )
            empty = root / "empty"
            empty.mkdir()
            self.assertEqual(
                policy.scan_repository(empty),
                ["infrastructure:exported tree contains no files"],
            )
            populated = root / "populated"
            populated.mkdir()
            (populated / "source.py").write_text("ordinary", encoding="utf-8")
            with mock.patch.object(
                policy.os,
                "walk",
                side_effect=OSError("blocked"),
            ):
                self.assertEqual(
                    policy.scan_repository(populated),
                    ["infrastructure:exported root cannot be enumerated"],
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
        with mock.patch.dict(os.environ, {
                 "FIRECRAWL_API_KEY": "test-key",
                 "FLEXFACTOR_FIRECRAWL_URL": "",
                 "FLEXFACTOR_FIRECRAWL_API_KEY": "",
             }), \
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

        with mock.patch.dict(os.environ, {
                 "FIRECRAWL_API_KEY": "test-key",
                 "FLEXFACTOR_FIRECRAWL_URL": "",
                 "FLEXFACTOR_FIRECRAWL_API_KEY": "",
             }), \
             mock.patch.object(
                 competitors, "_default_opener", offline_transport
             ), mock.patch.object(
                 competitors,
                 "_default_firecrawl_opener",
                 side_effect=AssertionError("injected transport was bypassed"),
             ):
            hits, backend, skipped = competitors.web_search(
                "competitors",
                allow_credentialed_firecrawl=True,
            )

        self.assertTrue(hits)
        self.assertEqual(backend, "firecrawl")
        self.assertEqual(skipped, {})
        self.assertEqual(calls, ["https://api.firecrawl.dev/v2/search"])

    def test_unrecognized_firecrawl_shapes_fail_closed(self):
        responses = (
            {"error": "authentication failed"},
            {"success": True, "data": {"documents": []}},
            {
                "success": True,
                "data": {"web": [{"title": "broken", "url": "https://"}]},
            },
        )
        environment = {
            "FIRECRAWL_API_KEY": "test-key",
            "FLEXFACTOR_FIRECRAWL_URL": "",
            "FLEXFACTOR_FIRECRAWL_API_KEY": "",
        }
        with mock.patch.dict(os.environ, environment):
            for response in responses:
                with self.subTest(response=response), self.assertRaises(RuntimeError):
                    competitors._firecrawl(
                        "competitors",
                        5,
                        lambda *args, response=response, **kwargs: json.dumps(response),
                    )

    def test_recognized_empty_firecrawl_collection_remains_empty(self):
        environment = {
            "FIRECRAWL_API_KEY": "test-key",
            "FLEXFACTOR_FIRECRAWL_URL": "",
            "FLEXFACTOR_FIRECRAWL_API_KEY": "",
        }
        with mock.patch.dict(os.environ, environment):
            hits = competitors._firecrawl(
                "competitors",
                5,
                lambda *args, **kwargs: json.dumps(
                    {"success": True, "data": {"web": []}}
                ),
            )
        self.assertEqual(hits, [])

    def test_credentialed_firecrawl_requires_explicit_paid_research(self):
        calls: list[str] = []
        environment = {
            "FIRECRAWL_API_KEY": "test-key",
            "FLEXFACTOR_FIRECRAWL_URL": "",
            "FLEXFACTOR_FIRECRAWL_API_KEY": "",
        }
        with mock.patch.dict(os.environ, environment), self.assertRaisesRegex(
            RuntimeError, "paid research is explicit"
        ):
            competitors._firecrawl(
                "competitors",
                5,
                lambda url, *args, **kwargs: calls.append(url),
                allow_credentials=False,
            )
        self.assertEqual(calls, [])

        with mock.patch.dict(os.environ, environment), mock.patch.object(
            competitors,
            "_WEB_BACKENDS",
            (("firecrawl", competitors._firecrawl),),
        ):
            hits, backend, skipped = competitors.web_search(
                "competitors",
                opener=lambda url, *args, **kwargs: calls.append(url),
            )
        self.assertEqual((hits, backend), ([], ""))
        self.assertIn("paid research is explicit", skipped["firecrawl"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
