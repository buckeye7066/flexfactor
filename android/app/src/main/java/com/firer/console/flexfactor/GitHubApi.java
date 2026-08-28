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
import java.net.URLEncoder;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Minimal GitHub/OpenAI client for the standalone Android control plane. */
final class GitHubApi {
    static final String CONTROL_REPOSITORY = "buckeye7066/flexfactor";
    static final String API_VERSION = "2026-03-10";
    private static final String GITHUB = "https://api.github.com";
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final int READ_TIMEOUT_MS = 45_000;
    private static final int MAX_PHONE_ARTIFACT_BYTES = 2 * 1024 * 1024;
    private static final int MAX_PHONE_ENTRY_BYTES = 256 * 1024;

    static final class ConfigurationResult {
        final String login;
        ConfigurationResult(String login) { this.login = login; }
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
        @Override public String toString() { return fullName; }
    }

    static final class RunState {
        final long id;
        final String status;
        final String conclusion;
        final String htmlUrl;
        final String currentStep;
        RunState(long id, String status, String conclusion, String htmlUrl,
                String currentStep) {
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
                    text.append(row.optBoolean("success", false)
                            ? "Run completed successfully." : "Run did not complete successfully.");
                    text.append("\nRepository: ").append(row.optString("target_repository", "unknown"));
                    text.append("\nMode: ").append(row.optString("mode", "unknown"));
                    text.append("\nExit code: ").append(row.optInt("exit_code", -1));
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

    ConfigurationResult configure(String githubToken, String openAiKey, String anthropicKey)
            throws Exception {
        String token = requireSecret(githubToken, "GitHub token");
        JSONObject user = github(token, "GET", "/user", null);
        String login = user.optString("login", "").trim();
        if (login.isEmpty()) throw new ApiException("GitHub did not identify this account.");
        String openAi = cleanSecret(openAiKey);
        String anthropic = cleanSecret(anthropicKey);
        if (!openAi.isEmpty()) verifyOpenAi(openAi);
        if (!anthropic.isEmpty()) verifyAnthropic(anthropic);
        return new ConfigurationResult(login);
    }

    List<Repository> repositories(String token) throws Exception {
        String cleanToken = requireSecret(token, "GitHub token");
        List<Repository> repositories = new ArrayList<>();
        for (int page = 1; page <= 100; page++) {
            JSONObject result = github(cleanToken, "GET",
                    "/user/repos?affiliation=owner,collaborator,organization_member"
                            + "&sort=updated&direction=desc&per_page=100&page=" + page, null);
            JSONArray rows = result.getJSONArray("_array");
            for (int i = 0; i < rows.length(); i++) {
                JSONObject row = rows.getJSONObject(i);
                JSONObject permissions = row.optJSONObject("permissions");
                if (permissions != null && !permissions.optBoolean("push", false)) continue;
                String fullName = row.optString("full_name", "");
                String branch = row.optString("default_branch", "main");
                if (!fullName.isEmpty()) repositories.add(new Repository(
                        fullName, branch, row.optBoolean("private", false)));
            }
            if (rows.length() < 100) break;
        }
        return repositories;
    }

    RunState dispatch(String token, String openAiKey, String anthropicKey,
            MobileRunRequest request) throws Exception {
        String cleanToken = requireSecret(token, "GitHub token");
        boolean workflowChanged = ensureTargetWorkflow(
                cleanToken, request.repository, request.ref);
        putRepositorySecret(cleanToken, request.repository,
                "FLEXFACTOR_MOBILE_GITHUB_TOKEN", cleanToken);
        prepareProviderSecret(cleanToken, request.repository, request.provider,
                openAiKey, anthropicKey);
        JSONObject inputs = new JSONObject();
        for (Map.Entry<String, String> entry : request.workflowInputs().entrySet()) {
            if ("target_repository".equals(entry.getKey())) continue;
            inputs.put(entry.getKey(), entry.getValue());
        }
        JSONObject body = new JSONObject();
        body.put("ref", request.ref);
        body.put("inputs", inputs);
        long submittedAt = System.currentTimeMillis();
        String dispatchPath = "/repos/" + request.repository + "/actions/workflows/"
                + MobileWorkflow.FILE_NAME + "/dispatches";
        HttpResult dispatched = null;
        for (int attempt = 0; attempt < (workflowChanged ? 15 : 1); attempt++) {
            dispatched = rawGithub(cleanToken, "POST", dispatchPath, body);
            if (dispatched.status >= 200 && dispatched.status < 300) break;
            if (!workflowChanged || (dispatched.status != 404 && dispatched.status != 422)) {
                throw new ApiException(githubError(dispatched));
            }
            Thread.sleep(1_000L);
        }
        if (dispatched == null || dispatched.status < 200 || dispatched.status >= 300) {
            throw new ApiException(dispatched == null
                    ? "The workflow dispatch was not attempted." : githubError(dispatched));
        }
        return locateDispatchedRun(cleanToken, request, submittedAt);
    }

    RunState run(String token, String repository, long runId) throws Exception {
        if (runId <= 0) throw new IllegalArgumentException("Run ID is invalid.");
        if (!repository.matches("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")) {
            throw new IllegalArgumentException("Run repository is invalid.");
        }
        JSONObject run = github(requireSecret(token, "GitHub token"), "GET",
                "/repos/" + repository + "/actions/runs/" + runId, null);
        String status = run.optString("status", "unknown");
        String conclusion = run.optString("conclusion", "");
        String step = status;
        if (!"completed".equals(status)) {
            JSONObject jobs = github(token, "GET", "/repos/" + repository
                    + "/actions/runs/" + runId + "/jobs?per_page=20", null);
            JSONArray rows = jobs.optJSONArray("jobs");
            if (rows != null) {
                outer: for (int i = 0; i < rows.length(); i++) {
                    JSONArray steps = rows.getJSONObject(i).optJSONArray("steps");
                    if (steps == null) continue;
                    for (int j = 0; j < steps.length(); j++) {
                        JSONObject item = steps.getJSONObject(j);
                        if ("in_progress".equals(item.optString("status"))) {
                            step = item.optString("name", status);
                            break outer;
                        }
                    }
                }
            }
        }
        return new RunState(runId, status, conclusion,
                run.optString("html_url", ""), step);
    }

    RunDetails runDetails(String token, String repository, long runId) throws Exception {
        if (runId <= 0) throw new IllegalArgumentException("Run ID is invalid.");
        if (!repository.matches("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")) {
            throw new IllegalArgumentException("Run repository is invalid.");
        }
        String cleanToken = requireSecret(token, "GitHub token");
        JSONObject page = github(cleanToken, "GET", "/repos/" + repository
                + "/actions/runs/" + runId + "/artifacts?per_page=100", null);
        JSONArray artifacts = page.optJSONArray("artifacts");
        long artifactId = 0L;
        if (artifacts != null) {
            for (int i = 0; i < artifacts.length(); i++) {
                JSONObject item = artifacts.getJSONObject(i);
                if (!item.optBoolean("expired", false)
                        && item.optString("name", "").startsWith("mobile-phone-")) {
                    artifactId = item.optLong("id", 0L);
                    break;
                }
            }
        }
        if (artifactId <= 0) {
            throw new ApiException("Phone-readable details are not available for this run yet.");
        }
        byte[] archive = downloadArtifact(cleanToken, repository, artifactId);
        String result = "";
        String errors = "";
        String status = "";
        try (ZipInputStream zip = new ZipInputStream(new ByteArrayInputStream(archive))) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                if (entry.isDirectory()) continue;
                String name = entry.getName().replace('\\', '/');
                if (name.contains("../") || name.startsWith("/")) continue;
                String base = name.substring(name.lastIndexOf('/') + 1);
                if (!"mobile-result.json".equals(base)
                        && !"errors.md".equals(base)
                        && !"status.json".equals(base)) continue;
                String value = new String(readZipEntryLimited(zip, MAX_PHONE_ENTRY_BYTES),
                        StandardCharsets.UTF_8);
                if ("mobile-result.json".equals(base)) result = value;
                if ("errors.md".equals(base)) errors = value;
                if ("status.json".equals(base)) status = value;
            }
        }
        return new RunDetails(result, errors, status);
    }

