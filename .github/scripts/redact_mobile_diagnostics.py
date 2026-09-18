#!/usr/bin/env python3
"""Emit a bounded, credential-safe diagnostic for a failed mobile run."""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_INPUT_BYTES = 64 * 1024
MAX_LINES = 24
MAX_LINE_CHARS = 240
SENSITIVE = re.compile(
    r"(?i)(authorization|bearer|access[_ -]?token|refresh[_ -]?token|device[_ -]?code|"
    r"client[_ -]?secret|api[_ -]?key|password|github_pat_|gh[opusr]_|sk-[a-z0-9])"
)


def redacted_tail(raw: bytes) -> list[str]:
    """Return the useful tail without ever reproducing credential-bearing lines."""
    text = raw[-MAX_INPUT_BYTES:].decode("utf-8", "replace")
    safe: list[str] = []
    for source in text.splitlines():
        line = " ".join(source.strip().split())
        if not line:
            continue
        if SENSITIVE.search(line):
            line = "[REDACTED SENSITIVE LINE]"
        # Prevent untrusted output from creating additional workflow commands.
        line = line.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        safe.append(line[:MAX_LINE_CHARS])
    return safe[-MAX_LINES:]


def main(path: str) -> int:
    source = Path(path)
    if not source.is_file():
        print("::error title=FlexFactor failure::Execution failed before a bounded log was created.")
        return 0
    lines = redacted_tail(source.read_bytes())
    detail = " | ".join(lines) if lines else "Execution failed without a diagnostic line."
    print(f"::error title=FlexFactor bounded failure::{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "mobile-run.log"))
