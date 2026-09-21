"""Provision only this hosted job's delegated memory subtree, never the host root."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

BASE = Path("/sys/fs/cgroup")


def group_path(run_id, attempt, job):
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise ValueError("invalid managed run identity")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job):
        raise ValueError("invalid managed job identity")
    return BASE / f"flexfactor-memory-{run_id}-{attempt}-{job}"


def verify_cgroup_mount():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("managed memory requires Linux")
    lines = Path("/proc/self/mountinfo").read_text().splitlines()
    if not any(line.split()[4] == str(BASE) and " - cgroup2 " in line for line in lines):
        raise RuntimeError("managed memory requires cgroup-v2 at /sys/fs/cgroup")


def read_receipt(receipt):
    # Check and read the same open file: replacing the pathname cannot substitute
    # an untrusted receipt after its owner was checked. NONBLOCK also prevents a
    # FIFO from hanging before fstat rejects it as a non-regular file.
    fd = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("managed memory receipt must be a root-owned, protected regular file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
            return json.load(fh)
    finally:
        os.close(fd)


def setup(root, receipt, uid, gid):
    verify_cgroup_mount()
    if "memory" not in (BASE / "cgroup.subtree_control").read_text().split():
        raise RuntimeError("host memory controller must be already enabled; refusing host-wide changes")
    if uid <= 0 or gid < 0:
        raise ValueError("delegation requires an unprivileged supervisor UID")
    if receipt.exists() or receipt.is_symlink():
        raise RuntimeError("managed memory receipt already exists")
    root.mkdir(mode=0o755)  # exclusive: never adopt another job's subtree
    try:
        info = root.stat()
        with receipt.open("x", encoding="utf-8") as fh:
            json.dump({"path": str(root), "device": info.st_dev, "inode": info.st_ino}, fh)
        receipt.chmod(0o644)
    except Exception:
        # No controls or descendants exist yet. Remove exactly the empty root
        # just created here, even when there is no receipt to authorize cleanup.
        root.rmdir()
        raise
    # The delegated parent remains empty; only leaves may contain processes.
    (root / "cgroup.subtree_control").write_text("+memory")
    if "memory" not in (root / "cgroup.subtree_control").read_text().split():
        raise RuntimeError("memory delegation readback failed")
    if not (root / "cgroup.kill").exists():
        raise RuntimeError("managed cleanup requires cgroup.kill")
    supervisor = root / ".supervisor"
    supervisor.mkdir(mode=0o755)
    for path in (root, root / "cgroup.procs", root / "cgroup.subtree_control",
                 supervisor / "cgroup.procs"):
        os.chown(path, uid, gid)
    print(root)


def _remove_children(fd):
    # cgroupfs has kernel-generated control files, removed automatically by rmdir.
    # Follow only directory descriptors beneath our verified, self-created root.
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            continue
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            _remove_children(child)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=fd)


def cleanup(root, receipt):
    verify_cgroup_mount()
    try:
        record = read_receipt(receipt)
    except FileNotFoundError:
        return  # setup never created a subtree; never adopt one based on its name
    if record.get("path") != str(root):
        raise RuntimeError("managed memory receipt names a different subtree")
    parent = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (record.get("device"), record.get("inode")):
                raise RuntimeError("managed memory subtree identity changed")
            control = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(control, "w") as fh:
                fh.write("1")
            for attempt in range(100):
                try:
                    _remove_children(fd)
                    os.rmdir(root.name, dir_fd=parent)
                    break
                except OSError:
                    if attempt == 99:
                        raise
                    time.sleep(0.05)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    receipt.unlink()


def probe():
    # Run as the ordinary job user, after its shell entered the supervisor leaf.
    # Exercise the exact engine wrapper rather than assuming an installed bwrap works.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import flexfactor_sandbox as sandbox
    if sandbox.capability_report()["strongest"] != "bwrap":
        raise RuntimeError("managed memory requires a successful bubblewrap namespace probe")
    code = "import pathlib; assert 'NoNewPrivs:\\t1' in pathlib.Path('/proc/self/status').read_text(); print('MEMORY_CONTAINED')"
    result = sandbox.run_contained([sys.executable, "-c", code], os.getcwd(),
                                   limits=sandbox.Limits(memory_bytes=128 * 1024 ** 2))
    level = result.flexfactor_containment["level"]
    if (result.returncode != 0 or level.get("memory_mechanism") != "cgroup-v2"
            or level.get("memory_cgroup_cleanup") != "removed"
            or "MEMORY_CONTAINED" not in result.stdout):
        raise RuntimeError(f"managed physical-memory probe failed: {level}; {result.stderr}")
    print("Managed bubblewrap and physical-memory containment verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("setup", "cleanup", "probe"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--job", default=os.environ.get("GITHUB_JOB", ""))
    parser.add_argument("--receipt")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args()
    if args.operation == "probe":
        probe()
        return
    root = group_path(args.run_id, args.attempt, args.job)
    if not args.receipt or not Path(args.receipt).is_absolute():
        parser.error("an absolute receipt path is required")
    if os.geteuid() != 0:
        parser.error("setup and cleanup require sudo; the engine must run unprivileged")
    if args.operation == "setup":
        setup(root, Path(args.receipt), args.uid or 0, args.gid if args.gid is not None else -1)
    else:
        cleanup(root, Path(args.receipt))


if __name__ == "__main__":
    main()
