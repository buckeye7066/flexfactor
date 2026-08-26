"""Executable repository-language policy for FlexFactor releases.

The scanner covers the exact Git index when it belongs to this repository and
falls back to the workspace for exported source trees. Tracked sparse-checkout
blobs are read from the index, and every plausible BOM-less UTF-16/32 decoding
is examined so byte order cannot hide a policy violation.
"""
from __future__ import annotations

import argparse
import ast
import codecs
from html import unescape
from html.entities import html5
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tokenize
from typing import Iterable
import unicodedata


_EXCLUDED_DIRECTORIES = {".git"}

_BINARY_SOURCE_SUFFIXES = {
    ".7z",
    ".a",
    ".avi",
    ".bmp",
    ".bz2",
    ".class",
    ".dll",
    ".dylib",
    ".eot",
    ".exe",
    ".flac",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".ogg",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xz",
    ".zip",
}

_LITERAL_SOURCE_SUFFIXES = {
    ".cjs",
    ".cts",
    ".js",
    ".jsx",
    ".json",
    ".mjs",
    ".mts",
    ".py",
    ".pyw",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}

_MARKUP_SOURCE_SUFFIXES = {
    ".htm",
    ".html",
    ".jsx",
    ".mdx",
    ".svg",
    ".svelte",
    ".tsx",
    ".vue",
}

_MARKDOWN_SOURCE_SUFFIXES = {".md", ".mdx"}
_JSX_SOURCE_SUFFIXES = {".jsx", ".mdx", ".tsx"}
_CSS_SOURCE_SUFFIXES = {".css", ".less", ".sass", ".scss"}

_STATIC_QUOTED_LITERAL_SOURCE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_STATIC_TEMPLATE_OPERAND_SOURCE = r'''`(?:\\.|[^`\\$]|\$(?!\{))*`'''
_STATIC_TEMPLATE_LITERAL_SOURCE = (
    r'''`(?:\\.|[^`\\$]|\$(?!\{)|\$\{(?:\\.|[^}`\\]|'''
    + _STATIC_TEMPLATE_OPERAND_SOURCE
    + r''')*\})*`'''
)
_STATIC_LITERAL_SOURCE = (
    rf"(?:{_STATIC_QUOTED_LITERAL_SOURCE}|{_STATIC_TEMPLATE_LITERAL_SOURCE})"
)
_STATIC_LITERAL = re.compile(_STATIC_LITERAL_SOURCE, re.DOTALL)
_JSX_COMMENT_EXPRESSION = re.compile(r"\{\s*/\*[\s\S]*?\*/\s*\}")
_JSX_NON_RENDERING_EXPRESSION = re.compile(
    r"\{\s*(?:false|null|true|undefined)\s*\}"
)
_JSX_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_NON_RENDERED_ELEMENT = re.compile(
    r'''<(?P<tag>script|style|template)\b'''
    r'''(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*>'''
    r'''[\s\S]*?</(?P=tag)\s*>''',
    re.IGNORECASE,
)
_HTML_TAG = re.compile(
    r'''</?[A-Za-z][A-Za-z0-9:-]*(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*/?>''',
    re.DOTALL,
)
_HTML_OPEN_TAG = re.compile(
    r'''<(?P<tag>[A-Za-z][A-Za-z0-9:-]*)'''
    r'''(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*/?>''',
    re.DOTALL,
)
_HTML_ATTRIBUTE = re.compile(
    r'''(?:^|\s)(?P<name>[^\s"'=<>`]+)\s*=\s*(?:"(?P<double>[^"]*)"'''
    r'''|'(?P<single>[^']*)'|(?P<bare>[^\s"'=<>`]+))'''
)
_EXPOSED_MARKUP_ATTRIBUTES = {
    "alt",
    "aria-description",
    "aria-label",
    "aria-placeholder",
    "aria-roledescription",
    "aria-valuetext",
    "placeholder",
    "title",
}
_SCRIPT_BLOCK = re.compile(
    r'''<script\b(?P<attributes>'''
    r'''(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?)\s*>'''
    r'''(?P<body>[\s\S]*?)</script\s*>''',
    re.IGNORECASE,
)
_STYLE_BLOCK = re.compile(
    r'''<style(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*>'''
    r'''(?P<body>[\s\S]*?)</style\s*>''',
    re.IGNORECASE,
)
_RCDATA_ELEMENT = re.compile(
    r'''(?P<opening><(?P<tag>textarea|title)\b'''
    r'''(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*>)'''
    r'''(?P<body>[\s\S]*?)(?P<closing></(?P=tag)\s*>)''',
    re.IGNORECASE,
)
_JS_ESCAPE = re.compile(
    r'''\\(?:u\{(?P<braced>0*[0-9A-Fa-f]{1,6})\}'''
    r'''|u(?P<fixed>[0-9A-Fa-f]{4})'''
    r'''|x(?P<hexadecimal>[0-9A-Fa-f]{2})'''
    r'''|(?P<octal>[0-3][0-7]{0,2}|[4-7][0-7]?)'''
    r'''|(?P<continuation>\r\n|[\r\n\u2028\u2029])'''
    r'''|(?P<simple>0|[^\d\r\n\u2028\u2029]))''',
)
_SOURCE_TRIVIA = (
    r"(?:(?:\s+)|/\*[\s\S]*?\*/|//[^\r\n\u2028\u2029]*"
    r"(?:\r\n|[\r\n\u2028\u2029]))*"
)
_SOURCE_GROUPING = (
    r"(?:(?:\s+)|/\*[\s\S]*?\*/|//[^\r\n\u2028\u2029]*"
    r"(?:\r\n|[\r\n\u2028\u2029])|[()])*"
)
_JSX_STATIC_EXPRESSION = re.compile(
    rf"\{{(?P<expression>{_SOURCE_GROUPING}{_STATIC_LITERAL_SOURCE}"
    rf"(?:{_SOURCE_GROUPING}\+{_SOURCE_GROUPING}{_STATIC_LITERAL_SOURCE})*"
    rf"{_SOURCE_GROUPING})\}}",
    re.DOTALL,
)
_TEMPLATE_INTERPOLATION = re.compile(
    rf"(?<!\\)\$\{{(?P<expression>{_SOURCE_GROUPING}"
    rf"(?:{_STATIC_QUOTED_LITERAL_SOURCE}|{_STATIC_TEMPLATE_OPERAND_SOURCE})"
    rf"(?:{_SOURCE_GROUPING}\+{_SOURCE_GROUPING}"
    rf"(?:{_STATIC_QUOTED_LITERAL_SOURCE}|{_STATIC_TEMPLATE_OPERAND_SOURCE}))*"
    rf"{_SOURCE_GROUPING})\}}",
    re.DOTALL,
)
_CSS_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.X | re.DOTALL)
_CSS_ESCAPE = re.compile(
    r"\\(?P<hexadecimal>[0-9A-Fa-f]{1,6})(?:\r\n|[\t\n\f\r ])?"
    r"|\\(?P<continuation>\r\n|[\n\f\r])"
    r"|\\(?P<character>[\s\S])"
)
_AMBIGUOUS_LEGACY_HTML_REFERENCE = re.compile(
    r"&(?P<name>"
    + "|".join(
        sorted(
            (re.escape(name) for name in html5 if not name.endswith(";")),
            key=len,
            reverse=True,
        )
    )
    + r")(?=[A-Za-z0-9=])"
)

