package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Before;
import org.junit.BeforeClass;
import org.junit.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLConnection;
import java.net.URLStreamHandler;
import java.nio.charset.StandardCharsets;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/** Runs the actual client HTTP and ZIP handling against an in-memory HTTPS transport. */
public final class GitHubApiArtifactTest {
    private static final String ID = "123e4567-e89b-12d3-a456-426614174000";
    private static URL requested;
    private static byte[] response;
    private static int responseCode;

    @BeforeClass
    public static void interceptHttps() {
        URL.setURLStreamHandlerFactory(protocol -> "https".equals(protocol)
                ? new URLStreamHandler() {
                    @Override protected URLConnection openConnection(URL url) {
                        requested = url;
                        return new HttpURLConnection(url) {
                            @Override public void connect() { }
                            @Override public void disconnect() { }
                            @Override public boolean usingProxy() { return false; }
                            @Override public int getResponseCode() {
                                return GitHubApiArtifactTest.responseCode;
                            }
                            @Override public String getHeaderField(String name) {
                                return "Content-Type".equalsIgnoreCase(name)
                                        ? (GitHubApiArtifactTest.responseCode == 200
                                                ? "application/zip" : "application/json")
                                        : null;
                            }
                            @Override public InputStream getInputStream() {
                                return new ByteArrayInputStream(response);
                            }
                            @Override public InputStream getErrorStream() { return getInputStream(); }
                        };
                    }
                } : null);
    }

    @Before
    public void resetResponse() throws Exception {
        requested = null;
        responseCode = 200;
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (ZipOutputStream zip = new ZipOutputStream(bytes)) {
            zip.putNextEntry(new ZipEntry("errors.md"));
            zip.write("Verified error ledger".getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();
        }
        response = bytes.toByteArray();
    }

    @Test
    public void detailsSendsBothIdsAndParsesTheBoundedArtifact() throws Exception {
        GitHubApi.RunDetails details = details("owner/repo", ID, 42L);
        assertEquals("/api/runs/details", requested.getPath());
        assertEquals("repository=owner%2Frepo&request_id=" + ID + "&run_id=42",
                requested.getQuery());
        assertTrue(details.displayText().contains("Verified error ledger"));
    }

    @Test
    public void detailsRejectsMissingOrNoncanonicalRequestBeforeTransport() {
        for (String id : new String[]{null, "", "legacy", "1-1-1-1-1", " " + ID}) {
            assertThrows(IllegalArgumentException.class, () -> details("owner/repo", id, 42L));
            assertNull(requested);
        }
    }

    @Test
    public void detailsStillRejectsInvalidRepositoryOrRunBeforeTransport() {
        assertThrows(IllegalArgumentException.class, () -> details("owner/repo/extra", ID, 42L));
        assertThrows(IllegalArgumentException.class, () -> details("owner/repo", ID, 0L));
        assertNull(requested);
    }

    @Test
    public void mismatchedRunResponseRemainsAnErrorInsteadOfAnEmptyResult() {
        responseCode = 409;
        response = "{\"message\":\"Run does not match the request ID.\"}"
                .getBytes(StandardCharsets.UTF_8);
        GitHubApi.ApiException failed = assertThrows(GitHubApi.ApiException.class,
                () -> details("owner/repo", ID, 42L));
        assertEquals(409, failed.status);
    }

    private static GitHubApi.RunDetails details(String repository, String requestId, long runId)
            throws Exception {
        return new GitHubApi().runDetails("test-session", repository, requestId, runId);
    }
}
