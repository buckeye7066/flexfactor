package com.firer.console.flexfactor;

import android.util.Base64;

import com.goterl.lazysodium.LazySodiumAndroid;
import com.goterl.lazysodium.SodiumAndroid;
import com.goterl.lazysodium.interfaces.Box;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Minimal GitHub/OpenAI client for the standalone Android control plane. */
final class GitHubApi {
    static final String CONTROL_REPOSITORY = "buckeye7066/flexfactor";
    static final String MOBILE_WORKFLOW = "mobile-run.yml";
    static final String API_VERSION = "2026-03-10";
    private static final String GITHUB = "https://api.github.com";
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final int READ_TIMEOUT_MS = 45_000;

    static final class ConfigurationResult {
        final String login;
        ConfigurationResult(String login) { this.login = login; }
    }

    static final class Repository {
        final String fullName;
        final String defaultBranch;
        Repository(String fullName, String defaultBranch) {
            this.fullName = fullName;
            this.defaultBranch = defaultBranch;
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

    ConfigurationResult configure(String githubToken, String openAiKey) throws Exception {
        String token = requireSecret(githubToken, "GitHub token");
        JSONObject user = github(token, "GET", "/user", null);
        String login = user.optString("login", "").trim();
        if (login.isEmpty()) throw new ApiException("GitHub did not identify this account.");
        String provider = openAiKey == null ? "" : openAiKey.trim();
        // Validate every supplied credential before mutating repository state.
        // A rejected optional key must not leave the runner using a different
        // GitHub token than the one retained by the phone.
        if (!provider.isEmpty()) verifyOpenAi(provider);
        putRepositorySecret(token, "FLEXFACTOR_MOBILE_GITHUB_TOKEN", token);
        if (provider.isEmpty()) {
            deleteRepositorySecretIfPresent(token, "OPENAI_API_KEY");
        } else {
            putRepositorySecret(token, "OPENAI_API_KEY", provider);
        }
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
                if (row.optBoolean("private", true)) continue;
                JSONObject permissions = row.optJSONObject("permissions");
                if (permissions != null && !permissions.optBoolean("push", false)) continue;
                String fullName = row.optString("full_name", "");
                String branch = row.optString("default_branch", "main");
                if (!fullName.isEmpty()) repositories.add(new Repository(fullName, branch));
            }
            if (rows.length() < 100) break;
        }
        return repositories;
    }

    RunState dispatch(String token, MobileRunRequest request) throws Exception {
        JSONObject inputs = new JSONObject();
        for (Map.Entry<String, String> entry : request.workflowInputs().entrySet()) {
            inputs.put(entry.getKey(), entry.getValue());
        }
        JSONObject body = new JSONObject();
        body.put("ref", "main");
        body.put("inputs", inputs);
        JSONObject response = github(requireSecret(token, "GitHub token"), "POST",
                "/repos/" + CONTROL_REPOSITORY + "/actions/workflows/"
                        + MOBILE_WORKFLOW + "/dispatches", body);
        long id = response.optLong("workflow_run_id", 0L);
        String htmlUrl = response.optString("html_url", "");
        if (id <= 0) throw new ApiException("GitHub accepted the run but returned no run ID.");
        return new RunState(id, "queued", "", htmlUrl, "Queued");
    }

    RunState run(String token, long runId) throws Exception {
        if (runId <= 0) throw new IllegalArgumentException("Run ID is invalid.");
        JSONObject run = github(requireSecret(token, "GitHub token"), "GET",
                "/repos/" + CONTROL_REPOSITORY + "/actions/runs/" + runId, null);
        String status = run.optString("status", "unknown");
        String conclusion = run.optString("conclusion", "");
        String step = status;
        if (!"completed".equals(status)) {
            JSONObject jobs = github(token, "GET", "/repos/" + CONTROL_REPOSITORY
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

    private void verifyOpenAi(String key) throws Exception {
        HttpURLConnection connection = connection(new URL("https://api.openai.com/v1/models"));
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Authorization", "Bearer " + key);
        HttpResult result = execute(connection, null);
        if (result.status != 200) {
            throw new ApiException("OpenAI rejected this key (HTTP " + result.status + ").");
        }
    }

    private void putRepositorySecret(String token, String name, String value) throws Exception {
        JSONObject key = github(token, "GET", "/repos/" + CONTROL_REPOSITORY
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
        github(token, "PUT", "/repos/" + CONTROL_REPOSITORY
                + "/actions/secrets/" + name, payload);
    }

    private void deleteRepositorySecretIfPresent(String token, String name) throws Exception {
        URL url = new URL(GITHUB + "/repos/" + CONTROL_REPOSITORY
                + "/actions/secrets/" + name);
        HttpURLConnection connection = connection(url);
        connection.setRequestMethod("DELETE");
        connection.setRequestProperty("Accept", "application/vnd.github+json");
        connection.setRequestProperty("Authorization", "Bearer " + token);
        connection.setRequestProperty("X-GitHub-Api-Version", API_VERSION);
        HttpResult result = execute(connection, null);
        if (result.status != 204 && result.status != 404) {
            throw new ApiException(githubError(result));
        }
    }

    private JSONObject github(String token, String method, String path, JSONObject body)
            throws Exception {
        HttpURLConnection connection = connection(new URL(GITHUB + path));
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/vnd.github+json");
        connection.setRequestProperty("Authorization", "Bearer " + token);
        connection.setRequestProperty("X-GitHub-Api-Version", API_VERSION);
        HttpResult result = execute(connection,
                body == null ? null : body.toString().getBytes(StandardCharsets.UTF_8));
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
        connection.setRequestProperty("User-Agent", "FlexFactor-Android/3.0");
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

    private static String requireSecret(String value, String label) {
        String clean = value == null ? "" : value.trim();
        if (clean.isEmpty()) throw new IllegalArgumentException(label + " is missing.");
        if (clean.indexOf('\n') >= 0 || clean.indexOf('\r') >= 0) {
            throw new IllegalArgumentException(label + " contains an invalid line break.");
        }
        return clean;
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
