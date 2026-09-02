package com.firer.console.flexfactor;

import android.util.Base64;

import com.goterl.lazysodium.LazySodiumAndroid;
import com.goterl.lazysodium.SodiumAndroid;
import com.goterl.lazysodium.interfaces.Box;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Domain client for FlexFactor Cloud. The APK contains no direct GitHub API fallback. */
final class GitHubApi {
    static final String CONTROL_REPOSITORY = "buckeye7066/flexfactor";
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final int DEFAULT_READ_TIMEOUT_MS = 60_000;
    // Vercel's dispatch function has a 300-second execution ceiling. Waiting
    // beyond that contract prevents a successful, accepted run from looking
    // like a client timeout and being submitted a second time.
    private static final int DISPATCH_READ_TIMEOUT_MS = 330_000;
    private static final int MAX_JSON_BYTES = 2 * 1024 * 1024;
    private static final int MAX_PHONE_ARTIFACT_BYTES = 2 * 1024 * 1024;
    private static final int MAX_PHONE_ENTRY_BYTES = 256 * 1024;
    private static final int MAX_PHONE_EXTRACTED_BYTES = 768 * 1024;
    private static final int MAX_ZIP_ENTRIES = 64;
    private static final int SODIUM_PUBLIC_KEY_BYTES = 32;

    static final class ConfigurationResult {
        final String login;
        ConfigurationResult(String login) { this.login = login; }
    }

    static final class DeviceAuthorization {
        final String deviceCode;
        final String userCode;
        final String verificationUri;
        final long expiresAt;
        final int intervalSeconds;

        DeviceAuthorization(String deviceCode, String userCode, String verificationUri,
                long expiresAt, int intervalSeconds) {
            this.deviceCode = deviceCode;
            this.userCode = userCode;
            this.verificationUri = verificationUri;
            this.expiresAt = expiresAt;
            this.intervalSeconds = intervalSeconds;
        }
    }

    static final class OAuthToken {
        final String accessToken;
        final String refreshToken;
        final long expiresAt;

        OAuthToken(String accessToken, String refreshToken, long expiresAt) {
            this.accessToken = accessToken;
            this.refreshToken = refreshToken;
            this.expiresAt = expiresAt;
        }
    }

    static final class AuthorizationPendingException extends Exception {
        final boolean slowDown;
        AuthorizationPendingException(boolean slowDown) {
            super(slowDown ? "GitHub asked FlexFactor to poll more slowly." : "Authorization pending.");
            this.slowDown = slowDown;
        }
    }

    static final class Repository {
        final String fullName;
        final String defaultBranch;
        final boolean isPrivate;

        Repository(String fullName, String defaultBranch, boolean isPrivate) {
            this.fullName = fullName;
            this.defaultBranch = defaultBranch;
            this.isPrivate = isPrivate;
        }
    }

    static final class RunState {
        final long id;
        final String status;
        final String conclusion;
        final String htmlUrl;
        final String currentStep;

        RunState(long id, String status, String conclusion, String htmlUrl, String currentStep) {
            this.id = id;
            this.status = status;
            this.conclusion = conclusion;
            this.htmlUrl = htmlUrl;
            this.currentStep = currentStep;
        }

        boolean complete() { return "completed".equals(status); }
    }

    static final class RunDetails {
        final String result;
        final String errors;
        final String status;

        RunDetails(String result, String errors, String status) {
            this.result = result;
            this.errors = errors;
            this.status = status;
        }