    void submitSteering(String token, String repository, String requestId, String comment)
            throws Exception {
        String cleanToken = requireSecret(token, "GitHub token");
        if (!repository.matches("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")) {
            throw new IllegalArgumentException("Run repository is invalid.");
        }
        String compactId = requestId == null ? "" : requestId.replace("-", "");
        if (!compactId.matches("[A-Fa-f0-9]{32}")) {
            throw new IllegalArgumentException("Run request ID is invalid.");
        }
        String value = comment == null ? "" : comment.trim();
        boolean invalidControl = false;
        for (int i = 0; i < value.length(); i++) {
            char item = value.charAt(i);
            if (Character.getType(item) == Character.CONTROL
                    && item != '\n' && item != '\r' && item != '\t') {
                invalidControl = true;
                break;
            }
        }
        if (value.isEmpty() || value.length() > 4_000 || invalidControl) {
            throw new IllegalArgumentException(
                    "Steering comments must contain 1 to 4,000 printable characters.");
        }
        String name = "FLEXFACTOR_STEERING_" + compactId.substring(0, 16).toUpperCase();
        String path = "/repos/" + repository + "/actions/variables/" + name;
        HttpResult existing = rawGithub(cleanToken, "GET", path, null);
        JSONArray comments = new JSONArray();
        if (existing.status == 200) {
            JSONObject row = new JSONObject(new String(existing.body, StandardCharsets.UTF_8));
            try {
                comments = new JSONArray(row.optString("value", "[]"));
            } catch (JSONException ignored) {
                comments = new JSONArray();
            }
        } else if (existing.status != 404) {
            throw new ApiException(githubError(existing));
        }
        JSONObject entry = new JSONObject();
        entry.put("id", java.util.UUID.randomUUID().toString());
        entry.put("comment", value);
        entry.put("created_at", Instant.now().toString());
        comments.put(entry);
        while (comments.length() > 8) comments.remove(0);
        String serialized = comments.toString();
        if (serialized.length() > 40_000) {
            throw new IllegalArgumentException("The active build's steering queue is full.");
        }
        JSONObject payload = new JSONObject();
        payload.put("name", name);
        payload.put("value", serialized);
        if (existing.status == 200) {
            github(cleanToken, "PATCH", path, payload);
        } else {
            github(cleanToken, "POST", "/repos/" + repository + "/actions/variables", payload);
        }
    }

