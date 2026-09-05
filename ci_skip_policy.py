#!/usr/bin/env python3
"""Fail CI when unittest skips are not explicitly explained for that OS."""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys


_SKIP = re.compile(r"\.\.\. skipped (?P<quote>['\"])(?P<reason>.*)(?P=quote)\s*$")

# These are capability, opposite-platform, or explicitly retired tests.
# Counts are upper bounds:
# gaining a capability may remove a skip, but a new or duplicated skip fails.
_ALLOW: dict[str, tuple[tuple[re.Pattern[str], int], ...]] = {
    "Windows": (
        (re.compile(
            r"^retired characterization: superseded by the one best-available paid-to-free ladder$"
        ), 33),
        (re.compile(r"^POSIX openat component-walk unavailable on this platform$"), 6),
        (re.compile(r"^review_files entry not present - covered by review_file test$"), 1),
        (re.compile(r"^POSIX openat write path unavailable on this platform$"), 1),
        (re.compile(r"^the replacement schedule is POSIX-specific$"), 1),
        (re.compile(r"^no live catalog at .*AITime[\\/]+routes\.json$"), 1),
        (re.compile(r"^BLOCKED: no OS network isolation on this host "), 1),
        (re.compile(r"^BLOCKED: no sufficient OS sandbox "), 1),
    ),
    "Linux": (
        (re.compile(
            r"^retired characterization: superseded by the one best-available paid-to-free ladder$"
        ), 33),
        (re.compile(r"^Windows junction test$"), 1),
        (re.compile(r"^review_files entry not present - covered by review_file test$"), 1),
        (re.compile(r"^POSIX openat\+O_NOFOLLOW leaf open does not use the Windows "), 1),
        (re.compile(r"^this host uses the POSIX openat writer, not the win fallback$"), 1),
        (re.compile(r"^POSIX dir_fd path uses a handle, not the stat re-check$"), 1),
        (re.compile(r"^no live catalog at .*AITime[\\/]+routes\.json$"), 1),
        (re.compile(r"^BLOCKED: Windows-only assertion on this host "), 1),
        # The desktop launcher runs Windows PowerShell 5.1, and the
        # NativeCommandError-on-native-stderr behaviour it guards against is
        # specific to 5.1 (pwsh 7.6 does not do it). Both arms of that probe
        # therefore skip here rather than silently measuring the wrong host.
        (re.compile(r"^BLOCKED: needs Windows PowerShell 5\.1 "), 2),
        (re.compile(r"^BLOCKED: no OS network isolation on this host "), 1),
        (re.compile(r"^BLOCKED: no sufficient OS sandbox "), 1),
    ),
}


def verify(runner_os: str, text: str) -> list[str]:
    """Return policy violations found in concatenated unittest output."""
    if runner_os not in _ALLOW:
        return [f"unsupported runner OS: {runner_os}"]
    counts: collections.Counter[int] = collections.Counter()
    violations: list[str] = []
    for line in text.splitlines():
        match = _SKIP.search(line)
        if not match:
            continue
        reason = match.group("reason")
        hits = [i for i, (pattern, _maximum) in enumerate(_ALLOW[runner_os])
                if pattern.search(reason)]
        if len(hits) != 1:
            violations.append(f"unapproved skip: {reason}")
            continue
        counts[hits[0]] += 1
    for index, count in counts.items():
        pattern, maximum = _ALLOW[runner_os][index]
        if count > maximum:
            violations.append(
                f"skip count {count} exceeds {maximum}: {pattern.pattern}")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runner_os", choices=sorted(_ALLOW))
    parser.add_argument("logs", nargs="+", type=pathlib.Path)
    args = parser.parse_args(argv)
    missing = [str(path) for path in args.logs if not path.is_file()]
    if missing:
        print("missing test logs: " + ", ".join(missing), file=sys.stderr)
        return 2
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in args.logs)
    violations = verify(args.runner_os, text)
    if violations:
        print("skip policy failed:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    found = sum(1 for line in text.splitlines() if _SKIP.search(line))
    print(f"skip policy: {found} explained skip(s), 0 unapproved ({args.runner_os})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