        String displayText() {
            StringBuilder text = new StringBuilder();
            if (!result.isEmpty()) {
                try {
                    JSONObject row = new JSONObject(result);
                    boolean publicationRequired = row.optBoolean(
                            "publication_required", false);
                    boolean publicationComplete = row.optBoolean(
                            "publication_complete", !publicationRequired);
                    text.append(row.optBoolean("success", false) && publicationComplete
                            ? "Run completed successfully." : "Run did not complete successfully.");
                    text.append("\nRepository: ").append(row.optString("target_repository", "unknown"));
                    text.append("\nMode: ").append(row.optString("mode", "unknown"));
                    text.append("\nExit code: ").append(row.optInt("exit_code", -1));
                    if (publicationRequired) {
                        text.append("\nRemote default branch: ")
                                .append(row.optString("default_branch", "unresolved"));
                        text.append("\nPublication: ")
                                .append(publicationComplete ? "verified" : "incomplete");
                    }
                } catch (JSONException ignored) {
                    text.append(result.trim());
                }
            }
            if (!errors.trim().isEmpty()) {
                if (text.length() > 0) text.append("\n\n");
                text.append("Error ledger\n\n").append(errors.trim());
            }
            if (text.length() == 0 && !status.trim().isEmpty()) {
                text.append("Latest engine status\n\n").append(status.trim());
            }
            return text.length() == 0
                    ? "This run did not publish phone-readable details." : text.toString();
        }
    }

    ConfigurationResult configure(String githubToken) throws Exception {
        JSONObject response = cloudJson(
                githubToken, "POST", "/api/configure", new JSONObject(), false);
        String login = response.optString("login", "").trim();
        if (login.isEmpty()) throw new ApiException("FlexFactor Cloud did not identify this account.");
        return new ConfigurationResult(login);
    }

    ConfigurationResult configure(String githubToken, String openAiKey, String anthropicKey)
            throws Exception {
        ConfigurationResult configured = configure(githubToken);
        validateProviderKeys(openAiKey, anthropicKey);
        return configured;
    }

    DeviceAuthorization beginDeviceAuthorization() throws Exception {
        JSONObject response = cloudJson("", "POST", "/api/oauth/device", new JSONObject(), true);
        return requireDeviceAuthorization(response, System.currentTimeMillis());
    }

    static DeviceAuthorization requireDeviceAuthorization(JSONObject response, long now)
            throws Exception {
        String deviceCode = response.optString("device_code", "").trim();
        String userCode = response.optString("user_code", "").trim();
        String verificationUri = response.optString("verification_uri", "").trim();
        int expiresIn = response.optInt("expires_in", 0);
        int interval = Math.max(5, response.optInt("interval", 5));
        if (deviceCode.isEmpty() || userCode.isEmpty()
                || !"https://github.com/login/device".equals(verificationUri)
                || expiresIn <= 0) {
            throw new ApiException("FlexFactor Cloud returned an incomplete device sign-in response.");
        }
        return new DeviceAuthorization(deviceCode, userCode, verificationUri,
                now + expiresIn * 1000L, interval);
    }

    OAuthToken pollDeviceAuthorization(DeviceAuthorization authorization) throws Exception {
        JSONObject body = new JSONObject();
        body.put("device_code", authorization.deviceCode);
        HttpResult result = cloudRaw("", "POST", "/api/oauth/token", body, true);
        JSONObject response = json(result);
        if (result.status == 202) {
            String error = response.optString("error", "");
            if ("authorization_pending".equals(error)) throw new AuthorizationPendingException(false);
            if ("slow_down".equals(error)) throw new AuthorizationPendingException(true);
        }
        requireSuccess(result, response);
        return requireOAuthToken(response);
    }

    OAuthToken refreshOAuthToken(String refreshToken) throws Exception {
        JSONObject body = new JSONObject();
        body.put("refresh_token", requireSecret(refreshToken, "GitHub refresh token"));
        return requireOAuthToken(cloudJson("", "POST", "/api/oauth/refresh", body, false));
    }

    static OAuthToken requireOAuthToken(JSONObject response) throws Exception {
        String access = response.optString("access_token", "").trim();
        String refresh = response.optString("refresh_token", "").trim();
        long expiresIn = response.optLong("expires_in", 0L);
        if (access.isEmpty()) throw new ApiException("FlexFactor Cloud did not return an access token.");
        if (expiresIn > 0 && refresh.isEmpty()) {
            throw new ApiException("FlexFactor Cloud returned an expiring session without a refresh token.");
        }
        long expiresAt = expiresIn <= 0 ? Long.MAX_VALUE
                : System.currentTimeMillis() + expiresIn * 1000L;
        return new OAuthToken(access, refresh, expiresAt);
    }

