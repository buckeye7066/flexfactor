"""Enable the distro's restrictive bubblewrap policy only when its probe fails."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

PROFILE = Path("/usr/share/apparmor/extra-profiles/bwrap-userns-restrict")
PARSER = "/usr/sbin/apparmor_parser"
NAMES = {"bwrap", "unpriv_bwrap"}
PROBE = ["/usr/bin/bwrap", "--unshare-all", "--ro-bind", "/", "/",
         "--dev", "/dev", "--proc", "/proc", "--die-with-parent", "--", "/bin/true"]


def run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, timeout=180, **kwargs)


def require_ok(result, purpose):
    if result.returncode:
        raise RuntimeError(f"{purpose}: {result.stderr.strip()}")
    return result.stdout


def apparmor_active():
    return Path("/sys/module/apparmor/parameters/enabled").read_text().strip() == "Y"


def loaded_profiles():
    lines = Path("/sys/kernel/security/apparmor/profiles").read_text().splitlines()
    return dict(line.rsplit(" (", 1) for line in lines if " (" in line)


def reject_conflicts():
    if NAMES.intersection(loaded_profiles()):
        raise RuntimeError("existing bwrap/unpriv_bwrap profile; refusing to replace it")
    # Ask the parser for declared names without loading anything. Include
    # disabled vendor files too: their existence is not permission to overwrite.
    for file in sorted(Path("/etc/apparmor.d").iterdir()):
        if not file.is_file() or file.name.startswith("."):
            continue
        names = require_ok(run([PARSER, "--names", str(file)]),
                           f"could not inspect existing policy {file}").splitlines()
        if (NAMES | {"/usr/bin/bwrap"}).intersection(name.strip() for name in names):
            raise RuntimeError(f"existing bubblewrap policy in {file}; refusing to replace it")
    # The packaged profile has optional local includes. Do not silently load
    # operator extensions as part of this narrowly authorized setup.
    for name in ("bwrap-userns-restrict", "unpriv_bwrap"):
        local = Path("/etc/apparmor.d/local") / name
        if local.exists() or local.is_symlink():
            raise RuntimeError(f"existing local bubblewrap policy {local}; refusing to change it")


def install(uid, gid):
    if os.geteuid() != 0 or uid <= 0 or gid < 0:
        raise RuntimeError("profile setup requires root and an unprivileged probe identity")
    # Reconfirm under the ordinary target identity before privileged changes.
    result = run(PROBE, user=uid, group=gid, extra_groups=[])
    if result.returncode == 0:
        return
    if not apparmor_active():
        raise RuntimeError("bubblewrap failed without active AppArmor; refusing unrelated policy changes")
    reject_conflicts()
    require_ok(run(["/usr/bin/apt-get", "install", "--yes", "apparmor-profiles"]),
               "could not install distro AppArmor profiles")
    reject_conflicts()
    info = PROFILE.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or PROFILE.is_symlink():
        raise RuntimeError("packaged bubblewrap profile is not a protected root-owned file")
    names = set(require_ok(run([PARSER, "--names", str(PROFILE)]),
                           "could not inspect packaged bubblewrap profile").splitlines())
    if names != NAMES:
        raise RuntimeError(f"unexpected packaged bubblewrap profile names: {names}")
    # --add refuses collisions; --replace would silently weaken this boundary.
    require_ok(run([PARSER, "--add", str(PROFILE)]), "could not load restrictive bubblewrap profile")
    loaded = loaded_profiles()
    if any(loaded.get(name) != "enforce)" for name in NAMES):
        raise RuntimeError("restrictive bubblewrap profiles did not both load in enforce mode")


def ensure():
    if os.geteuid() == 0:
        raise RuntimeError("bubblewrap capability must be proved by the ordinary runner user")
    initial = run(PROBE)
    if initial.returncode == 0:
        print("Ordinary-user bubblewrap probe already succeeds; no AppArmor changes")
        return
    if not apparmor_active():
        raise RuntimeError(f"bubblewrap probe failed without active AppArmor: {initial.stderr.strip()}")
    require_ok(run(["sudo", sys.executable, str(Path(__file__).resolve()), "install",
                    "--uid", str(os.getuid()), "--gid", str(os.getgid())]),
               "restrictive bubblewrap profile provisioning failed")
    require_ok(run(PROBE), "ordinary-user bubblewrap probe still fails after restrictive profile setup")
    print("Ordinary-user bubblewrap succeeds with distro restrictive AppArmor policy")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("ensure", "install"))
    parser.add_argument("--uid", type=int, default=0)
    parser.add_argument("--gid", type=int, default=-1)
    args = parser.parse_args()
    if args.operation == "ensure":
        ensure()
    else:
        install(args.uid, args.gid)
