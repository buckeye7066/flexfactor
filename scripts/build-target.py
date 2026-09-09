#!/usr/bin/env python3
"""Dispatch only configured build tools; never install or launch the application."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("auto", "windows", "android", "web", "ios", "macos", "safari", "linux")
HOSTS = {"win32": "windows", "darwin": "macos", "linux": "linux"}


def plan(config, host, requested):
    if host not in HOSTS:
        raise ValueError("Unsupported build host: " + host)
    if requested not in TARGETS:
        raise ValueError("Unknown target: " + requested)
    target = HOSTS[host] if requested == "auto" else requested
    entry = config["targets"].get(target)
    if entry is None:
        raise ValueError(
            f'{config["name"]}: no configured {target} build. ' + config["guidance"]
        )
    if host not in entry["hosts"]:
        raise ValueError(f"{target} requires a build host in {entry['hosts']}")
    return {
        "app": config["name"], "host": host, "requested": requested,
        "target": target, **entry,
    }


def options(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--host", choices=HOSTS,
                        help="Simulated host; allowed only with --dry-run")
    args = parser.parse_args(argv)
    if args.host is not None and not args.dry_run:
        parser.error("--host is available only with --dry-run")
    return args


def execute(selected, root=ROOT, runner=subprocess.run):
    for step in selected["commands"]:
        command = list(step)
        if command[0] == "python":
            command[0] = sys.executable
        elif command[0] == "gradle" and sys.platform == "win32":
            # All arguments are fixed in the reviewed manifest, not user input.
            command = ["cmd.exe", "/d", "/c", "gradle.bat", *command[1:]]
        runner(command, cwd=root, check=True)


def main(argv=None):
    args = options(argv)
    try:
        config = json.loads((ROOT / "scripts/build-targets.json").read_text())
        selected = plan(config, args.host or sys.platform, args.target)
        print(json.dumps(selected, indent=2))
        if not args.dry_run:
            execute(selected)
            print("Build complete: " + selected["output"])
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
