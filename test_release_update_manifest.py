import copy
import unittest

from release_update_manifest import ManifestError, build_manifest, validate_manifest


class ReleaseUpdateManifestTests(unittest.TestCase):
    def setUp(self):
        self.revision = "a" * 40
        self.manifest = build_manifest(
            revision=self.revision, version_name="3.6.0", version_code=30600,
            apk_url="https://github.com/buckeye7066/flexfactor/releases/download/android-v3.6.0/flexfactor-3.6.0.apk",
            apk_sha256="b" * 64, cloud_version="1.1.5")

    def changed(self, path, value):
        result = copy.deepcopy(self.manifest)
        target = result
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        return result

    def test_builds_one_signal_for_android_and_cloud(self):
        self.assertNotIn("source", self.manifest["platforms"])
        self.assertEqual("android-v3.6.0", self.manifest["compatibility"]["engineRef"])
        self.assertEqual(30600, self.manifest["platforms"]["androidDirect"]["versionCode"])
        self.assertEqual(30600, self.manifest["versionCode"])

    def test_legacy_android_bootstrap_cannot_disagree(self):
        with self.assertRaisesRegex(ManifestError, "bootstrap"):
            validate_manifest(self.changed(("versionCode",), 99999))

    def test_withdrawn_release_is_not_offered(self):
        with self.assertRaisesRegex(ManifestError, "not active"):
            validate_manifest(self.changed(("status",), "withdrawn"))

    def test_malformed_source_revision_is_rejected(self):
        with self.assertRaises(ManifestError):
            validate_manifest(self.changed(("sourceRevision",), "c" * 39))

    def test_current_or_one_patch_staged_cloud_engine_is_compatible(self):
        staged = build_manifest(
            revision=self.revision, version_name="3.5.10", version_code=30510,
            apk_url="https://github.com/buckeye7066/flexfactor/releases/download/android-v3.5.10/flexfactor-3.5.10.apk",
            apk_sha256="b" * 64, cloud_version="1.1.5",
            engine_ref="android-v3.5.9")
        validate_manifest(staged)
        for engine in ("android-v3.5.8", "android-v3.6.0", "main", "android-v4.0.0"):
            with self.subTest(engine=engine), self.assertRaises(ManifestError):
                rejected = copy.deepcopy(staged)
                rejected["compatibility"]["engineRef"] = engine
                validate_manifest(rejected)

    def test_apk_url_is_bound_to_the_declared_version_and_repository(self):
        for url in (
            "https://github.com/buckeye7066/flexfactor/releases/download/android-v3.5.9/flexfactor-3.5.9.apk",
            "https://github.com/attacker/flexfactor/releases/download/android-v3.6.0/flexfactor-3.6.0.apk",
        ):
            with self.subTest(url=url), self.assertRaises(ManifestError):
                validate_manifest(self.changed(("platforms", "androidDirect", "url"), url))


if __name__ == "__main__":
    unittest.main()
