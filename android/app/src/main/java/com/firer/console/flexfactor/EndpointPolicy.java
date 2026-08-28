package com.firer.console.flexfactor;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/** Security boundary for every URL stored or loaded by the Android client. */
public final class EndpointPolicy {
    private EndpointPolicy() {}

    public static URI parseLocalEndpoint(String value) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("The local engine address is missing.");
        }
        final URI uri;
        try {
            uri = new URI(value.trim());
        } catch (URISyntaxException error) {
            throw new IllegalArgumentException("The local engine address is invalid.", error);
        }
        String scheme = lower(uri.getScheme());
        String host = lower(uri.getHost());
        if (!("http".equals(scheme) || "https".equals(scheme))) {
            throw new IllegalArgumentException("Only HTTP(S) engine addresses are supported.");
        }
        if (!("127.0.0.1".equals(host) || "localhost".equals(host))) {
            throw new IllegalArgumentException("The engine must run on this phone (loopback only).");
        }
        if (uri.getUserInfo() != null || uri.getFragment() != null) {
            throw new IllegalArgumentException("Credentials and fragments are not allowed in the engine address.");
        }
        if (!hasToken(uri.getRawQuery())) {
            throw new IllegalArgumentException("The authenticated engine token is missing.");
        }
        return uri;
    }

    public static boolean sameOrigin(URI trusted, String candidate) {
        try {
            URI other = new URI(candidate);
            return lower(trusted.getScheme()).equals(lower(other.getScheme()))
                    && lower(trusted.getHost()).equals(lower(other.getHost()))
                    && effectivePort(trusted) == effectivePort(other)
                    && other.getUserInfo() == null;
        } catch (Exception ignored) {
            return false;
        }
    }

    private static boolean hasToken(String query) {
        if (query == null) return false;
        for (String part : query.split("&")) {
            int split = part.indexOf('=');
            if (split > 0 && "t".equals(part.substring(0, split)) && split + 1 < part.length()) {
                return true;
            }
        }
        return false;
    }

    private static int effectivePort(URI uri) {
        if (uri.getPort() >= 0) return uri.getPort();
        return "https".equals(lower(uri.getScheme())) ? 443 : 80;
    }

    private static String lower(String value) {
        return value == null ? "" : value.toLowerCase(Locale.ROOT);
    }
}
