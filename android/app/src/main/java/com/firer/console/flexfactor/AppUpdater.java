package com.firer.console.flexfactor;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class AppUpdater {
    private static final int MAX_REDIRECTS = 5;
    private static final int MAX_MANIFEST_BYTES = 64 * 1024;
    private static final long MAX_APK_BYTES = 100L * 1024L * 1024L;

    interface Callback {
        void onUpToDate(String versionName);
        void onInstallerReady(String versionName);
        void onError(String message);
    }

    interface CheckCallback {
        void onUpToDate(String versionName);
        void onUpdateAvailable(String versionName);
        void onError(String message);
    }

    private final Context context;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    AppUpdater(Context context) {
        this.context = context.getApplicationContext();
    }

    void check(CheckCallback callback) {
        worker.execute(() -> {
            try {
                UpdateInfo update = fetchManifest();
                PackageInfo installed = installedPackage();
                if (UpdatePolicy.isNewer(update.versionCode, versionCode(installed))) {
                    post(() -> callback.onUpdateAvailable(update.versionName));
                } else {
                    String installedName = installed.versionName == null
                            ? "unknown" : installed.versionName;
                    post(() -> callback.onUpToDate(installedName));
                }
            } catch (Exception failed) {
                String detail = failed.getMessage();
                if (detail == null || detail.trim().isEmpty()) {
                    detail = failed.getClass().getSimpleName();
                }
                String message = detail;
                post(() -> callback.onError(message));
            } finally {
                worker.shutdown();
            }
        });
    }

    void checkAndInstall(Callback callback) {
        worker.execute(() -> {
            File apk = null;
            try {
                UpdateInfo update = fetchManifest();
                PackageInfo installed = installedPackage();
                if (!UpdatePolicy.isNewer(update.versionCode, versionCode(installed))) {
                    String installedName = installed.versionName == null ? "unknown" : installed.versionName;
                    post(() -> callback.onUpToDate(installedName));
                    return;
                }
                apk = File.createTempFile("flexfactor-update-", ".apk", context.getCacheDir());
                download(update.apkUri, apk, MAX_APK_BYTES);
                verifySha256(apk, update.sha256);
                verifyArchive(apk, update);
                install(apk);
                post(() -> callback.onInstallerReady(update.versionName));
            } catch (Exception failed) {
                String detail = failed.getMessage();
                if (detail == null || detail.trim().isEmpty()) detail = failed.getClass().getSimpleName();
                String message = detail;
                post(() -> callback.onError(message));
            } finally {
                if (apk != null && apk.exists()) apk.delete();
                worker.shutdown();
            }
        });
    }

    private UpdateInfo fetchManifest() throws Exception {
        byte[] body = readBytes(
                UpdatePolicy.requireTrustedTransport(UpdatePolicy.MANIFEST_URL),
                MAX_MANIFEST_BYTES);
        JSONObject json = new JSONObject(new String(body, StandardCharsets.UTF_8));
        String packageName = json.getString("packageName");
        if (!UpdatePolicy.PACKAGE_NAME.equals(packageName)) {
            throw new IllegalArgumentException("The update manifest names a different app.");
        }
        long versionCode = json.getLong("versionCode");
        String versionName = json.getString("versionName").trim();
        if (versionCode <= 0 || versionName.isEmpty()) {
            throw new IllegalArgumentException("The update manifest has an invalid version.");
        }
        URI apkUri = UpdatePolicy.requireReleaseApk(json.getString("apkUrl"));
        String sha256 = UpdatePolicy.requireSha256(json.getString("sha256"));
        return new UpdateInfo(versionCode, versionName, apkUri, sha256);
    }

    private byte[] readBytes(URI uri, int limit) throws Exception {
        HttpURLConnection connection = open(uri);
        try (InputStream input = connection.getInputStream();
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            copyLimited(input, output, limit);
            return output.toByteArray();
        } finally {
            connection.disconnect();
        }
    }

    private void download(URI uri, File destination, long limit) throws Exception {
        HttpURLConnection connection = open(uri);
        try (InputStream input = connection.getInputStream();
             OutputStream output = new FileOutputStream(destination)) {
            copyLimited(input, output, limit);
        } finally {
            connection.disconnect();
        }
    }

    private HttpURLConnection open(URI initial) throws Exception {
        URI current = initial;
        for (int redirect = 0; redirect <= MAX_REDIRECTS; redirect++) {
            HttpURLConnection connection = (HttpURLConnection) current.toURL().openConnection();
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(30_000);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("Accept", "application/octet-stream, application/json");
            connection.setRequestProperty("User-Agent", "FlexFactor-Android");
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) {
                String location = connection.getHeaderField("Location");
                connection.disconnect();
                if (location == null || redirect == MAX_REDIRECTS) {
                    throw new IllegalStateException("The update download returned too many redirects.");
                }
                current = UpdatePolicy.requireTrustedTransport(current.resolve(location).toString());
                continue;
            }
            if (status != HttpURLConnection.HTTP_OK) {
                connection.disconnect();
                throw new IllegalStateException("The update server returned HTTP " + status + ".");
            }
            return connection;
        }
        throw new IllegalStateException("The update download could not be opened.");
    }

    private void copyLimited(InputStream input, OutputStream output, long limit) throws Exception {
        byte[] buffer = new byte[32 * 1024];
        long total = 0;
        int read;
        while ((read = input.read(buffer)) != -1) {
            total += read;
            if (total > limit) throw new IllegalStateException("The update download is unexpectedly large.");
            output.write(buffer, 0, read);
        }
    }

    private void verifySha256(File apk, String expected) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new FileInputStream(apk)) {
            byte[] buffer = new byte[32 * 1024];
            int read;
            while ((read = input.read(buffer)) != -1) digest.update(buffer, 0, read);
        }
        StringBuilder actual = new StringBuilder(64);
        for (byte value : digest.digest()) actual.append(String.format(Locale.ROOT, "%02x", value));
        if (!expected.equals(actual.toString())) {
            throw new SecurityException("The downloaded APK failed its SHA-256 check.");
        }
    }

    private void verifyArchive(File apk, UpdateInfo update) throws Exception {
        PackageManager manager = context.getPackageManager();
        PackageInfo archive = manager.getPackageArchiveInfo(
                apk.getAbsolutePath(), signingFlags());
        if (archive == null || !UpdatePolicy.PACKAGE_NAME.equals(archive.packageName)) {
            throw new SecurityException("The downloaded APK is not FlexFactor.");
        }
        if (versionCode(archive) != update.versionCode
                || archive.versionName == null
                || !archive.versionName.equals(update.versionName)) {
            throw new SecurityException("The downloaded APK version does not match its manifest.");
        }
        PackageInfo installed = installedPackage();
        Set<String> installedCertificates = certificateDigests(installed);
        Set<String> archiveCertificates = certificateDigests(archive);
        archiveCertificates.retainAll(installedCertificates);
        if (archiveCertificates.isEmpty()) {
            throw new SecurityException("The update is not signed by FlexFactor's trusted key.");
        }
    }

    private Set<String> certificateDigests(PackageInfo info) throws Exception {
        Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            if (info.signingInfo == null) {
                throw new SecurityException("The APK has no signing certificate.");
            }
            signatures = info.signingInfo.hasMultipleSigners()
                    ? info.signingInfo.getApkContentsSigners()
                    : info.signingInfo.getSigningCertificateHistory();
        } else {
            signatures = info.signatures;
        }
        if (signatures == null || signatures.length == 0) {
            throw new SecurityException("The APK has no signing certificate.");
        }
        Set<String> result = new HashSet<>();
        for (Signature signature : signatures) {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(signature.toByteArray());
            StringBuilder value = new StringBuilder(64);
            for (byte item : digest) value.append(String.format(Locale.ROOT, "%02x", item));
            result.add(value.toString());
        }
        return result;
    }

    private PackageInfo installedPackage() throws PackageManager.NameNotFoundException {
        return context.getPackageManager().getPackageInfo(
                context.getPackageName(), signingFlags());
    }

    @SuppressWarnings("deprecation")
    private int signingFlags() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES
                : PackageManager.GET_SIGNATURES;
    }

    @SuppressWarnings("deprecation")
    private long versionCode(PackageInfo info) {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? info.getLongVersionCode()
                : info.versionCode;
    }

    private void install(File apk) throws Exception {
        PackageInstaller installer = context.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(UpdatePolicy.PACKAGE_NAME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_REQUIRED);
        }
        int sessionId = installer.createSession(params);
        boolean committed = false;
        try (PackageInstaller.Session session = installer.openSession(sessionId)) {
            try (InputStream input = new FileInputStream(apk);
                 OutputStream output = session.openWrite("flexfactor.apk", 0, apk.length())) {
                copyLimited(input, output, MAX_APK_BYTES);
                session.fsync(output);
            }
            Intent result = new Intent(context, UpdateResultReceiver.class)
                    .setAction(UpdatePolicy.PACKAGE_NAME + ".UPDATE_STATUS." + sessionId);
            int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                pendingFlags |= PendingIntent.FLAG_MUTABLE;
            }
            PendingIntent pending = PendingIntent.getBroadcast(
                    context,
                    sessionId,
                    result,
                    pendingFlags);
            session.commit(pending.getIntentSender());
            committed = true;
        } finally {
            if (!committed) installer.abandonSession(sessionId);
        }
    }

    private void post(Runnable action) {
        main.post(action);
    }

    private static final class UpdateInfo {
        final long versionCode;
        final String versionName;
        final URI apkUri;
        final String sha256;

        UpdateInfo(long versionCode, String versionName, URI apkUri, String sha256) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.apkUri = apkUri;
            this.sha256 = sha256;
        }
    }
}
