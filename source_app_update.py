"""Opt-in updates for authenticated Git source installs (Python standard library).

Run before starting the app: --prompt exits 10 after a successful update so the
launcher can restart itself and refresh dependencies; exit 20 aborts startup
after a busy or failed update. --check is JSON, --apply
requires the revision displayed by a prior check. Never resets or stashes files.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import threading


class UpdateError(RuntimeError):
    pass


class UpdateBusy(UpdateError):
    pass


class SourceUpdater:
    def __init__(self, root, repo, idle_port=None):
        self.root = Path(root).resolve()
        if not re.fullmatch(r"buckeye7066/[A-Za-z0-9_.-]+", repo):
            raise UpdateError("The update repository is not trusted.")
        self.repo = repo
        ports = idle_port if isinstance(idle_port, list) else [idle_port]
        if any(port is not None and not 1 <= port <= 65535 for port in ports):
            raise UpdateError("The app port must be between 1 and 65535.")
        self.idle_port = idle_port
        self._owns_update_lock = False

    def git(self, *args, timeout=30):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
                   GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ConnectionAttempts=1 -o ServerAliveInterval=10 -o ServerAliveCountMax=1",
                   GIT_SSH_VARIANT="ssh")
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
            f"git@github.com:{self.repo}", f"ssh://git@github.com/{self.repo}",
        }
        if url.lower() not in {x.lower() for x in allowed}:
            raise UpdateError("Updates require this app's original GitHub repository.")
        # --get-url resolves insteadOf rewrites. Reject redirected credentials/sources.
        effective = self.git("ls-remote", "--get-url", url)
        if effective.lower() not in {x.lower() for x in allowed}:
            raise UpdateError("The GitHub update URL has been redirected in Git settings.")
        return url

    def ensure_idle_checkout(self):
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir"))
        if (git_dir / "source-app-update.lock").exists() and not self._owns_update_lock:
            raise UpdateBusy("Another update is active. Wait for it to finish before opening the app.")
        ports = self.idle_port if isinstance(self.idle_port, list) else [self.idle_port]
        for idle_port in ports:
            if idle_port is None:
                continue
            try:
                # An exclusive bind proves the port is available without
                # connecting to the game or relying on Windows refusal timing.
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind(("127.0.0.1", idle_port))
            except OSError as exc:
                raise UpdateBusy("Close the local app server before updating. The port is occupied or unavailable; no source was changed.") from exc
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
            raise UpdateBusy("Another update is active. If it was interrupted, review the repository before removing its update lock.") from exc
        except OSError as exc:
            raise UpdateError("Could not create the update lock. Check folder permissions; no source was changed.") from exc
        try:
            self._owns_update_lock = True
            os.close(fd)
            state = self.check()
            if state["latest"] != expected:
                raise UpdateError("A different version is now available. Check again before updating.")
            if state["status"] == "current":
                return state
            self.ensure_idle_checkout()
            if self.repo.lower() == "buckeye7066/flexfactor":
                try:
                    (git_dir / "flexfactor-refresh-needs-install").write_text(expected, encoding="ascii")
                except OSError as exc:
                    raise UpdateError("Could not record dependency preparation; no source was updated.") from exc
            # Fast-forward only: no merge commit, conflict resolution, force, reset,
            # stash, clean, or overwriting ignored personal files such as .env/data.
            self.git("-c", "core.hooksPath=", "merge", "--ff-only",
                     "--no-overwrite-ignore", "--no-edit", expected, timeout=120)
            if self.git("rev-parse", "HEAD") != expected:
                raise UpdateError("The installed revision did not match the selected update.")
            return {"status": "updated", "current": expected, "latest": expected}
        finally:
            self._owns_update_lock = False
            try:
                lock.unlink(missing_ok=True)
            except OSError as exc:
                raise UpdateError("The update lock could not be removed. Review folder permissions before opening the app.") from exc


def begin_apply(updater, expected):
    """Keep Git/network work off Tk's event loop; return one result or error."""
    completed = queue.Queue(maxsize=1)

    def work():
        try:
            completed.put(updater.apply(expected))
        except UpdateError as exc:
            completed.put(exc)
        except Exception:
            completed.put(UpdateError("The update could not finish. Review the installed revision before opening the app."))

    threading.Thread(target=work, name="source-app-update", daemon=True).start()
    return completed


def prompt(updater, name):
    try:
        state = updater.check()
    except UpdateBusy as exc:
        print(f"{name} update check: {exc}")
        return 20  # never start another app instance during source replacement
    except UpdateError as exc:
        print(f"{name} update check: {exc}")
        return 0  # offline/sign-in problems must not prevent using the installed app
    if state["status"] != "available":
        return 0
    try:
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk()
    except Exception:
        command = [sys.executable, str(Path(__file__).resolve()), "--repo", updater.repo,
                   "--root", str(updater.root), "--apply", state["latest"]]
        ports = updater.idle_port if isinstance(updater.idle_port, list) else [updater.idle_port]
        for port in ports:
            if port is not None:
                command.extend(["--idle-port", str(port)])
        if os.name == "nt":
            rendered = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in command)
            terminal = "PowerShell"
        else:
            rendered, terminal = shlex.join(command), "a terminal"
        print(f"{name}: update {state['latest'][:12]} available. Run in {terminal}:\n{rendered}")
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
        completed = begin_apply(updater, state["latest"])

        def poll():
            try:
                applied = completed.get_nowait()
            except queue.Empty:
                window.after(75, poll)
                return
            if isinstance(applied, UpdateError):
                result[0] = 20
                messagebox.showerror(f"{name} update", str(applied), parent=window)
            else:
                result[0] = 10 if applied["status"] == "updated" else 0
            window.destroy()

        window.after(75, poll)

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
    parser.add_argument("--idle-port", type=int, action="append", help="Refuse updates while this server port is occupied (repeat for multiple servers)")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--prompt", action="store_true")
    action.add_argument("--apply", metavar="REVISION")
    args = parser.parse_args()
    try:
        updater = SourceUpdater(args.root, args.repo, idle_port=args.idle_port)
        if args.prompt:
            return prompt(updater, args.name)
        print(json.dumps(updater.apply(args.apply) if args.apply else updater.check()))
        return 0
    except UpdateError as exc:
        print(json.dumps({"status": "unavailable", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
