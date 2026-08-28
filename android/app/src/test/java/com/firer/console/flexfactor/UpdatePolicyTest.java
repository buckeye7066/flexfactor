package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class UpdatePolicyTest {
    @Test
    public void acceptsOnlyThisRepositoryReleaseApks() {
        assertEquals(
                "github.com",
                UpdatePolicy.requireReleaseApk(
                        "https://github.com/buckeye7066/flexfactor/releases/download/android-v2.2.0/flexfactor-2.2.0.apk")
                        .getHost());
        assertThrows(IllegalArgumentException.class, () ->
                UpdatePolicy.requireReleaseApk(
                        "https://github.com/attacker/flexfactor/releases/download/android-v9/flexfactor-9.apk"));
        assertThrows(IllegalArgumentException.class, () ->
                UpdatePolicy.requireReleaseApk("http://github.com/buckeye7066/flexfactor/releases/download/android-v2/flexfactor-2.apk"));
    }

    @Test
    public void allowsOnlyGithubTransportHosts() {
        UpdatePolicy.requireTrustedTransport("https://github.com/release");
        UpdatePolicy.requireTrustedTransport("https://release-assets.githubusercontent.com/file");
        assertThrows(IllegalArgumentException.class, () ->
                UpdatePolicy.requireTrustedTransport("https://example.com/flexfactor.apk"));
        assertThrows(IllegalArgumentException.class, () ->
                UpdatePolicy.requireTrustedTransport("https://github.com@example.com/flexfactor.apk"));
    }

    @Test
    public void validatesDigestAndVersionOrder() {
        String digest = "a".repeat(64);
        assertEquals(digest, UpdatePolicy.requireSha256(digest.toUpperCase()));
        assertThrows(IllegalArgumentException.class, () -> UpdatePolicy.requireSha256("abc"));
        assertTrue(UpdatePolicy.isNewer(20201, 20200));
        assertFalse(UpdatePolicy.isNewer(20200, 20200));
    }
}