    private byte[] downloadArtifact(String token, String repository, long artifactId)
            throws Exception {
        HttpURLConnection api = connection(new URL(GITHUB + "/repos/" + repository
                + "/actions/artifacts/" + artifactId + "/zip"));
        api.setRequestMethod("GET");
        api.setRequestProperty("Accept", "application/vnd.github+json");
        api.setRequestProperty("Authorization", "Bearer " + token);
        api.setRequestProperty("X-GitHub-Api-Version", API_VERSION);
        try {
            int status = api.getResponseCode();
            if (status != 302 && status != 307) {
                throw new ApiException("GitHub could not open the result artifact (HTTP "
                        + status + ").");
            }
            String location = api.getHeaderField("Location");
            if (location == null || location.trim().isEmpty()) {
                throw new ApiException("GitHub returned no artifact download location.");
            }
            URL target = new URL(location);
            String host = target.getHost().toLowerCase();
            if (!"https".equals(target.getProtocol())
                    || (!host.endsWith(".blob.core.windows.net")
                    && !host.endsWith(".githubusercontent.com")
                    && !host.endsWith(".github.com"))) {
                throw new SecurityException("GitHub returned an untrusted artifact location.");
            }
            HttpURLConnection download = connection(target);
            download.setRequestMethod("GET");
            try {
                int downloadStatus = download.getResponseCode();
                if (downloadStatus != 200) {
                    throw new ApiException("The result artifact returned HTTP "
                            + downloadStatus + ".");
                }
                return readLimited(download.getInputStream(), MAX_PHONE_ARTIFACT_BYTES);
            } finally {
                download.disconnect();
            }
        } finally {
            api.disconnect();
        }
    }

    private void verifyOpenAi(String key) throws Exception {
        HttpURLConnection connection = connection(new URL("https://api.openai.com/v1/models"));
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Authorization", "Bearer " + key);
        HttpResult result = execute(connection, null);
        if (result.status != 200) {
            throw new ApiException("OpenAI rejected this key (HTTP " + result.status + ").");
        }
    }

    private void verifyAnthropic(String key) throws Exception {
        HttpURLConnection connection = connection(new URL("https://api.anthropic.com/v1/models"));
        connection.setRequestMethod("GET");
        connection.setRequestProperty("x-api-key", key);
        connection.setRequestProperty("anthropic-version", "2023-06-01");
        HttpResult result = execute(connection, null);
        if (result.status != 200) {
            throw new ApiException("Anthropic rejected this key (HTTP " + result.status + ").");
        }
    }

