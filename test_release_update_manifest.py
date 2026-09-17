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

    def test_builds_one_signal_for_source_android_and_cloud(self):
        self.assertEqual(self.revision, self.manifest["platforms"]["source"]["revision"])
        self.assertEqual("android-v3.6.0", self.manifest["compatibility"]["engineRef"])
        self.assertEqual(30600, self.manifest["platforms"]["androidDirect"]["versionCode"])
        self.assertEqual(30600, self.manifest["versionCode"])

    def test_legacy_android_bootstrap_cannot_disagree(self):
        with self.assertRaisesRegex(ManifestError, "bootstrap"):
            validate_manifest(self.changed(("versionCode",), 99999))

    def test_withdrawn_release_is_not_offered(self):
        with self.assertRaisesRegex(ManifestError, "not active"):
            validate_manifest(self.changed(("status",), "withdrawn"))

    def test_mismatched_source_or_cloud_revision_is_rejected(self):
        for path in (("platforms", "source", "revision"),
                     ("compatibility", "engineRef")):
            with self.subTest(path=path), self.assertRaises(ManifestError):
                validate_manifest(self.changed(path, "android-v9.9.9"))

    def test_apk_url_is_bound_to_the_declared_version_and_repository(self):
        for url in (
            "https://github.com/buckeye7066/flexfactor/releases/download/android-v3.5.9/flexfactor-3.5.9.apk",
            "https://github.com/attacker/flexfactor/releases/download/android-v3.6.0/flexfactor-3.6.0.apk",
        ):
            with self.subTest(url=url), self.assertRaises(ManifestError):
                validate_manifest(self.changed(("platforms", "androidDirect", "url"), url))


if __name__ == "__main__":
    unittest.main()
