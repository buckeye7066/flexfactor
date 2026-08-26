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
    ".svelte",
    ".tsx",
    ".vue",
}

_MARKDOWN_SOURCE_SUFFIXES = {".md", ".mdx"}
_JSX_SOURCE_SUFFIXES = {".jsx", ".mdx", ".tsx"}
_CSS_SOURCE_SUFFIXES = {".css", ".less", ".sass", ".scss"}

_STATIC_LITERAL = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`',
    re.DOTALL,
)

_JSX_WHITESPACE_EXPRESSION = re.compile(
    r'''\{\s*(?:"(?P<double>(?:\\.|[^"\\])*)"'''
    r'''|'(?P<single>(?:\\.|[^'\\])*)'|`(?P<template>(?:\\.|[^`\\])*)`)\s*\}''',
    re.DOTALL,
)
_JSX_COMMENT_EXPRESSION = re.compile(r"\{\s*/\*[\s\S]*?\*/\s*\}")

_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_NON_RENDERED_ELEMENT = re.compile(
    r"<(?P<tag>script|style|template)\b[^>]*>[\s\S]*?</(?P=tag)\s*>",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(
    r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>]*?)?\s*/?>",
    re.DOTALL,
)
_HTML_OPEN_TAG = re.compile(
    r'''<[A-Za-z][A-Za-z0-9:-]*(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*/?>''',
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
    r"<script\b(?P<attributes>[^>]*)>(?P<body>[\s\S]*?)</script\s*>",
    re.IGNORECASE,
)
_STYLE_BLOCK = re.compile(
    r'''<style(?:\s+(?:"[^"]*"|'[^']*'|[^'">])*)?\s*>'''
    r'''(?P<body>[\s\S]*?)</style\s*>''',
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
_STATIC_QUOTED_LITERAL_SOURCE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_STATIC_QUOTED_LITERAL = re.compile(_STATIC_QUOTED_LITERAL_SOURCE, re.DOTALL)
_TEMPLATE_INTERPOLATION = re.compile(
    rf"(?<!\\)\$\{{(?P<expression>{_SOURCE_GROUPING}{_STATIC_QUOTED_LITERAL_SOURCE}"
    rf"(?:{_SOURCE_GROUPING}\+{_SOURCE_GROUPING}{_STATIC_QUOTED_LITERAL_SOURCE})*"
    rf"{_SOURCE_GROUPING})\}}",
    re.DOTALL,
)
_CSS_CONTENT_DECLARATION = re.compile(r"\bcontent\s*:\s*([^;}]+)", re.IGNORECASE)
_CSS_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.X | re.DOTALL)
_CSS_ESCAPE = re.compile(
    r"\\(?P<hexadecimal>[0-9A-Fa-f]{1,6})(?:\r\n|[\t\n\f\r ])?"
    r"|\\(?P<continuation>\r\n|[\n\f\r])"
    r"|\\(?P<character>[\s\S])"
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


def _render_static_literal(literal: str) -> str:
    if not literal.startswith("`"):
        return _unquote_static_literal(literal)
    body = _TEMPLATE_INTERPOLATION.sub(
        lambda match: "".join(
            _unquote_static_literal(literal_match.group())
            for literal_match in _STATIC_QUOTED_LITERAL.finditer(
                match.group("expression")
            )
        ).replace("\\", "\\\\"),
        literal[1:-1],
    )
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


def _render_css_content(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    without_comments = re.sub(r"/\*[\s\S]*?\*/", "", value)
    for declaration in _CSS_CONTENT_DECLARATION.finditer(without_comments):
        for branch in _split_css_alternative_content(declaration.group(1)):
            combined = ""
            for literal in _CSS_STRING.finditer(branch):
                rendered = _decode_css_string(literal.group())
                candidates.append(rendered)
                combined += rendered
            if combined:
                candidates.append(combined)
    return tuple(candidates)


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


def _render_markup_attributes(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for tag in _HTML_OPEN_TAG.finditer(value):
        for attribute in _HTML_ATTRIBUTE.finditer(tag.group()):
            if attribute.group("name").lower() not in _EXPOSED_MARKUP_ATTRIBUTES:
                continue
            candidate = next(
                value
                for value in (
                    attribute.group("double"),
                    attribute.group("single"),
                    attribute.group("bare"),
                )
                if value is not None
            )
            candidates.append(unescape(candidate))
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
        script_type = script_type.strip().lower().split(";", 1)[0].strip()
        if script_type in executable_types:
            candidates.extend(_render_javascript_literals(script.group("body")))
    return tuple(candidates)


def _replace_jsx_whitespace_expression(match: re.Match[str]) -> str:
    for quote, group_name in (("\"", "double"), ("'", "single"), ("`", "template")):
        body = match.group(group_name)
        if body is None:
            continue
        rendered = _render_static_literal(f"{quote}{body}{quote}")
        if rendered and rendered.isspace():
            return rendered
        break
    return match.group()


def _strip_markup(value: str) -> str:
    without_comments = _HTML_COMMENT.sub("", value)
    without_non_rendered = _NON_RENDERED_ELEMENT.sub("", without_comments)
    return _HTML_TAG.sub(" ", without_non_rendered)


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
        normalize_language(match.group(1))
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
            if normalize_language(reference) in reference_ids
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
            if normalize_language(match.group("label")) in reference_ids
            else match.group()
        ),
        rendered,
    )
    rendered = re.sub(r"~~(?=\S)([\s\S]*?\S)~~", r"\1", rendered)
    rendered = re.sub(r"\*\*(?=\S)([\s\S]*?\S)\*\*", r"\1", rendered)
    rendered = re.sub(r"__(?=\S)([\s\S]*?\S)__", r"\1", rendered)
    rendered = re.sub(r"\*(?=\S)([^*\r\n]*?\S)\*", r"\1", rendered)
    rendered = re.sub(r"_(?=\S)([^_\r\n]*?\S)_", r"\1", rendered)
    for placeholder, segment in code_segments:
        rendered = rendered.replace(placeholder, segment)
    return rendered


def _render_python_literals(value: str) -> tuple[str, ...]:
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
    return tuple(dict.fromkeys(candidates))


def _render_javascript_literals(value: str) -> tuple[str, ...]:
    candidates: list[str] = []
    previous: re.Match[str] | None = None
    chain: str | None = None
    for match in _STATIC_LITERAL.finditer(value):
        rendered_literal = _render_static_literal(match.group())
        candidates.append(rendered_literal)
        gap = value[previous.end() : match.start()] if previous else ""
        gap_without_comments = re.sub(
            r"/\*[\s\S]*?\*/|//[^\r\n\u2028\u2029]*",
            "",
            gap,
        )
        if previous and re.fullmatch(r"\s*\+\s*", gap_without_comments):
            first_literal = _render_static_literal(previous.group())
            chain = (
                (chain if chain is not None else first_literal)
                + rendered_literal
            )
            candidates.append(chain)
        else:
            chain = None
        previous = match
    return tuple(candidates)


def rendered_source_candidates(value: str, relative_path: str) -> tuple[str, ...]:
    """Approximate user-visible JSX and adjacent static string expressions."""
    suffix = Path(relative_path).suffix.lower()
    candidates: list[str] = []
    visible_value = value
    if suffix in _JSX_SOURCE_SUFFIXES:
        visible_value = _JSX_COMMENT_EXPRESSION.sub("", visible_value)
        visible_value = _JSX_WHITESPACE_EXPRESSION.sub(
            _replace_jsx_whitespace_expression,
            visible_value,
        )
    if suffix in _MARKUP_SOURCE_SUFFIXES:
        candidates.append(unescape(_strip_markup(visible_value)))
        candidates.extend(_render_markup_attributes(visible_value))
        candidates.extend(_render_embedded_css_content(visible_value))
        candidates.extend(_render_inline_executable_scripts(visible_value))
    if suffix in _MARKDOWN_SOURCE_SUFFIXES:
        candidates.append(unescape(_render_markdown(visible_value)))
    if suffix in _CSS_SOURCE_SUFFIXES:
        candidates.extend(_render_css_content(value))
    if suffix in {".py", ".pyw"}:
        candidates.extend(_render_python_literals(value))
        return tuple(candidates)
    if suffix not in _LITERAL_SOURCE_SUFFIXES:
        return tuple(candidates)
    candidates.extend(_render_javascript_literals(value))
    return tuple(candidates)


def _workspace_entries(root: Path) -> list[tuple[str, Path, bool]]:
    entries: list[tuple[str, Path, bool]] = []
    for current, directories, files in os.walk(root):
        directories[:] = [
            name for name in directories if name not in _EXCLUDED_DIRECTORIES
        ]
        for name in files:
            path = Path(current, name)
            entries.append((path.relative_to(root).as_posix(), path, False))
    return entries


def repository_entries(root: Path) -> list[tuple[str, Path, bool]]:
    """Enumerate the exact index, or an exported workspace when no index fits."""
    root = root.resolve()
    try:
        git_probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return _workspace_entries(root)
    if git_probe.returncode != 0:
        return _workspace_entries(root)
    git_root = Path(git_probe.stdout.strip()).resolve()
    if os.path.normcase(str(git_root)) != os.path.normcase(str(root)):
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