    List<Repository> repositories(String token) throws Exception {
        List<Repository> repositories = new ArrayList<>();
        Set<String> found = new HashSet<>();
        for (int page = 1; page <= 100; page++) {
            JSONObject response = cloudJson(token, "GET", "/api/repositories?page=" + page,
                    null, true);
            JSONArray rows = response.optJSONArray("repositories");
            if (rows == null || response.optInt("page", 0) != page) {
                throw new ApiException("FlexFactor Cloud returned an invalid repository page.");
            }
            for (int i = 0; i < rows.length(); i++) {
                JSONObject row = rows.getJSONObject(i);
                String fullName = row.optString("full_name", "");
                if (!fullName.isEmpty() && found.add(fullName)) {
                    repositories.add(new Repository(fullName,
                            row.optString("default_branch", "main"),
                            row.optBoolean("private", false)));
                }
            }
            if (!response.optBoolean("has_more", false)) return repositories;
        }
        throw new ApiException("The repository list exceeded FlexFactor's page limit.");
    }

    RunState dispatch(String token, String openAiKey, String anthropicKey,
            MobileRunRequest request) throws Exception {
        String cleanToken = requireSecret(token, "GitHub session");
        JSONObject body = new JSONObject();
        body.put("request", runRequest(request));
        body.put("encrypted_secrets", encryptedProviderSecrets(cleanToken, request,
                openAiKey, anthropicKey));
        return runState(cloudJson(cleanToken, "POST", "/api/runs/dispatch", body, false));
    }

    RunState run(String token, String repository, long runId) throws Exception {
        validateRunIdentity(repository, runId);
        return runState(cloudJson(token, "GET", "/api/runs/status?repository="
                + encode(repository) + "&run_id=" + runId, null, true));
    }