_PROHIBITED_FRAGMENTS = (
    ("organizational_gate", "sign" + " off"),
    ("organizational_gate_compact", "sign" + "off"),
    ("organizational_gate_third_person", "signs" + " off"),
    ("organizational_gate_gerund", "signing" + " off"),
    ("organizational_gate_plural", "sign" + " offs"),
    ("organizational_gate_compact_plural", "sign" + "offs"),
    ("completed_organizational_gate", "signed" + " off"),
    ("identity_gate", "authenticated " + "reviewer"),
    ("identity_gate_plural", "authenticated " + "reviewers"),
    ("mandatory_reviewer_gate", "required " + "reviewer"),
    ("mandatory_reviewer_gate_plural", "required " + "reviewers"),
    ("mandatory_person_gate", "required " + "human"),
    ("person_reviewer_gate", "human " + "reviewer"),
    ("person_reviewer_gate_plural", "human " + "reviewers"),
    ("person_review_gate", "human " + "review"),
    ("person_review_gate_plural", "human " + "reviews"),
    ("manual_gate", "manual " + "approval"),
    ("manual_gate_plural", "manual " + "approvals"),
    ("implementation_claim", "self " + "certified"),
    ("implementation_claim_noun", "self " + "certification"),
)

_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


class PolicyInfrastructureError(RuntimeError):
    """The exact repository exists, but its tracked contents cannot be read."""


def normalize_language(value: str) -> str:
    """Normalize line wrapping and separator variants before matching."""
    value = unicodedata.normalize("NFKC", value)
    without_format_controls = "".join(
        character
        for character in value.lower()
        if not _is_default_ignorable(character)
    )
    return re.sub(r"[-_\s]+", " ", without_format_controls)


def _is_default_ignorable(character: str) -> bool:
    code_point = ord(character)
    return unicodedata.category(character) == "Cf" or any(
        first <= code_point <= last
        for first, last in _DEFAULT_IGNORABLE_RANGES
    )


def contains_language_fragment(value: str, target: str) -> bool:
    """Match a normalized phrase only at lexical word boundaries."""
    offset = 0
    while offset <= len(value) - len(target):
        index = value.find(target, offset)
        if index < 0:
            return False
        before = value[index - 1] if index else ""
        after_index = index + len(target)
        after = value[after_index] if after_index < len(value) else ""
        before_is_word = bool(before) and (before.isalnum() or before == "_")
        after_is_word = bool(after) and (after.isalnum() or after == "_")
        if not before_is_word and not after_is_word:
            return True
        offset = index + 1
    return False


