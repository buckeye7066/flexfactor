package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.json.JSONObject;
import org.junit.Test;

public final class GitHubDeviceFlowTest {
    @Test
    public void managedDeviceResponseIsPinned() throws Exception {
        JSONObject response = new JSONObject()
                .put("device_code", "device-secret")
                .put("user_code", "ABCD-1234")
                .put("verification_uri", "https://github.com/login/device")
                .put("expires_in", 900)
                .put("interval", 1);
        GitHubApi.DeviceAuthorization authorization =
                GitHubApi.requireDeviceAuthorization(response, 10_000L);
        assertEquals("ABCD-1234", authorization.userCode);
        assertEquals(910_000L, authorization.expiresAt);
        assertEquals(5, authorization.intervalSeconds);
    }

    @Test
    public void deviceResponseRejectsAnUntrustedVerificationHost() throws Exception {
        JSONObject response = new JSONObject()
                .put("device_code", "device-secret")
                .put("user_code", "ABCD-1234")
                .put("verification_uri", "https://attacker.example/login/device")
                .put("expires_in", 900);
        assertThrows(GitHubApi.ApiException.class,
                () -> GitHubApi.requireDeviceAuthorization(response, 0L));
    }

    @Test
    public void tokenResponseRequiresAnAccessToken() throws Exception {
        assertThrows(GitHubApi.ApiException.class,
                () -> GitHubApi.requireOAuthToken(new JSONObject()));
    }

    @Test
    public void expiringTokenRequiresServerIssuedRotationMaterial() throws Exception {
        JSONObject incomplete = new JSONObject()
                .put("access_token", "gho_access")
                .put("expires_in", 28_800);
        assertThrows(GitHubApi.ApiException.class,
                () -> GitHubApi.requireOAuthToken(incomplete));

        GitHubApi.OAuthToken complete = GitHubApi.requireOAuthToken(new JSONObject()
                .put("access_token", "gho_access")
                .put("refresh_token", "ghr_refresh")
                .put("expires_in", 28_800));
        assertEquals("ghr_refresh", complete.refreshToken);
    }

    @Test
    public void onlyDefinitiveMissingRunResponsesAreTerminal() {
        assertTrue(GitHubApi.isAuthoritativelyMissing(
                new GitHubApi.ApiException(404, "not found")));
        assertTrue(GitHubApi.isAuthoritativelyMissing(
                new GitHubApi.ApiException(410, "gone")));
        assertFalse(GitHubApi.isAuthoritativelyMissing(
                new GitHubApi.ApiException(503, "retry")));
        assertFalse(GitHubApi.isAuthoritativelyMissing(
                new IllegalStateException("retry")));
    }
}
