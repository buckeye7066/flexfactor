package com.firer.console.flexfactor;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Receives the authenticated loopback URL from scripts/phone/engine.sh. */
public final class ConfigReceiver extends BroadcastReceiver {
    private static final java.util.Set<String> RECOVERY_STATES =
            new java.util.HashSet<>(java.util.Arrays.asList(
                    "updating", "starting", "ready", "failed", "missing-engine",
                    "github-auth-required", "checkout-dirty"));

    @Override
    public void onReceive(Context context, Intent intent) {
        String recovery = intent == null ? null : intent.getStringExtra("recovery_status");
        if (recovery != null) {
            if (!RECOVERY_STATES.contains(recovery)) {
                setResultCode(4);
                return;
            }
            context.getSharedPreferences(MainActivity.PREFERENCES, Context.MODE_PRIVATE)
                    .edit()
                    .putString(MainActivity.RECOVERY_STATUS_KEY, recovery)
                    .apply();
            setResultCode(1);
            return;
        }
        String endpoint = intent == null ? null : intent.getStringExtra("local");
        if (endpoint == null || endpoint.trim().isEmpty()) {
            setResultCode(3);
            return;
        }
        try {
            String normalized = EndpointPolicy.parseLocalEndpoint(endpoint).toString();
            context.getSharedPreferences(MainActivity.PREFERENCES, Context.MODE_PRIVATE)
                    .edit()
                    .putString(MainActivity.PENDING_ENDPOINT_KEY, normalized)
                    .apply();
            setResultCode(1);
        } catch (IllegalArgumentException rejected) {
            setResultCode(2);
        }
    }
}