def _looks_like_text(value: str | None) -> bool:
    if not value:
        return False
    controls = sum(
        ord(character) < 32 and character not in "\t\r\n"
        for character in value
    )
    replacements = value.count("\ufffd")
    control_limit = max(1, len(value) // 20)
    replacement_limit = max(1, len(value) // 50)
    return controls <= control_limit and replacements <= replacement_limit


def _decode_with_replacement(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding, "replace")
    except (UnicodeDecodeError, UnicodeError):
        return None


def decode_text_candidates(
    raw: bytes,
    relative_path: str = "",
) -> tuple[str, ...]:
    """Return every plausible textual interpretation of repository bytes."""
    if (
        relative_path
        and Path(relative_path).suffix.lower() in _BINARY_SOURCE_SUFFIXES
    ):
        return ()
    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            # A single damaged unit must not make the rest of a BOM-tagged text
            # file disappear from policy scanning. The same bounded
            # replacement-character rule used for UTF-8 decides whether the
            # tolerant decoding still looks textual.
            decoded = _decode_with_replacement(raw, encoding)
            return (decoded,) if _looks_like_text(decoded) else ()

    candidates: list[str] = []
    decoded_utf8 = raw.decode("utf-8", "replace") if raw else ""
    retain_utf8 = bool(relative_path) and (
        Path(relative_path).suffix.lower() not in _BINARY_SOURCE_SUFFIXES
    )
    if retain_utf8 or _looks_like_text(decoded_utf8):
        candidates.append(decoded_utf8)
    if b"\0" not in raw:
        return tuple(candidates)

    for encoding in ("utf-32-le", "utf-32-be", "utf-16-le", "utf-16-be"):
        decoded = _decode_with_replacement(raw, encoding)
        if _looks_like_text(decoded) and decoded not in candidates:
            candidates.append(decoded)
    return tuple(candidates)


def _unquote_static_literal(literal: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        if match.group("braced") is not None:
            code_point = int(match.group("braced"), 16)
            if code_point <= 0x10FFFF and not 0xD800 <= code_point <= 0xDFFF:
                return chr(code_point)
            return match.group()
        if match.group("fixed") is not None:
            code_point = int(match.group("fixed"), 16)
            if not 0xD800 <= code_point <= 0xDFFF:
                return chr(code_point)
            return match.group()
        if match.group("hexadecimal") is not None:
            return chr(int(match.group("hexadecimal"), 16))
        if match.group("octal") is not None:
            return (
                match.group()
                if literal.startswith("`")
                else chr(int(match.group("octal"), 8))
            )
        if match.group("continuation") is not None:
            return ""
        simple = match.group("simple") or ""
        return {
            "0": "\0",
            "b": "\b",
            "t": "\t",
            "n": "\n",
            "v": "\v",
            "f": "\f",
            "r": "\r",
            '"': '"',
            "'": "'",
            "`": "`",
            "\\": "\\",
        }.get(simple, simple)

    return _JS_ESCAPE.sub(replace_escape, literal[1:-1])


def _constant_expression_literals(expression: str) -> tuple[str, ...]:
    literals: list[str] = []
    parentheses = 0
    expect_operand = True
    index = 0
    while index < len(expression):
        if expression[index].isspace():
            index += 1
            continue
        if expression.startswith("/*", index):
            end = expression.find("*/", index + 2)
            if end < 0:
                return ()
            index = end + 2
            continue
        if expression.startswith("//", index):
            terminator = re.search(r"[\r\n\u2028\u2029]", expression[index + 2 :])
            if terminator is None:
                return ()
            index += 2 + terminator.end()
            continue
        if expression[index] == "(":
            if not expect_operand:
                return ()
            parentheses += 1
            index += 1
            continue
        if expression[index] == ")":
            if not parentheses or expect_operand:
                return ()
            parentheses -= 1
            index += 1
            continue
        if expression[index] == "+":
            if expect_operand:
                return ()
            expect_operand = True
            index += 1
            continue
        if not expect_operand:
            return ()
        match = _STATIC_LITERAL.match(expression, index)
        if match is None:
            return ()
        literals.append(match.group())
        expect_operand = False
        index = match.end()
    return (
        tuple(literals)
        if literals and not expect_operand and not parentheses
        else ()
    )


def _render_static_literal(literal: str) -> str:
    if not literal.startswith("`"):
        return _unquote_static_literal(literal)
    def replace_interpolation(match: re.Match[str]) -> str:
        operands = _constant_expression_literals(match.group("expression"))
        if not operands:
            return match.group()
        return "".join(
            _unquote_static_literal(operand) for operand in operands
        ).replace("\\", "\\\\")

    body = _TEMPLATE_INTERPOLATION.sub(replace_interpolation, literal[1:-1])
    return _unquote_static_literal(f"`{body}`")


def _decode_css_string(literal: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        hexadecimal = match.group("hexadecimal")
        if hexadecimal is not None:
            code_point = int(hexadecimal, 16)
            if code_point and code_point <= 0x10FFFF and not (
                0xD800 <= code_point <= 0xDFFF
            ):
                return chr(code_point)
            return "\ufffd"
        if match.group("continuation") is not None:
            return ""
        return match.group("character") or ""

    return _CSS_ESCAPE.sub(replace_escape, literal[1:-1])


def _iter_css_content_values(value: str) -> Iterable[str]:
    """Yield content declarations with terminators located outside strings."""
    index = 0
    statement_start = 0
    nesting = 0
    block_kinds: list[str] = []
    while index < len(value):
        if value[index] in {'"', "'"}:
            quote = value[index]
            index += 1
            while index < len(value):
                if value[index] == "\\":
                    index += 2
                    continue
                index += 1
                if value[index - 1] == quote:
                    break
            continue
        if value[index] == "(":
            nesting += 1
            index += 1
            continue
        if value[index] == ")" and nesting:
            nesting -= 1
            index += 1
            continue
        if value[index] == "{" and not nesting:
            header = value[statement_start:index].lstrip()
            block_kinds.append(
                "at-rule" if header.startswith("@") else "style-rule"
            )
            statement_start = index + 1
            index += 1
            continue
        if value[index] == "}" and not nesting:
            if block_kinds:
                block_kinds.pop()
            statement_start = index + 1
            index += 1
            continue
        if value[index] == ";" and not nesting:
            statement_start = index + 1
            index += 1
            continue
        if value[index : index + 7].lower() != "content":
            index += 1
            continue
        before = value[index - 1] if index else ""
        after = value[index + 7] if index + 7 < len(value) else ""
        if (before and (before.isalnum() or before in "_-")) or (
            after and (after.isalnum() or after in "_-")
        ):
            index += 7
            continue
        if (
            not block_kinds
            or block_kinds[-1] != "style-rule"
            or value[statement_start:index].strip()
        ):
            index += 7
            continue
        cursor = index + 7
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor >= len(value) or value[cursor] != ":":
            index += 7
            continue
        start = cursor + 1
        cursor = start
        quote = ""
        escaped = False
        value_parentheses = 0
        while cursor < len(value):
            character = value[cursor]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
                cursor += 1
                continue
            if character in {'"', "'"}:
                quote = character
            elif character == "\\":
                cursor += 2
                continue
            elif character == "(":
                value_parentheses += 1
            elif character == ")" and value_parentheses:
                value_parentheses -= 1
            elif character in ";}" and not value_parentheses:
                break
            cursor += 1
        yield value[start:cursor]
        if cursor < len(value) and value[cursor] == "}" and block_kinds:
            block_kinds.pop()
        index = cursor + 1
        statement_start = index


def _render_css_content(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    without_comments = _strip_css_comments(value)
    for declaration in _iter_css_content_values(without_comments):
        for branch in _split_css_alternative_content(declaration):
            url_ranges = _css_url_argument_ranges(branch)
            chain = ""
            previous_end: int | None = None
            for literal in _CSS_STRING.finditer(branch):
                if any(
                    start <= literal.start() < end
                    for start, end in url_ranges
                ):
                    continue
                rendered = _decode_css_string(literal.group())
                candidates.append(rendered)
                chain = (
                    chain + rendered
                    if previous_end is not None
                    and not branch[previous_end : literal.start()].strip()
                    else rendered
                )
                if chain != rendered:
                    candidates.append(chain)
                previous_end = literal.end()
    return tuple(candidates)


def _css_url_argument_ranges(value: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        if value[index] in {'"', "'"}:
            quote = value[index]
            index += 1
            while index < len(value):
                if value[index] == "\\":
                    index += 2
                    continue
                index += 1
                if value[index - 1] == quote:
                    break
            continue
        if value[index : index + 3].lower() != "url":
            index += 1
            continue
        before = value[index - 1] if index else ""
        after = value[index + 3] if index + 3 < len(value) else ""
        if (before and (before.isalnum() or before in "_-")) or (
            after and (after.isalnum() or after in "_-")
        ):
            index += 3
            continue
        cursor = index + 3
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor >= len(value) or value[cursor] != "(":
            index += 3
            continue
        start = cursor + 1
        cursor = start
        quote = ""
        escaped = False
        parentheses = 1
        while cursor < len(value) and parentheses:
            character = value[cursor]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
            elif character in {'"', "'"}:
                quote = character
            elif character == "(":
                parentheses += 1
            elif character == ")":
                parentheses -= 1
            cursor += 1
        ranges.append((start, cursor - 1 if not parentheses else len(value)))
        index = cursor
    return tuple(ranges)


def _strip_css_comments(value: str) -> str:
    rendered: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote:
            rendered.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
            rendered.append(character)
            index += 1
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            index = len(value) if end < 0 else end + 2
            continue
        rendered.append(character)
        index += 1
    return "".join(rendered)


def _split_css_alternative_content(value: str) -> tuple[str, ...]:
    quote = ""
    escaped = False
    parentheses = 0
    for index, character in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "(":
            parentheses += 1
        elif character == ")" and parentheses:
            parentheses -= 1
        elif character == "/" and not parentheses:
            return value[:index], value[index + 1 :]
    return (value,)


def _render_embedded_css_content(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for match in _STYLE_BLOCK.finditer(value):
        candidates.extend(_render_css_content(match.group("body")))
    return tuple(candidates)


def _decode_html_character_references(
    value: str,
    *,
    in_attribute: bool = False,
) -> str:
    if in_attribute:
        value = _AMBIGUOUS_LEGACY_HTML_REFERENCE.sub(
            lambda match: f"&amp;{match.group('name')}",
            value,
        )
    return unescape(value)


def _render_markup_attributes(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for tag in _HTML_OPEN_TAG.finditer(value):
        attributes = tuple(
            (
                attribute.group("name").lower(),
                _decode_html_character_references(
                    next(
                        item
                        for item in (
                            attribute.group("double"),
                            attribute.group("single"),
                            attribute.group("bare"),
                        )
                        if item is not None
                    ),
                    in_attribute=True,
                ),
            )
            for attribute in _HTML_ATTRIBUTE.finditer(tag.group())
        )
        input_type = next(
            (item for name, item in attributes if name == "type"),
            "",
        ).strip().lower()
        for name, candidate in attributes:
            visible_input_value = (
                tag.group("tag").lower() == "input"
                and name == "value"
                and input_type in {"button", "reset", "submit"}
            )
            if name in _EXPOSED_MARKUP_ATTRIBUTES or visible_input_value:
                candidates.append(candidate)
    return tuple(candidates)


def _mask_markup_rcdata_bodies(value: str) -> str:
    return _RCDATA_ELEMENT.sub(
        lambda match: (
            match.group("opening")
            + "".join(
                character if character in "\r\n" else " "
                for character in match.group("body")
            )
            + match.group("closing")
        ),
        value,
    )


def _render_inline_event_handlers(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for tag in _HTML_OPEN_TAG.finditer(value):
        for attribute in _HTML_ATTRIBUTE.finditer(tag.group()):
            if re.fullmatch(r"on[a-z]+", attribute.group("name"), re.I) is None:
                continue
            source = next(
                item
                for item in (
                    attribute.group("double"),
                    attribute.group("single"),
                    attribute.group("bare"),
                )
                if item is not None
            )
            candidates.extend(
                _render_javascript_literals(
                    _decode_html_character_references(
                        source,
                        in_attribute=True,
                    )
                )
            )
    return tuple(candidates)


def _render_inline_executable_scripts(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    executable_types = {
        "",
        "application/ecmascript",
        "application/javascript",
        "module",
        "text/ecmascript",
        "text/javascript",
    }
    type_pattern = re.compile(
        r'''(?:^|\s)type\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))''',
        re.IGNORECASE,
    )
    for script in _SCRIPT_BLOCK.finditer(value):
        type_match = type_pattern.search(script.group("attributes"))
        script_type = next(
            (
                item
                for item in (type_match.groups() if type_match else ("",))
                if item is not None
            ),
            "",
        )
        script_type = (
            _decode_html_character_references(
                script_type,
                in_attribute=True,
            )
            .strip()
            .lower()
            .split(";", 1)[0]
            .strip()
        )
        if script_type in executable_types:
            candidates.extend(_render_javascript_literals(script.group("body")))
            candidates.extend(_render_inline_html_assignments(script.group("body")))
    return tuple(candidates)


def _javascript_expression_end(value: str, start: int) -> int:
    parentheses = brackets = braces = 0
    index = start
    while index < len(value):
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            index = len(value) if end < 0 else end + 2
            continue
        if value.startswith("//", index):
            end = re.search(r"[\r\n\u2028\u2029]", value[index + 2 :])
            index = len(value) if end is None else index + 2 + end.end()
            continue
        if value[index] in {'"', "'", "`"}:
            quote = value[index]
            index += 1
            while index < len(value):
                if value[index] == "\\":
                    index += 2
                    continue
                index += 1
                if value[index - 1] == quote:
                    break
            continue
        character = value[index]
        if character == "(":
            parentheses += 1
        elif character == ")" and parentheses:
            parentheses -= 1
        elif character == "[":
            brackets += 1
        elif character == "]" and brackets:
            brackets -= 1
        elif character == "{":
            braces += 1
        elif character == "}" and braces:
            braces -= 1
        elif character == ";" and not (parentheses or brackets or braces):
            return index
        index += 1
    return len(value)


def _render_html_fragment_candidates(value: str) -> tuple[str, ...]:
    executable_markup = _mask_markup_rcdata_bodies(value)
    return (
        unescape(_strip_markup(value)),
        *_render_markup_attributes(executable_markup),
        *_render_embedded_css_content(executable_markup),
        *_render_inline_event_handlers(executable_markup),
    )


def _render_inline_html_assignments(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    pattern = re.compile(r"\.(?:innerHTML|outerHTML)\s*=\s*")
    for assignment in pattern.finditer(value):
        start = assignment.end()
        end = _javascript_expression_end(value, start)
        operands = _constant_expression_literals(value[start:end])
        if not operands:
            continue
        rendered = "".join(_render_static_literal(item) for item in operands)
        candidates.extend(_render_html_fragment_candidates(rendered))
    return tuple(candidates)


def _replace_jsx_static_expression(match: re.Match[str]) -> str:
    rendered = "".join(
        _render_static_literal(literal)
        for literal in _constant_expression_literals(match.group("expression"))
    )
    return rendered if rendered is not None else match.group()


def _scan_jsx_tag(
    value: str,
    start: int,
) -> tuple[int, bool, bool, str] | None:
    if value.startswith("<>", start):
        return start + 2, False, False, ""
    if value.startswith("</>", start):
        return start + 3, True, False, ""
    opening = re.match(r"</?([A-Za-z][A-Za-z0-9:.-]*)", value[start:])
    if opening is None:
        return None
    quote = ""
    escaped = False
    index = start + opening.end()
    while index < len(value):
        character = value[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            closing = value[start + 1 : start + 2] == "/"
            self_closing = value[start:index].rstrip().endswith("/")
            return index + 1, closing, self_closing, opening.group(1).lower()
        index += 1
    return None


def _jsx_element_depth_at(value: str, position: int) -> int:
    depth = 0
    index = 0
    while index < position:
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            index = position if end < 0 else end + 2
            continue
        if value.startswith("//", index):
            end = re.search(r"[\r\n\u2028\u2029]", value[index + 2 :])
            index = position if end is None else index + 2 + end.end()
            continue
        if value[index] in {'"', "'", "`"}:
            quote = value[index]
            index += 1
            while index < position:
                if value[index] == "\\":
                    index += 2
                    continue
                index += 1
                if value[index - 1] == quote:
                    break
            continue
        if value[index] == "<":
            tag = _scan_jsx_tag(value, index)
            if tag is not None:
                end, closing, self_closing, name = tag
                if closing:
                    depth = max(0, depth - 1)
                elif not self_closing and name not in _JSX_VOID_ELEMENTS:
                    depth += 1
                index = end
                continue
        index += 1
    return depth


def _is_likely_jsx_child_expression(
    value: str,
    start: int,
    end: int,
) -> bool:
    if _jsx_element_depth_at(value, start):
        return True
    before = value[:start].rstrip()
    after = value[end:].lstrip()
    return bool(
        re.search(r"</[A-Za-z][A-Za-z0-9:.-]*\s*>$", before)
        and re.match(r"(?:<|\{)", after)
    )


def _project_static_jsx_expressions(value: str) -> str:
    return _JSX_STATIC_EXPRESSION.sub(
        lambda match: (
            _replace_jsx_static_expression(match)
            if _is_likely_jsx_child_expression(
                value,
                match.start(),
                match.end(),
            )
            else match.group()
        ),
        value,
    )


def _strip_markup(value: str) -> str:
    rcdata_segments: list[tuple[str, str]] = []

    def shield_rcdata(match: re.Match[str]) -> str:
        placeholder = f"\0RCDATA{len(rcdata_segments)}\0"
        rcdata_segments.append((placeholder, match.group("body")))
        return placeholder

    shielded = _RCDATA_ELEMENT.sub(shield_rcdata, value)
    without_comments = _HTML_COMMENT.sub("", shielded)
    without_non_rendered = _NON_RENDERED_ELEMENT.sub("", without_comments)
    rendered = _HTML_TAG.sub(" ", without_non_rendered)
    for placeholder, segment in rcdata_segments:
        rendered = rendered.replace(placeholder, segment)
    return rendered


def _normalize_markdown_reference_label(value: str) -> str:
    """Apply Markdown's case-folding and whitespace-only label identity."""
    return " ".join(value.split()).casefold()


def _render_markdown(value: str) -> str:
    code_segments: list[tuple[str, str]] = []

    def shield(match: re.Match[str]) -> str:
        placeholder = f"\0MARKDOWNCODE{len(code_segments)}\0"
        code_segments.append((placeholder, match.group()))
        return placeholder

    value = re.sub(
        r"^ {0,3}(`{3,}|~{3,})[^\r\n]*(?:\r\n|[\r\n])"
        r"[\s\S]*?^ {0,3}\1[ \t]*$",
        shield,
        value,
        flags=re.M,
    )
    value = re.sub(r"(`+)([^\r\n]*?)\1", shield, value)
    reference_ids = {
        _normalize_markdown_reference_label(match.group(1))
        for match in re.finditer(r"^\s{0,3}\[([^\]]+)\]:\s+\S+.*$", value, re.M)
    }
    value = re.sub(r"^\s{0,3}\[[^\]]+\]:\s+\S+.*$", "", value, flags=re.M)
    rendered = _strip_markup(value)
    rendered = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", rendered)
    rendered = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", rendered)

    def replace_reference(match: re.Match[str]) -> str:
        label = match.group("label")
        reference = match.group("reference") or label
        return (
            label
            if _normalize_markdown_reference_label(reference) in reference_ids
            else match.group()
        )

    rendered = re.sub(
        r"!?\[(?P<label>[^\]]+)\]\[(?P<reference>[^\]]*)\]",
        replace_reference,
        rendered,
    )
    rendered = re.sub(
        r"!?\[(?P<label>[^\]]+)\]",
        lambda match: (
            match.group("label")
            if _normalize_markdown_reference_label(match.group("label"))
            in reference_ids
            else match.group()
        ),
        rendered,
    )
    rendered = re.sub(r"~~(?=\S)([\s\S]*?\S)~~", r"\1", rendered)
    rendered = re.sub(r"\*\*(?=\S)([\s\S]*?\S)\*\*", r"\1", rendered)
    rendered = re.sub(r"__(?=\S)([\s\S]*?\S)__", r"\1", rendered)
    rendered = re.sub(r"\*(?=\S)([^*\r\n]*?\S)\*", r"\1", rendered)
    rendered = re.sub(r"_(?=\S)([^_\r\n]*?\S)_", r"\1", rendered)
    rendered = re.sub(r"\\(?:\r\n|[\r\n])", " ", rendered)
    rendered = unescape(rendered)
    for placeholder, segment in code_segments:
        rendered = rendered.replace(placeholder, segment)
    return rendered


def _static_python_ast_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bytes):
            return node.value.decode("latin-1")
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.FormattedValue):
        value = _static_python_ast_string(node.value)
        if value is None or node.conversion not in {-1, ord("a"), ord("r"), ord("s")}:
            return None
        if node.conversion == ord("a"):
            value = ascii(value)
        elif node.conversion == ord("r"):
            value = repr(value)
        elif node.conversion == ord("s"):
            value = str(value)
        format_spec = ""
        if node.format_spec is not None:
            rendered_spec = _static_python_ast_string(node.format_spec)
            if rendered_spec is None:
                return None
            format_spec = rendered_spec
        try:
            return format(value, format_spec)
        except (TypeError, ValueError):
            return None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            rendered = _static_python_ast_string(part)
            if rendered is None:
                return None
            parts.append(rendered)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_python_ast_string(node.left)
        right = _static_python_ast_string(node.right)
        return None if left is None or right is None else left + right
    return None


def _render_python_literals(
    value: str,
    project_explicit_addition: bool = True,
) -> tuple[str, ...]:
    """Render safe Python literals and the language's implicit concatenation."""
    candidates: list[str] = []
    chain: str | None = None
    try:
        tokens = tokenize.generate_tokens(io.StringIO(value).readline)
        for current in tokens:
            if current.type == tokenize.STRING:
                try:
                    rendered_value = ast.literal_eval(current.string)
                except (SyntaxError, ValueError):
                    chain = None
                    continue
                if isinstance(rendered_value, bytes):
                    rendered = rendered_value.decode("latin-1")
                elif isinstance(rendered_value, str):
                    rendered = rendered_value
                else:
                    chain = None
                    continue
                candidates.append(rendered)
                chain = (chain or "") + rendered
                candidates.append(chain)
            elif current.type not in {tokenize.COMMENT, tokenize.NL}:
                chain = None
    except (IndentationError, SyntaxError, tokenize.TokenError):
        # Keep every candidate produced before malformed source interrupted the
        # tokenizer; raw-text matching still covers the complete file.
        pass
    try:
        tree = ast.parse(value)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and not project_explicit_addition:
                continue
            rendered = _static_python_ast_string(node)
            if rendered is not None:
                candidates.append(rendered)
    return tuple(dict.fromkeys(candidates))


def _mask_javascript_comments(value: str) -> str:
    """Blank source comments while preserving literal offsets and line breaks."""
    rendered = list(value)
    quote = ""
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            index += 1
            continue
        if character in {'"', "'", "`"}:
            quote = character
            index += 1
            continue
        end = -1
        if value.startswith("/*", index):
            closing = value.find("*/", index + 2)
            end = len(value) if closing < 0 else closing + 2
        elif value.startswith("//", index):
            terminator = re.search(r"[\r\n\u2028\u2029]", value[index + 2 :])
            end = (
                len(value)
                if terminator is None
                else index + 2 + terminator.start()
            )
        if end < 0:
            index += 1
            continue
        for offset in range(index, end):
            if not value[offset].isspace():
                rendered[offset] = " "
        index = end
    return "".join(rendered)


def _javascript_regex_can_start(value: str, index: int) -> bool:
    prefix = value[:index].rstrip()
    if not prefix or prefix.endswith("=>"):
        return True
    if prefix[-1] in "([{,:;=!?~%&|^*+-<>":
        return True
    word = re.search(r"([A-Za-z_$][\w$]*)$", prefix)
    return bool(
        word
        and word.group(1)
        in {
            "await",
            "case",
            "delete",
            "in",
            "instanceof",
            "of",
            "return",
            "throw",
            "typeof",
            "void",
            "yield",
        }
    )


def _javascript_regex_end(value: str, start: int) -> int | None:
    escaped = False
    character_class = False
    index = start + 1
    while index < len(value):
        character = value[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "[":
            character_class = True
        elif character == "]":
            character_class = False
        elif character in "\r\n\u2028\u2029":
            return None
        elif character == "/" and not character_class:
            index += 1
            while index < len(value) and value[index].isalpha():
                index += 1
            return index
        index += 1
    return None


def _mask_javascript_regex_literals(value: str) -> str:
    rendered = list(value)
    index = 0
    while index < len(value):
        if value[index] in {'"', "'", "`"}:
            quote = value[index]
            index += 1
            while index < len(value):
                if value[index] == "\\":
                    index += 2
                    continue
                index += 1
                if value[index - 1] == quote:
                    break
            continue
        if value[index] == "/" and _javascript_regex_can_start(value, index):
            end = _javascript_regex_end(value, index)
            if end is not None:
                for offset in range(index, end):
                    if not value[offset].isspace():
                        rendered[offset] = " "
                index = end
                continue
        index += 1
    return "".join(rendered)


def _render_static_raw_template(literal: str) -> str:
    def replace_interpolation(match: re.Match[str]) -> str:
        operands = _constant_expression_literals(match.group("expression"))
        if not operands:
            return match.group()
        return "".join(_render_static_literal(operand) for operand in operands)

    return _TEMPLATE_INTERPOLATION.sub(replace_interpolation, literal[1:-1])


def _render_javascript_literals(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    previous: re.Match[str] | None = None
    previous_rendered = ""
    chain: str | None = None
    projected = _mask_javascript_regex_literals(
        _mask_javascript_comments(value)
    )
    for match in _STATIC_LITERAL.finditer(projected):
        literal = value[match.start() : match.end()]
        is_raw_template = literal.startswith("`") and re.search(
            r"String\s*\.\s*raw\s*$",
            projected[: match.start()],
        )
        rendered_literal = (
            _render_static_raw_template(literal)
            if is_raw_template
            else _render_static_literal(literal)
        )
        candidates.append(rendered_literal)
        gap = projected[previous.end() : match.start()] if previous else ""
        gap_without_grouping = gap.replace("(", "").replace(")", "")
        if previous and re.fullmatch(r"\s*\+\s*", gap_without_grouping):
            chain = (
                (chain if chain is not None else previous_rendered)
                + rendered_literal
            )
            candidates.append(chain)
        else:
            chain = None
        previous = match
        previous_rendered = rendered_literal
    return tuple(candidates)


def rendered_source_candidates(value: str, relative_path: str) -> tuple[str, ...]:
    """Approximate user-visible JSX and adjacent static string expressions."""
    suffix = Path(relative_path).suffix.lower()
    candidates: list[str] = []
    visible_value = value
    if suffix in _JSX_SOURCE_SUFFIXES:
        visible_value = _JSX_COMMENT_EXPRESSION.sub("", visible_value)
        visible_value = _JSX_NON_RENDERING_EXPRESSION.sub("", visible_value)
        visible_value = _project_static_jsx_expressions(visible_value)
    if suffix in _MARKUP_SOURCE_SUFFIXES:
        executable_markup = _mask_markup_rcdata_bodies(visible_value)
        candidates.append(unescape(_strip_markup(visible_value)))
        candidates.extend(_render_markup_attributes(executable_markup))
        candidates.extend(_render_embedded_css_content(executable_markup))
        candidates.extend(_render_inline_executable_scripts(executable_markup))
        candidates.extend(_render_inline_event_handlers(executable_markup))
    if suffix in _MARKDOWN_SOURCE_SUFFIXES:
        candidates.append(_render_markdown(visible_value))
    if suffix in _CSS_SOURCE_SUFFIXES:
        candidates.extend(_render_css_content(value))
    if suffix in {".py", ".pyw"}:
        path = Path(relative_path)
        is_policy_or_test = path.name in {
            "flexfactor_release_policy.py",
            "flexfactor_tests.py",
            "test_flexfactor_release_policy.py",
        }
        candidates.extend(
            _render_python_literals(
                value,
                project_explicit_addition=not is_policy_or_test,
            )
        )
        return tuple(candidates)
    if suffix not in _LITERAL_SOURCE_SUFFIXES:
        return tuple(candidates)
    candidates.extend(_render_javascript_literals(value))
    return tuple(candidates)


def _workspace_entries(root: Path) -> list[tuple[str, Path, bool]]:
    try:
        root_mode = root.stat().st_mode
    except OSError as error:
        raise PolicyInfrastructureError(
            "exported root cannot be enumerated"
        ) from error
    if not stat.S_ISDIR(root_mode):
        raise PolicyInfrastructureError("exported root is not a directory")

    def traversal_error(error: OSError) -> None:
        raise PolicyInfrastructureError(
            "exported root cannot be enumerated"
        ) from error

    entries: list[tuple[str, Path, bool]] = []
    try:
        for current, directories, files in os.walk(root, onerror=traversal_error):
            directories[:] = [
                name for name in directories if name not in _EXCLUDED_DIRECTORIES
            ]
            for name in files:
                path = Path(current, name)
                entries.append((path.relative_to(root).as_posix(), path, False))
    except OSError as error:
        raise PolicyInfrastructureError(
            "exported root cannot be enumerated"
        ) from error
    if not entries:
        raise PolicyInfrastructureError("exported tree contains no files")
    return entries


def repository_entries(root: Path) -> list[tuple[str, Path, bool]]:
    """Enumerate the exact index, or an exported workspace when no index fits."""
    root = root.resolve()
    git_metadata = root / ".git"
    has_git_metadata = (
        git_metadata.is_dir()
        or git_metadata.is_file()
        or git_metadata.is_symlink()
    )
    try:
        git_probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        if has_git_metadata:
            raise PolicyInfrastructureError(
                "Git metadata exists but the exact index cannot be probed"
            ) from error
        return _workspace_entries(root)
    if git_probe.returncode != 0:
        if has_git_metadata:
            raise PolicyInfrastructureError(
                "Git metadata exists but the exact index cannot be probed"
            )
        return _workspace_entries(root)
    git_root = Path(git_probe.stdout.strip()).resolve()
    if os.path.normcase(str(git_root)) != os.path.normcase(str(root)):
        if has_git_metadata:
            raise PolicyInfrastructureError(
                "Git metadata resolved outside the requested repository"
            )
        return _workspace_entries(root)

    try:
        tracked_result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PolicyInfrastructureError("exact Git index is unreadable") from error
    relative_paths = [
        value.decode("utf-8", "surrogateescape")
        for value in tracked_result.stdout.split(b"\0")
        if value
    ]
    if not relative_paths:
        raise PolicyInfrastructureError("exact Git index contains no files")
    return [
        (relative_path, root / relative_path, True)
        for relative_path in relative_paths
    ]


def _read_entry(root: Path, relative_path: str, path: Path, tracked: bool) -> bytes:
    if tracked:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f":{relative_path}"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise OSError("tracked blob is unavailable from the Git index")
        return result.stdout
    try:
        mode = path.lstat().st_mode
    except OSError:
        mode = 0
    if stat.S_ISREG(mode):
        return path.read_bytes()
    if stat.S_ISLNK(mode):
        return os.readlink(path).encode("utf-8", "surrogateescape")
    raise OSError("workspace entry is unavailable")


def matching_labels(raw: bytes, relative_path: str = "") -> tuple[str, ...]:
    """Return each policy label found in at least one plausible decoding."""
    normalized_candidates = tuple(
        normalize_language(variant)
        for candidate in decode_text_candidates(raw, relative_path)
        for variant in (
            candidate,
            *rendered_source_candidates(candidate, relative_path),
        )
    )
    return tuple(
        label
        for label, fragment in _PROHIBITED_FRAGMENTS
        if any(
            contains_language_fragment(normalized, fragment)
            for normalized in normalized_candidates
        )
    )


def scan_repository(root: Path | str) -> list[str]:
    """Return deterministic findings; an unreadable tracked blob is a failure."""
    resolved_root = Path(root).resolve()
    findings: list[str] = []
    try:
        entries = repository_entries(resolved_root)
    except PolicyInfrastructureError as error:
        return [f"infrastructure:{error}"]
    for relative_path, path, tracked in entries:
        try:
            raw = _read_entry(resolved_root, relative_path, path, tracked)
        except OSError as error:
            findings.append(f"unreadable:{relative_path}:{error}")
            continue
        findings.extend(
            f"prohibited:{label}:{relative_path}"
            for label in matching_labels(raw, relative_path)
        )
    return sorted(findings)


def _format_findings(findings: Iterable[str]) -> str:
    return "\n".join(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check FlexFactor release language")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args(argv)
    findings = scan_repository(args.root)
    if findings:
        print(_format_findings(findings))
        return 1
    print("release language policy: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
