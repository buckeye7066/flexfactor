"""Executable repository-language policy for FlexFactor releases.

The scanner covers the exact Git index when it belongs to this repository and
falls back to the workspace for exported source trees. Tracked sparse-checkout
blobs are read from the index, and every plausible BOM-less UTF-16/32 decoding
is examined so byte order cannot hide a policy violation.
"""
from __future__ import annotations

import argparse
import codecs
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Iterable


_EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
}

_PROHIBITED_FRAGMENTS = (
    ("organizational_gate", "sign" + " off"),
    ("organizational_gate_compact", "sign" + "off"),
    ("completed_organizational_gate", "signed" + " off"),
    ("identity_gate", "authenticated " + "reviewer"),
    ("mandatory_reviewer_gate", "required " + "reviewer"),
    ("mandatory_person_gate", "required " + "human"),
    ("person_reviewer_gate", "human " + "reviewer"),
    ("person_review_gate", "human " + "review"),
    ("manual_gate", "manual " + "approval"),
    ("implementation_claim", "self " + "certified"),
    ("implementation_claim_noun", "self " + "certification"),
)


def normalize_language(value: str) -> str:
    """Normalize line wrapping and separator variants before matching."""
    return re.sub(r"[-_\s]+", " ", value.lower())


def _looks_like_text(value: str | None) -> bool:
    if not value or "\0" in value:
        return False
    controls = sum(
        ord(character) < 32 and character not in "\t\r\n"
        for character in value
    )
    return controls / len(value) <= 0.05


def _decode_strict(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding, "strict")
    except (UnicodeDecodeError, UnicodeError):
        return None


def decode_text_candidates(raw: bytes) -> tuple[str, ...]:
    """Return every plausible textual interpretation of repository bytes."""
    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            decoded = _decode_strict(raw, encoding)
            return (decoded,) if _looks_like_text(decoded) else ()

    if b"\0" not in raw:
        return (raw.decode("utf-8", "replace"),) if raw else ()

    candidates: list[str] = []
    for encoding in ("utf-32-le", "utf-32-be", "utf-16-le", "utf-16-be"):
        decoded = _decode_strict(raw, encoding)
        if _looks_like_text(decoded) and decoded not in candidates:
            candidates.append(decoded)
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
        git_root = Path(
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
        ).resolve()
        if os.path.normcase(str(git_root)) != os.path.normcase(str(root)):
            raise RuntimeError("enclosing Git index belongs to another repository")
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout
        relative_paths = [
            value.decode("utf-8", "surrogateescape")
            for value in tracked.split(b"\0")
            if value
        ]
        if not relative_paths:
            raise RuntimeError("Git index contains no files")
        return [
            (relative_path, root / relative_path, True)
            for relative_path in relative_paths
        ]
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return _workspace_entries(root)


def _read_entry(root: Path, relative_path: str, path: Path, tracked: bool) -> bytes:
    try:
        mode = path.lstat().st_mode
    except OSError:
        mode = 0
    if stat.S_ISREG(mode):
        return path.read_bytes()
    if not tracked:
        raise OSError("workspace entry is unavailable")
    result = subprocess.run(
        ["git", "-C", str(root), "show", f":{relative_path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError("tracked blob is unavailable from the Git index")
    return result.stdout


def matching_labels(raw: bytes) -> tuple[str, ...]:
    """Return each policy label found in at least one plausible decoding."""
    normalized_candidates = tuple(
        normalize_language(candidate) for candidate in decode_text_candidates(raw)
    )
    return tuple(
        label
        for label, fragment in _PROHIBITED_FRAGMENTS
        if any(fragment in normalized for normalized in normalized_candidates)
    )


def scan_repository(root: Path | str) -> list[str]:
    """Return deterministic findings; an unreadable tracked blob is a failure."""
    resolved_root = Path(root).resolve()
    findings: list[str] = []
    for relative_path, path, tracked in repository_entries(resolved_root):
        try:
            raw = _read_entry(resolved_root, relative_path, path, tracked)
        except OSError as error:
            findings.append(f"unreadable:{relative_path}:{error}")
            continue
        findings.extend(
            f"prohibited:{label}:{relative_path}" for label in matching_labels(raw)
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
