package com.firer.console.flexfactor;

import java.net.URI;
import java.util.Locale;

final class UpdatePolicy {
    static final String PACKAGE_NAME = "com.firer.console.flexfactor";
    static final String MANIFEST_URL =
            "https://github.com/buckeye7066/flexfactor/releases/latest/download/android-update.json";

    private UpdatePolicy() {}

    static URI requireReleaseApk(String value) {
        URI uri = requireHttps(value);
        if (!"github.com".equalsIgnoreCase(uri.getHost())
                || uri.getRawQuery() != null
                || uri.getRawFragment() != null
                || !uri.getPath().matches(
                        "^/buckeye7066/flexfactor/releases/download/android-v[^/]+/flexfactor-[^/]+\\.apk$")) {
            throw new IllegalArgumentException("The update APK is not an approved FlexFactor release.");
        }
        return uri;
    }

    static URI requireTrustedTransport(String value) {
        URI uri = requireHttps(value);
        String host = uri.getHost().toLowerCase(Locale.ROOT);
        if (!host.equals("github.com")
                && !host.equals("objects.githubusercontent.com")
                && !host.equals("release-assets.githubusercontent.com")) {
            throw new IllegalArgumentException("The update download left GitHub's trusted hosts.");
        }
        return uri;
    }

    static String requireSha256(String value) {
        String normalized = value == null ? "" : value.toLowerCase(Locale.ROOT);
        if (!normalized.matches("^[0-9a-f]{64}$")) {
            throw new IllegalArgumentException("The update manifest has an invalid SHA-256.");
        }
        return normalized;
    }

    static boolean isNewer(long candidate, long installed) {
        return candidate > installed;
    }

    static String requireRevision(String value) {
        String normalized = value == null ? "" : value.toLowerCase(Locale.ROOT);
        if (!normalized.matches("^[0-9a-f]{40}$")) {
            throw new IllegalArgumentException("The update manifest has an invalid source revision.");
        }
        return normalized;
    }

    static void requireVersionedApk(URI uri, String versionName) {
        String expected = "/buckeye7066/flexfactor/releases/download/android-v"
                + versionName + "/flexfactor-" + versionName + ".apk";
        if (!expected.equals(uri.getPath())) {
            throw new IllegalArgumentException("The update APK does not match the declared version.");
        }
    }

    static String requireCompatibleEngineRef(String value, String appVersion) {
        if (value == null || !value.matches("^android-v[0-9]+\\.[0-9]+\\.[0-9]+$")) {
            throw new IllegalArgumentException("The update manifest has an invalid cloud engine ref.");
        }
        if (appVersion == null || !appVersion.matches("^[0-9]+\\.[0-9]+\\.[0-9]+$")) {
            throw new IllegalArgumentException("The update manifest has an invalid app version.");
        }
        String[] engine = value.substring("android-v".length()).split("\\.");
        String[] app = appVersion.split("\\.");
        long enginePatch = Long.parseLong(engine[2]);
        long appPatch = Long.parseLong(app[2]);
        if (!engine[0].equals(app[0]) || !engine[1].equals(app[1])
                || (enginePatch != appPatch && enginePatch != appPatch - 1)) {
            throw new IllegalArgumentException("The cloud engine is not compatible with this app release.");
        }
        return value;
    }

    private static URI requireHttps(String value) {
        try {
            URI uri = URI.create(value);
            if (!"https".equalsIgnoreCase(uri.getScheme())
                    || uri.getHost() == null
                    || uri.getUserInfo() != null) {
                throw new IllegalArgumentException("Update downloads must use HTTPS.");
            }
            return uri;
        } catch (RuntimeException invalid) {
            if (invalid instanceof IllegalArgumentException
                    && "Update downloads must use HTTPS.".equals(invalid.getMessage())) {
                throw invalid;
            }
            throw new IllegalArgumentException("The update manifest contains an invalid URL.");
        }
    }
}
