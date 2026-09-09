"""Opt-in updates for authenticated Git source installs (Python standard library).

Run before starting the app: --prompt exits 10 after a successful update so the
launcher can restart itself and refresh dependencies. --check is JSON, --apply
requires the revision displayed by a prior check. Never resets or stashes files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class UpdateError(RuntimeError):
    pass


class SourceUpdater:
    def __init__(self, root, repo):
        self.root = Path(root).resolve()
        if not re.fullmatch(r"buckeye7066/[A-Za-z0-9_.-]+", repo):
            raise UpdateError("The update repository is not trusted.")
        self.repo = repo

    def git(self, *args, timeout=30):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), *args], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Git update check unavailable. Check Git, network and your GitHub sign-in.") from exc
        if result.returncode:
            # Do not reflect remote URLs or credential-helper output into the UI.
            raise UpdateError("Git refused this update. Check network/sign-in, repository ownership, local changes and branch history.")
        return result.stdout.strip()

    def trusted_source(self):
        url = self.git("remote", "get-url", "origin")
        allowed = {
            f"https://github.com/{self.repo}", f"https://github.com/{self.repo}.git",
            f"git@github.com:{self.repo}.git", f"ssh://git@github.com/{self.repo}.git",
        }
        if url.lower() not in {x.lower() for x in allowed}:
            raise UpdateError("Updates require this app's original GitHub repository.")
        # --get-url resolves insteadOf rewrites. Reject redirected credentials/sources.
        effective = self.git("ls-remote", "--get-url", url)
        if effective.lower() not in {x.lower() for x in allowed}:
            raise UpdateError("The GitHub update URL has been redirected in Git settings.")
        return url

    def ensure_idle_checkout(self):
        if self.git("symbolic-ref", "--short", "HEAD") != "main":
            raise UpdateError("Switch to main before updating; your current branch was preserved.")
        if self.git("status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("Save or commit local source changes before updating. No files were changed.")

    def check(self):
        source = self.trusted_source()
        self.ensure_idle_checkout()
        current = self.git("rev-parse", "HEAD")
        remote = self.git("ls-remote", "--exit-code", source, "refs/heads/main")
        latest = remote.split()[0] if remote else ""
        if not re.fullmatch(r"[0-9a-f]{40}", latest):
            raise UpdateError("GitHub did not return a valid app revision.")
        if latest == current:
            return {"status": "current", "current": current, "latest": latest}
        # Download commit metadata to prove this is newer. This never checks out code.
        self.git("fetch", "--no-tags", "--no-write-fetch-head", source, latest, timeout=90)
        self.git("merge-base", "--is-ancestor", current, latest)
        return {"status": "available", "current": current, "latest": latest}

    def apply(self, expected):
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise UpdateError("Check for updates before choosing Update.")
        # Serialize this updater. Git additionally locks its index/ref transaction.
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir"))
        lock = git_dir / "source-app-update.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise UpdateError("Another update is active. If it was interrupted, review the repository before removing its update lock.") from exc
        try:
            os.close(fd)
            state = self.check()
            if state["latest"] != expected:
                raise UpdateError("A different version is now available. Check again before updating.")
            if state["status"] == "current":
                return state
            self.ensure_idle_checkout()
            # Fast-forward only: no merge commit, conflict resolution, force, reset,
            # stash, clean, or overwriting ignored personal files such as .env/data.
            self.git("-c", "core.hooksPath=", "merge", "--ff-only",
                     "--no-overwrite-ignore", "--no-edit", expected, timeout=120)
            if self.git("rev-parse", "HEAD") != expected:
                raise UpdateError("The installed revision did not match the selected update.")
            return {"status": "updated", "current": expected, "latest": expected}
        finally:
            lock.unlink(missing_ok=True)


def prompt(updater, name):
    try:
        state = updater.check()
    except UpdateError as exc:
        print(f"{name} update check: {exc}", file=sys.stderr)
        return 0  # offline/sign-in problems must not prevent using the installed app
    if state["status"] != "available":
        return 0
    try:
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk()
    except Exception:
        print(f"{name}: update {state['latest'][:12]} available. Run source_app_update.py --repo {updater.repo} --apply {state['latest']} to install.")
        return 0
    window.title(f"{name} update")
    window.resizable(False, False)
    result = [0]
    tk.Label(window, text=f"A newer version of {name} is available.",
             padx=24, pady=14, font=("TkDefaultFont", 11, "bold")).pack()
    tk.Label(window, text=f"Installed {state['current'][:12]}  →  Latest {state['latest'][:12]}\n"
             "Update before opening the app? Your personal files are kept.", padx=24).pack()
    status = tk.StringVar(value="")
    tk.Label(window, textvariable=status, padx=24, pady=8).pack()
    controls = tk.Frame(window)
    controls.pack(pady=14)

    def install():
        update_button.configure(state="disabled")
        later_button.configure(state="disabled")
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        status.set("Updating…")
        window.update_idletasks()
        try:
            applied = updater.apply(state["latest"])
            result[0] = 10 if applied["status"] == "updated" else 0
            window.destroy()
        except UpdateError as exc:
            messagebox.showerror(f"{name} update", str(exc), parent=window)
            window.destroy()

    update_button = tk.Button(controls, text="Update", width=12, command=install)
    update_button.pack(side="left", padx=6)
    later_button = tk.Button(controls, text="Later", width=12, command=window.destroy)
    later_button.pack(side="left", padx=6)
    window.mainloop()
    return result[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--name", default="This app")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--prompt", action="store_true")
    action.add_argument("--apply", metavar="REVISION")
    args = parser.parse_args()
    try:
        updater = SourceUpdater(args.root, args.repo)
        if args.prompt:
            return prompt(updater, args.name)
        print(json.dumps(updater.apply(args.apply) if args.apply else updater.check()))
        return 0
    except UpdateError as exc:
        print(json.dumps({"status": "unavailable", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
