package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.json.JSONObject;
import org.junit.Test;

public final class GitHubDeviceFlowTest {
    @Test
    public void registeredClientAndDeviceResponseArePinned() throws Exception {
        assertTrue(GitHubApi.OAUTH_CLIENT_ID.matches("[A-Za-z0-9]{20}"));
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
    public void deviceResponseRejectsAnUntrustedVerificationHost() {
        JSONObject response = new JSONObject()
                .put("device_code", "device-secret")
                .put("user_code", "ABCD-1234")
                .put("verification_uri", "https://attacker.example/login/device")
                .put("expires_in", 900);
        assertThrows(GitHubApi.ApiException.class,
                () -> GitHubApi.requireDeviceAuthorization(response, 0L));
    }

    @Test
    public void tokenResponseRequiresAnAccessToken() {
        assertThrows(GitHubApi.ApiException.class,
                () -> GitHubApi.requireOAuthToken(new JSONObject()));
    }
}
