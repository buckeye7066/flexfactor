package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.net.URI;
import org.junit.Test;

public final class EndpointPolicyTest {
    @Test
    public void acceptsAuthenticatedLoopbackEndpoints() {
        assertEquals("127.0.0.1", EndpointPolicy.parseLocalEndpoint(
                "http://127.0.0.1:8765/?t=secret").getHost());
        assertEquals("localhost", EndpointPolicy.parseLocalEndpoint(
                "http://localhost:8765/?t=secret").getHost());
    }

    @Test
    public void refusesRemoteMissingTokenAndNonHttpEndpoints() {
        assertThrows(IllegalArgumentException.class,
                () -> EndpointPolicy.parseLocalEndpoint("https://example.com/?t=secret"));
        assertThrows(IllegalArgumentException.class,
                () -> EndpointPolicy.parseLocalEndpoint("http://[::1]:8765/?t=secret"));
        assertThrows(IllegalArgumentException.class,
                () -> EndpointPolicy.parseLocalEndpoint("http://127.0.0.1:8765/"));
        assertThrows(IllegalArgumentException.class,
                () -> EndpointPolicy.parseLocalEndpoint("file:///data/local/tmp/dashboard.html?t=x"));
        assertThrows(IllegalArgumentException.class,
                () -> EndpointPolicy.parseLocalEndpoint("http://user@127.0.0.1:8765/?t=x"));
    }

    @Test
    public void subresourcesMustRemainOnTheConfiguredOrigin() {
        URI trusted = EndpointPolicy.parseLocalEndpoint("http://127.0.0.1:8765/?t=secret");
        assertTrue(EndpointPolicy.sameOrigin(trusted, "http://127.0.0.1:8765/api/status"));
        assertFalse(EndpointPolicy.sameOrigin(trusted, "http://127.0.0.1:9999/api/status"));
        assertFalse(EndpointPolicy.sameOrigin(trusted, "https://127.0.0.1:8765/api/status"));
        assertFalse(EndpointPolicy.sameOrigin(trusted, "https://example.com/collect"));
    }
}