    private void prepareProviderSecret(String token, String repository,
            MobileRunRequest.Provider provider, String openAiKey, String anthropicKey)
            throws Exception {
        if (provider == MobileRunRequest.Provider.OPENAI) {
            String key = cleanSecret(openAiKey);
            if (!key.isEmpty()) {
                putRepositorySecret(token, repository, "OPENAI_API_KEY", key);
            } else if (!hasRepositorySecret(token, repository, "OPENAI_API_KEY")) {
                throw new ApiException("OpenAI is selected, but this repository has no "
                        + "OPENAI_API_KEY. Save the key once in Credentials.");
            }
        }
        if (provider == MobileRunRequest.Provider.ANTHROPIC) {
            String key = cleanSecret(anthropicKey);
            if (!key.isEmpty()) {
                putRepositorySecret(token, repository, "ANTHROPIC_API_KEY", key);
            } else if (!hasRepositorySecret(token, repository, "ANTHROPIC_API_KEY")) {
                throw new ApiException("Anthropic is selected, but this repository has no "
                        + "ANTHROPIC_API_KEY. Save the key once in Credentials.");
            }
        }
    }

    private boolean ensureTargetWorkflow(String token, String repository, String branch)
            throws Exception {
        String path = "/repos/" + repository + "/contents/" + MobileWorkflow.PATH
                + "?ref=" + encode(branch);
        HttpResult existing = rawGithub(token, "GET", path, null);
        String sha = "";
        String expected = MobileWorkflow.content();
        if (existing.status == 200) {
            JSONObject current = new JSONObject(new String(existing.body, StandardCharsets.UTF_8));
            sha = current.optString("sha", "");
            String encoded = current.optString("content", "").replace("\n", "");
            if (!encoded.isEmpty()) {
                String actual = new String(Base64.decode(encoded, Base64.DEFAULT),
                        StandardCharsets.UTF_8);
                if (expected.equals(actual)) return false;
            }
        } else if (existing.status != 404) {
            throw new ApiException(githubError(existing));
        }

        JSONObject payload = new JSONObject();
        payload.put("message", sha.isEmpty()
                ? "Install FlexFactor Mobile runner" : "Update FlexFactor Mobile runner");
        payload.put("content", Base64.encodeToString(
                expected.getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP));
        payload.put("branch", branch);
        if (!sha.isEmpty()) payload.put("sha", sha);
        github(token, "PUT", "/repos/" + repository + "/contents/"
                + MobileWorkflow.PATH, payload);
        return true;
    }

    private RunState locateDispatchedRun(String token, MobileRunRequest request,
            long submittedAt) throws Exception {
        String path = "/repos/" + request.repository + "/actions/workflows/"
                + MobileWorkflow.FILE_NAME + "/runs?event=workflow_dispatch&branch="
                + encode(request.ref) + "&per_page=30";
        long earliest = submittedAt - 10_000L;
        for (int attempt = 0; attempt < 30; attempt++) {
            JSONObject page = github(token, "GET", path, null);
            JSONArray runs = page.optJSONArray("workflow_runs");
            if (runs != null) {
                for (int i = 0; i < runs.length(); i++) {
                    JSONObject run = runs.getJSONObject(i);
                    String title = run.optString("display_title", "");
                    long created = parseInstant(run.optString("created_at", ""));
                    if (title.contains(request.requestId) && created >= earliest) {
                        long id = run.optLong("id", 0L);
                        if (id > 0) return new RunState(id,
                                run.optString("status", "queued"),
                                run.optString("conclusion", ""),
                                run.optString("html_url", ""), "Queued");
                    }
                }
            }
            Thread.sleep(1_000L);
        }
        throw new ApiException("GitHub accepted the run, but FlexFactor could not correlate "
                + "its run ID within 30 seconds.");
    }

    private boolean hasRepositorySecret(String token, String repository, String name)
            throws Exception {
        for (int page = 1; page <= 10; page++) {
            JSONObject response = github(token, "GET", "/repos/" + repository
                    + "/actions/secrets?per_page=100&page=" + page, null);
            JSONArray secrets = response.optJSONArray("secrets");
            if (secrets == null) return false;
            for (int i = 0; i < secrets.length(); i++) {
                if (name.equals(secrets.getJSONObject(i).optString("name"))) return true;
            }
            if (secrets.length() < 100) return false;
        }
        return false;
    }

