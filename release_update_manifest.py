"""Build and validate FlexFactor's versioned cross-platform update manifest.

The manifest is an update *signal*, not executable authority. Each platform
must still verify its native package signature before installation.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = "flexfactor-update-v1"
REPOSITORY = "buckeye7066/flexfactor"
SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")


class ManifestError(ValueError):
    """The release signal is malformed or not bound to this project."""


def _release_apk(url: str, version: str) -> str:
    parsed = urlparse(url)
    expected = f"/{REPOSITORY}/releases/download/android-v{version}/flexfactor-{version}.apk"
    if (parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment) != (
            "https", "github.com", expected, "", ""):
        raise ManifestError("Android artifact is not the versioned FlexFactor GitHub release APK")
    return url


def validate_manifest(value: dict) -> dict:
    """Return ``value`` after enforcing the shared stable-channel contract."""
    if value.get("schema") != SCHEMA or value.get("channel") != "stable":
        raise ManifestError("unsupported update schema or channel")
    if value.get("status") != "active":
        raise ManifestError("release is not active")
    revision = value.get("sourceRevision", "")
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ManifestError("source revision must be a full lowercase Git SHA")
    platforms = value.get("platforms")
    if not isinstance(platforms, dict):
        raise ManifestError("platform map is missing")
    source = platforms.get("source")
    if not isinstance(source, dict) or source.get("repository") != REPOSITORY:
        raise ManifestError("source update repository is not canonical")
    if source.get("revision") != revision:
        raise ManifestError("source platform revision does not match the release")
    android = platforms.get("androidDirect")
    if not isinstance(android, dict):
        raise ManifestError("Android direct-update entry is missing")
    version = android.get("versionName")
    version_match = VERSION.fullmatch(version) if isinstance(version, str) else None
    if not version_match:
        raise ManifestError("Android version is invalid")
    if not isinstance(android.get("versionCode"), int) or android["versionCode"] <= 0:
        raise ManifestError("Android version code is invalid")
    digest = android.get("sha256", "")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ManifestError("Android digest is invalid")
    _release_apk(android.get("url", ""), version)
    legacy = {
        "packageName": android.get("packageName"),
        "versionCode": android.get("versionCode"),
        "versionName": version,
        "apkUrl": android.get("url"),
        "sha256": digest,
    }
    if any(value.get(key) != expected for key, expected in legacy.items()):
        raise ManifestError("legacy Android bootstrap fields do not match the platform entry")
    compatibility = value.get("compatibility")
    if not isinstance(compatibility, dict) or not compatibility.get("cloudServiceVersion"):
        raise ManifestError("cloud compatibility is missing")
    engine_ref = compatibility.get("engineRef", "")
    engine_match = re.fullmatch(r"android-v([0-9]+)\.([0-9]+)\.([0-9]+)", engine_ref)
    if not engine_match:
        raise ManifestError("cloud engine ref is invalid")
    app_parts = tuple(map(int, version_match.groups()))
    engine_parts = tuple(map(int, engine_match.groups()))
    if (engine_parts[:2] != app_parts[:2]
            or engine_parts[2] not in (app_parts[2], app_parts[2] - 1)):
        raise ManifestError("cloud engine is not compatible with the Android release")
    return value


def build_manifest(*, revision: str, version_name: str, version_code: int,
                   apk_url: str, apk_sha256: str, cloud_version: str,
                   engine_ref: str | None = None) -> dict:
    value = {
        "schema": SCHEMA,
        "channel": "stable",
        "status": "active",
        "sourceRevision": revision,
        "compatibility": {
            "cloudServiceVersion": cloud_version,
            "engineRef": engine_ref or f"android-v{version_name}",
        },
        "platforms": {
            "source": {"repository": REPOSITORY, "revision": revision},
            "androidDirect": {
                "packageName": "com.firer.console.flexfactor",
                "versionCode": version_code,
                "versionName": version_name,
                "url": apk_url,
                "sha256": apk_sha256.lower(),
            },
        },
        # Bootstrap fields keep already-installed pre-schema Android clients
        # able to discover the first unified-manifest release. New clients
        # require the versioned structure above and verify both copies agree.
        "packageName": "com.firer.console.flexfactor",
        "versionCode": version_code,
        "versionName": version_name,
        "apkUrl": apk_url,
        "sha256": apk_sha256.lower(),
    }
    return validate_manifest(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--version-code", required=True, type=int)
    parser.add_argument("--apk-url", required=True)
    parser.add_argument("--apk-sha256", required=True)
    parser.add_argument("--cloud-version", required=True)
    parser.add_argument("--engine-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = build_manifest(revision=args.revision, version_name=args.version_name,
                              version_code=args.version_code, apk_url=args.apk_url,
                              apk_sha256=args.apk_sha256, cloud_version=args.cloud_version,
                              engine_ref=args.engine_ref)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
