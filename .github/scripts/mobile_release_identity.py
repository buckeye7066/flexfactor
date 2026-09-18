"""Bind a protected live proof to the signed runtime and current cloud rollout."""
from pathlib import Path
import re
import subprocess

# Proof maintenance is not an Android/engine release. Every other non-cloud
# change, including runner workflows and release tooling, requires publication.
PROOF_MAINTENANCE = frozenset({
    ".github/scripts/mobile_cloud_live_proof.py",
    ".github/scripts/mobile_release_identity.py",
    ".github/workflows/mobile-cloud-live-proof.yml",
    "test_android_standalone.py",
})


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=root, text=True,
            stderr=subprocess.STDOUT, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError("Could not verify release git identity") from error


def verify_release_identity(root: Path, expected_sha: str,
                            released: dict, source_version: str) -> dict:
    root = Path(root).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Invalid authorized verification revision")
    if _git(root, "rev-parse", "HEAD").strip() != expected_sha:
        raise ValueError("Checkout is not the authorized verification revision")
    if (not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", source_version)
            or released.get("versionName") != source_version
            or released.get("schema") != "flexfactor-update-v1"
            or released.get("channel") != "stable"
            or released.get("status") != "active"):
        raise ValueError("The published Android identity is not active and compatible")
    tag = "android-v" + source_version
    tagged_sha = _git(root, "rev-parse", "refs/tags/" + tag + "^{commit}").strip()
    release_sha = released.get("sourceRevision")
    if release_sha != tagged_sha:
        raise ValueError("Manifest source differs from the exact release tag")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", release_sha, expected_sha],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    if ancestor.returncode != 0:
        raise ValueError("The signed release is not an ancestor of authorized main")
    changed = _git(root, "diff", "--name-only", "-z", release_sha, expected_sha)
    unpublished = [name for name in changed.split("\0") if name
                   and not name.startswith("cloud/")
                   and name not in PROOF_MAINTENANCE]
    if unpublished:
        raise ValueError("The proof has unreleased runtime changes: " + repr(unpublished[:6]))
    if (root / "cloud/ENGINE_ROLLOUT_PENDING").exists():
        raise ValueError("The cloud engine rollout is still pending")
    config = (root / "cloud/lib/config.js").read_text(encoding="utf-8")
    engine = re.search(r'ENGINE_REF = "([^"]+)"', config)
    version = re.search(r'SERVICE_VERSION = "([^"]+)"', config)
    if engine is None or engine.group(1) != tag or version is None:
        raise ValueError("The configured cloud engine is not the published Android engine")
    return {"release_source_sha": release_sha, "verification_sha": expected_sha,
            "engine_ref": tag, "cloud_version": version.group(1)}


def verify_cloud_health(health: dict, identity: dict) -> None:
    if (health.get("ok") is not True
            or health.get("oauth_device_configured") is not True
            or health.get("version") != identity["cloud_version"]
            or health.get("engine_ref") != identity["engine_ref"]):
        raise ValueError("The deployed cloud does not match the verified release rollout")