    RunDetails runDetails(String token, String repository, long runId) throws Exception {
        validateRunIdentity(repository, runId);
        byte[] archive = cloudBytes(token, "/api/runs/details?repository="
                + encode(repository) + "&run_id=" + runId);
        String result = "";
        String errors = "";
        String status = "";
        int entryCount = 0;
        int extractedBytes = 0;
        Set<String> found = new HashSet<>();
        try (ZipInputStream zip = new ZipInputStream(new ByteArrayInputStream(archive))) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                entryCount++;
                if (entryCount > MAX_ZIP_ENTRIES) {
                    throw new ApiException("The run detail archive contains too many entries.");
                }
                if (entry.isDirectory()) continue;
                String name = entry.getName().replace('\\', '/');
                if (name.contains("../") || name.startsWith("/")) continue;
                String base = name.substring(name.lastIndexOf('/') + 1);
                if (!"mobile-result.json".equals(base)
                        && !"errors.md".equals(base)
                        && !"status.json".equals(base)) continue;
                if (!found.add(base)) {
                    throw new ApiException("The run detail archive contains a duplicate entry.");
                }
                byte[] raw = readZipEntryLimited(zip, MAX_PHONE_ENTRY_BYTES);
                extractedBytes += raw.length;
                if (extractedBytes > MAX_PHONE_EXTRACTED_BYTES) {
                    throw new ApiException("The run detail archive expands beyond its limit.");
                }
                String value = new String(raw, StandardCharsets.UTF_8);
                if ("mobile-result.json".equals(base)) result = value;
                if ("errors.md".equals(base)) errors = value;
                if ("status.json".equals(base)) status = value;
            }
        }
        return new RunDetails(result, errors, status);
    }

    void submitSteering(String token, String repository, String requestId, String comment)
            throws Exception {
        JSONObject body = new JSONObject();
        body.put("repository", repository);
        body.put("request_id", requestId);
        body.put("comment", comment);
        cloudJson(token, "POST", "/api/runs/steer", body, false);
    }

    private JSONObject encryptedProviderSecrets(String token, MobileRunRequest request,
            String openAiKey, String anthropicKey) throws Exception {
        String openAi = cleanSecret(openAiKey);
        String anthropic = cleanSecret(anthropicKey);
        boolean sendOpenAi = !openAi.isEmpty();
        boolean sendAnthropic = !anthropic.isEmpty();
        if (!sendOpenAi && !sendAnthropic) {
            return new JSONObject();
        }
        // The one ladder may need either paid account before reaching its free
        // fallback, so every configured key is sent—still sealed directly to
        // the selected repository's Actions public key.
        validateProviderKeys(sendOpenAi ? openAi : "", sendAnthropic ? anthropic : "");
        JSONObject key = cloudJson(token, "GET", "/api/provider-key?repository="
                + encode(request.repository), null, true);
        byte[] publicKey;
        try { publicKey = Base64.decode(key.getString("key"), Base64.DEFAULT); }
        catch (Exception failed) {
            throw new ApiException("FlexFactor Cloud returned an invalid repository encryption key.");
        }
        if (publicKey.length != SODIUM_PUBLIC_KEY_BYTES) {
            throw new ApiException("FlexFactor Cloud returned an invalid repository encryption key.");
        }
        String keyId = key.optString("key_id", "").trim();
        if (keyId.isEmpty()) {
            throw new ApiException("FlexFactor Cloud returned an incomplete repository encryption key.");
        }
        JSONObject encrypted = new JSONObject();
        if (sendOpenAi && !openAi.isEmpty()) {
            encrypted.put("OPENAI_API_KEY", seal(openAi, keyId, publicKey));
        }
        if (sendAnthropic && !anthropic.isEmpty()) {
            encrypted.put("ANTHROPIC_API_KEY", seal(anthropic, keyId, publicKey));
        }
        return encrypted;
    }

    private void validateProviderKeys(String openAiKey, String anthropicKey) throws Exception {
        String openAi = cleanSecret(openAiKey);
        String anthropic = cleanSecret(anthropicKey);
        if (!openAi.isEmpty()) verifyOpenAi(openAi);
        if (!anthropic.isEmpty()) verifyAnthropic(anthropic);
    }

    private void verifyOpenAi(String key) throws Exception {
        HttpURLConnection connection = connection(
                new URL("https://api.openai.com/v1/models"), DEFAULT_READ_TIMEOUT_MS);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("Authorization", "Bearer " + key);
        HttpResult result = execute(connection, null);
        if (result.status != 200) {
            throw new ApiException("OpenAI rejected this key (HTTP " + result.status + ").");
        }
    }

    private void verifyAnthropic(String key) throws Exception {
        HttpURLConnection connection = connection(
                new URL("https://api.anthropic.com/v1/models"), DEFAULT_READ_TIMEOUT_MS);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("x-api-key", key);
        connection.setRequestProperty("anthropic-version", "2023-06-01");
        HttpResult result = execute(connection, null);
        if (result.status != 200) {
            throw new ApiException("Anthropic rejected this key (HTTP " + result.status + ").");
        }
    }

    private JSONObject seal(String value, String keyId, byte[] publicKey) throws Exception {
        byte[] message = value.getBytes(StandardCharsets.UTF_8);
        byte[] cipher = new byte[message.length + Box.SEALBYTES];
        LazySodiumAndroid sodium = new LazySodiumAndroid(new SodiumAndroid());
        if (!sodium.cryptoBoxSeal(cipher, message, message.length, publicKey)) {
            throw new ApiException("Could not seal the provider credential.");
        }
        JSONObject result = new JSONObject();
        result.put("key_id", keyId);
        result.put("encrypted_value", Base64.encodeToString(cipher, Base64.NO_WRAP));
        return result;
    }

    private static JSONObject runRequest(MobileRunRequest request) throws Exception {
        JSONObject body = new JSONObject();
        for (Map.Entry<String, String> entry : request.workflowInputs().entrySet()) {
            String key = entry.getKey();
            if ("target_repository".equals(key)) key = "repository";
            if ("target_ref".equals(key)) key = "ref";
            String value = entry.getValue();
            if ("scout_apply".equals(key)) {
                body.put(key, Boolean.parseBoolean(value));
            } else if ("max_cost".equals(key)) {
                body.put(key, Double.parseDouble(value));
            } else if ("threshold".equals(key) || "max_iterations".equals(key)) {
                body.put(key, Integer.parseInt(value));
            } else body.put(key, value);
        }
        return body;
    }

    private static RunState runState(JSONObject response) throws Exception {
        long id = response.optLong("id", 0L);
        if (id <= 0) throw new ApiException("FlexFactor Cloud returned an invalid run identifier.");
        return new RunState(id, response.optString("status", "unknown"),
                response.optString("conclusion", ""), response.optString("html_url", ""),
                response.optString("step", "unknown"));
    }

    private static void validateRunIdentity(String repository, long runId) {
        if (runId <= 0) throw new IllegalArgumentException("Run ID is invalid.");
        if (repository == null || !repository.matches("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")) {
            throw new IllegalArgumentException("Run repository is invalid.");
        }
    }

    private JSONObject cloudJson(String token, String method, String path, JSONObject body,
            boolean retrySafe) throws Exception {
        HttpResult result = cloudRaw(token, method, path, body, retrySafe);
        JSONObject response = json(result);
        requireSuccess(result, response);
        return response;
    }

    private byte[] cloudBytes(String token, String path) throws Exception {
        HttpResult result = cloudRaw(token, "GET", path, null, true);
        if (result.status < 200 || result.status >= 300) throw cloudException(result, json(result));
        if (!result.contentType.toLowerCase().startsWith("application/zip")) {
            throw new ApiException("FlexFactor Cloud returned an invalid run artifact.");
        }
        return result.body;
    }

    private HttpResult cloudRaw(String token, String method, String path, JSONObject body,
            boolean retrySafe) throws Exception {
        URL url = cloudUrl(path);
        byte[] payload = body == null ? null : body.toString().getBytes(StandardCharsets.UTF_8);
        HttpResult latest = null;
        int attempts = retrySafe ? 3 : 1;
        for (int attempt = 0; attempt < attempts; attempt++) {
            int readTimeout = "/api/runs/dispatch".equals(path)
                    ? DISPATCH_READ_TIMEOUT_MS : DEFAULT_READ_TIMEOUT_MS;
            HttpURLConnection connection = connection(url, readTimeout);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json, application/zip");
            connection.setRequestProperty("X-FlexFactor-Client-Version", BuildConfig.VERSION_NAME);
            String cleanToken = token == null ? "" : token.trim();
            if (!cleanToken.isEmpty()) connection.setRequestProperty("Authorization", "Bearer "
                    + requireSecret(cleanToken, "GitHub session"));
            latest = execute(connection, payload);
            if (!retrySafe || (latest.status != 429 && latest.status != 502
                    && latest.status != 503 && latest.status != 504)) return latest;
            if (attempt + 1 < attempts) Thread.sleep((attempt + 1L) * 750L);
        }
        return latest;
    }

    private static URL cloudUrl(String path) throws Exception {
        if (path == null || !path.startsWith("/api/") || path.contains("://")
                || path.indexOf('\\') >= 0 || path.indexOf('\r') >= 0 || path.indexOf('\n') >= 0) {
            throw new IllegalArgumentException("FlexFactor Cloud path is invalid.");
        }
        URL base = new URL(BuildConfig.FLEXFACTOR_CLOUD_URL);
        if (!"https".equals(base.getProtocol()) || base.getHost().trim().isEmpty()
                || (!base.getPath().isEmpty() && !"/".equals(base.getPath()))) {
            throw new SecurityException("FlexFactor Cloud must use a fixed HTTPS origin.");
        }
        URL resolved = new URL(base, path);
        if (!resolved.getProtocol().equals(base.getProtocol())
                || !resolved.getHost().equals(base.getHost()) || resolved.getPort() != base.getPort()) {
            throw new SecurityException("FlexFactor Cloud request changed origin.");
        }
        return resolved;
    }

    private static HttpURLConnection connection(URL url, int readTimeout) throws Exception {
        if (!"https".equals(url.getProtocol())) {
            throw new IllegalArgumentException("Only HTTPS API connections are allowed.");
        }
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
        connection.setReadTimeout(readTimeout);
        connection.setInstanceFollowRedirects(false);
        connection.setUseCaches(false);
        connection.setRequestProperty("User-Agent", "FlexFactor-Android/" + BuildConfig.VERSION_NAME);
        return connection;
    }

    private static HttpResult execute(HttpURLConnection connection, byte[] body) throws Exception {
        try {
            if (body != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(body.length);
                try (OutputStream output = connection.getOutputStream()) { output.write(body); }
            }
            int status = connection.getResponseCode();
            String contentType = connection.getHeaderField("Content-Type");
            InputStream input = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            int limit = contentType != null && contentType.toLowerCase().startsWith("application/zip")
                    ? MAX_PHONE_ARTIFACT_BYTES : MAX_JSON_BYTES;
            return new HttpResult(status, readLimited(input, limit),
                    contentType == null ? "" : contentType);
        } finally { connection.disconnect(); }
    }

    private static JSONObject json(HttpResult result) throws Exception {
        if (result.body.length == 0) return new JSONObject();
        try { return new JSONObject(new String(result.body, StandardCharsets.UTF_8)); }
        catch (JSONException invalid) {
            throw new ApiException("FlexFactor Cloud returned an invalid response.");
        }
    }

    private static void requireSuccess(HttpResult result, JSONObject response) throws ApiException {
        if (result.status < 200 || result.status >= 300) throw cloudException(result, response);
    }

    private static ApiException cloudException(HttpResult result, JSONObject response) {
        String message = response.optString("message", "").trim();
        if (message.length() > 300) message = message.substring(0, 300);
        if (message.isEmpty()) message = "FlexFactor Cloud request failed (HTTP "
                + result.status + ").";
        return new ApiException(result.status, message);
    }

    private static byte[] readLimited(InputStream input, int limit) throws Exception {
        if (input == null) return new byte[0];
        try (InputStream source = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int total = 0;
            int count;
            while ((count = source.read(buffer)) != -1) {
                total += count;
                if (total > limit) throw new ApiException("The API response was unexpectedly large.");
                output.write(buffer, 0, count);
            }
            return output.toByteArray();
        }
    }

    private static byte[] readZipEntryLimited(ZipInputStream input, int limit) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int total = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            total += count;
            if (total > limit) throw new ApiException("The run detail entry was unexpectedly large.");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static String requireSecret(String value, String label) {
        String clean = value == null ? "" : value.trim();
        if (clean.isEmpty()) throw new IllegalArgumentException(label + " is missing.");
        if (clean.indexOf('\n') >= 0 || clean.indexOf('\r') >= 0) {
            throw new IllegalArgumentException(label + " contains an invalid line break.");
        }
        return clean;
    }

    private static String cleanSecret(String value) {
        String clean = value == null ? "" : value.trim();
        if (clean.length() > 8_192 || clean.indexOf('\n') >= 0 || clean.indexOf('\r') >= 0) {
            throw new IllegalArgumentException("Credential is invalid.");
        }
        return clean;
    }

    static boolean isAuthoritativelyMissing(Throwable failure) {
        if (!(failure instanceof ApiException)) return false;
        int status = ((ApiException) failure).status;
        return status == 404 || status == 410;
    }

    private static String encode(String value) throws Exception {
        return URLEncoder.encode(value, StandardCharsets.UTF_8.name()).replace("+", "%20");
    }

    private static final class HttpResult {
        final int status;
        final byte[] body;
        final String contentType;
        HttpResult(int status, byte[] body, String contentType) {
            this.status = status;
            this.body = body;
            this.contentType = contentType;
        }
    }

    static final class ApiException extends Exception {
        final int status;
        ApiException(String message) { this(0, message); }
        ApiException(int status, String message) {
            super(message);
            this.status = status;
        }
    }
}