    private void putRepositorySecret(String token, String repository, String name, String value)
            throws Exception {
        JSONObject key = github(token, "GET", "/repos/" + repository
                + "/actions/secrets/public-key", null);
        byte[] publicKey = Base64.decode(key.getString("key"), Base64.DEFAULT);
        byte[] message = value.getBytes(StandardCharsets.UTF_8);
        byte[] cipher = new byte[message.length + Box.SEALBYTES];
        LazySodiumAndroid sodium = new LazySodiumAndroid(new SodiumAndroid());
        if (!sodium.cryptoBoxSeal(cipher, message, message.length, publicKey)) {
            throw new ApiException("Could not encrypt the GitHub Actions credential.");
        }
        JSONObject payload = new JSONObject();
        payload.put("encrypted_value", Base64.encodeToString(cipher, Base64.NO_WRAP));
        payload.put("key_id", key.getString("key_id"));
        github(token, "PUT", "/repos/" + repository
                + "/actions/secrets/" + name, payload);
    }

    private JSONObject github(String token, String method, String path, JSONObject body)
            throws Exception {
        HttpResult result = rawGithub(token, method, path, body);
        if (result.status < 200 || result.status >= 300) {
            throw new ApiException(githubError(result));
        }
        if (result.body.length == 0) return new JSONObject();
        String text = new String(result.body, StandardCharsets.UTF_8);
        if (text.startsWith("[")) {
            JSONObject wrapper = new JSONObject();
            wrapper.put("_array", new JSONArray(text));
            return wrapper;
        }
        return new JSONObject(text);
    }

    private HttpResult rawGithub(String token, String method, String path, JSONObject body)
            throws Exception {
        HttpURLConnection connection = connection(new URL(GITHUB + path));
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/vnd.github+json");
        connection.setRequestProperty("Authorization", "Bearer " + token);
        connection.setRequestProperty("X-GitHub-Api-Version", API_VERSION);
        return execute(connection,
                body == null ? null : body.toString().getBytes(StandardCharsets.UTF_8));
    }

    private static String githubError(HttpResult result) {
        String detail = "";
        try {
            detail = new JSONObject(new String(result.body, StandardCharsets.UTF_8))
                    .optString("message", "").trim();
        } catch (JSONException ignored) {
            // Status is sufficient and avoids reflecting an arbitrary server body.
        }
        if (detail.length() > 160) detail = detail.substring(0, 160);
        return "GitHub request failed (HTTP " + result.status + ")"
                + (detail.isEmpty() ? "." : ": " + detail);
    }

    private static HttpURLConnection connection(URL url) throws Exception {
        if (!"https".equals(url.getProtocol())) {
            throw new IllegalArgumentException("Only HTTPS API connections are allowed.");
        }
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
        connection.setReadTimeout(READ_TIMEOUT_MS);
        connection.setInstanceFollowRedirects(false);
        connection.setUseCaches(false);
        connection.setRequestProperty("User-Agent", "FlexFactor-Android/3.2");
        return connection;
    }

    private static HttpResult execute(HttpURLConnection connection, byte[] body) throws Exception {
        try {
            if (body != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(body.length);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(body);
                }
            }
            int status = connection.getResponseCode();
            InputStream input = status >= 400 ? connection.getErrorStream()
                    : connection.getInputStream();
            return new HttpResult(status, readLimited(input, 2 * 1024 * 1024));
        } finally {
            connection.disconnect();
        }
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
            if (total > limit) {
                throw new ApiException("The run detail entry was unexpectedly large.");
            }
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
        if (clean.indexOf('\n') >= 0 || clean.indexOf('\r') >= 0) {
            throw new IllegalArgumentException("Credential contains an invalid line break.");
        }
        return clean;
    }

    private static String encode(String value) throws Exception {
        return URLEncoder.encode(value, StandardCharsets.UTF_8.name()).replace("+", "%20");
    }

    private static long parseInstant(String value) {
        try {
            return Instant.parse(value).toEpochMilli();
        } catch (Exception ignored) {
            return 0L;
        }
    }

    private static final class HttpResult {
        final int status;
        final byte[] body;
        HttpResult(int status, byte[] body) { this.status = status; this.body = body; }
    }

    static final class ApiException extends Exception {
        ApiException(String message) { super(message); }
    }
}
