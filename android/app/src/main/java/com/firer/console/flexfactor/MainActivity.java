package com.firer.console.flexfactor;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Color;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.Collections;

public final class MainActivity extends Activity {
    public static final String PREFERENCES = "flexfactor";
    public static final String ENDPOINT_KEY = "local_endpoint";
    public static final String PENDING_ENDPOINT_KEY = "pending_local_endpoint";

    private FrameLayout root;
    private WebView web;
    private String loadedEndpoint = "";
    private Button updateButton;
    private boolean handoffDialogVisible;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(11, 15, 20));
        setContentView(root);
        acceptActivityHandoff();
        render();
        confirmPendingHandoff();
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        acceptActivityHandoff();
        loadedEndpoint = "";
        render();
        confirmPendingHandoff();
    }

    @Override
    protected void onResume() {
        super.onResume();
        String current = storedEndpoint();
        if (!current.equals(loadedEndpoint)) render();
        confirmPendingHandoff();
    }

    @Override
    protected void onDestroy() {
        destroyWebView();
        super.onDestroy();
    }

    private void destroyWebView() {
        if (web != null) {
            web.stopLoading();
            web.destroy();
            web = null;
        }
    }

    private void acceptActivityHandoff() {
        String endpoint = getIntent() == null ? null : getIntent().getStringExtra("local");
        if (endpoint == null) return;
        try {
            String normalized = EndpointPolicy.parseLocalEndpoint(endpoint).toString();
            getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
                    .putString(PENDING_ENDPOINT_KEY, normalized).apply();
        } catch (IllegalArgumentException rejected) {
            Toast.makeText(this, rejected.getMessage(), Toast.LENGTH_LONG).show();
        } finally {
            getIntent().removeExtra("local");
        }
    }

    private void confirmPendingHandoff() {
        String pending = getSharedPreferences(PREFERENCES, MODE_PRIVATE)
                .getString(PENDING_ENDPOINT_KEY, "");
        if (pending.isEmpty() || handoffDialogVisible) return;
        handoffDialogVisible = true;
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Use this phone's engine?")
                .setMessage("FlexFactor received an authenticated loopback engine address from Termux. Approve this on-phone connection?")
                .setCancelable(false)
                .setNegativeButton("Reject", (ignored, which) -> clearPendingHandoff())
                .setPositiveButton("Use engine", (ignored, which) -> {
                    clearPendingHandoff();
                    saveEndpoint(pending);
                    loadedEndpoint = "";
                    render();
                })
                .create();
        dialog.setOnDismissListener(ignored -> handoffDialogVisible = false);
        dialog.show();
    }

    private void clearPendingHandoff() {
        getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
                .remove(PENDING_ENDPOINT_KEY).apply();
    }

    private String storedEndpoint() {
        return getSharedPreferences(PREFERENCES, MODE_PRIVATE).getString(ENDPOINT_KEY, "");
    }

    private void saveEndpoint(String endpoint) {
        String normalized = EndpointPolicy.parseLocalEndpoint(endpoint).toString();
        getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
                .putString(ENDPOINT_KEY, normalized).apply();
    }

    private void render() {
        String endpoint = storedEndpoint();
        if (endpoint.equals(loadedEndpoint) && root.getChildCount() > 0) return;
        loadedEndpoint = endpoint;
        destroyWebView();
        root.removeAllViews();
        if (endpoint.isEmpty()) {
            showUnpaired();
        } else {
            showDashboard(endpoint);
        }
        addSettingsButton();
        addUpdateButton();
    }

    private void showUnpaired() {
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER);
        content.setPadding(dp(28), dp(28), dp(28), dp(28));

        TextView title = text("FlexFactor", 28, Color.WHITE);
        TextView detail = text(
                "No on-phone engine is paired. Start FlexFactor in Termux, then return here.",
                16,
                Color.rgb(170, 181, 194));
        detail.setGravity(Gravity.CENTER);
        content.addView(title);
        content.addView(detail, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(content, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private void showDashboard(String endpoint) {
        final URI trusted = EndpointPolicy.parseLocalEndpoint(endpoint);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSafeBrowsingEnabled(true);
        web.setBackgroundColor(Color.rgb(11, 15, 20));
        // The launcher uses JavaScript confirm() before starting a run and
        // alert() for the result. Without a WebChromeClient, WebView cancels
        // confirm() by default, so the launch request is never sent.
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !EndpointPolicy.sameOrigin(trusted, request.getUrl().toString());
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(
                    WebView view, WebResourceRequest request) {
                String candidate = request.getUrl().toString();
                if (EndpointPolicy.sameOrigin(trusted, candidate)) return null;
                return blockedResponse();
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        web.loadUrl(endpoint);
    }

    private WebResourceResponse blockedResponse() {
        byte[] body = "Blocked: the FlexFactor app only connects to this phone."
                .getBytes(StandardCharsets.UTF_8);
        return new WebResourceResponse(
                "text/plain", "utf-8", 403, "Blocked", Collections.emptyMap(),
                new ByteArrayInputStream(body));
    }

    private void addSettingsButton() {
        Button settings = new Button(this);
        settings.setText("⚙");
        settings.setTextSize(22);
        settings.setContentDescription("Engine settings");
        settings.setOnClickListener(view -> showSettings());
        FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(dp(56), dp(56));
        params.gravity = Gravity.END | Gravity.BOTTOM;
        params.setMargins(dp(12), dp(12), dp(12), dp(20));
        root.addView(settings, params);
    }

    private void addUpdateButton() {
        updateButton = new Button(this);
        updateButton.setText("Update");
        updateButton.setTextSize(14);
        updateButton.setContentDescription("Check for a FlexFactor update");
        updateButton.setOnClickListener(view -> startUpdate());
        FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(dp(116), dp(56));
        params.gravity = Gravity.START | Gravity.BOTTOM;
        params.setMargins(dp(12), dp(12), dp(12), dp(20));
        root.addView(updateButton, params);
    }

    private void startUpdate() {
        if (!getPackageManager().canRequestPackageInstalls()) {
            new AlertDialog.Builder(this)
                    .setTitle("Allow FlexFactor updates")
                    .setMessage("Android needs permission for FlexFactor to open its signed update in the system installer. Enable Allow from this source, then tap Update again.")
                    .setNegativeButton("Cancel", null)
                    .setPositiveButton("Open settings", (dialog, which) -> {
                        Intent intent = new Intent(
                                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                Uri.parse("package:" + getPackageName()));
                        startActivity(intent);
                    })
                    .show();
            return;
        }

        updateButton.setEnabled(false);
        updateButton.setText("Checking…");
        new AppUpdater(this).checkAndInstall(new AppUpdater.Callback() {
            @Override
            public void onUpToDate(String versionName) {
                resetUpdateButton();
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("FlexFactor is current")
                        .setMessage("Version " + versionName + " is the latest signed release.")
                        .setPositiveButton("OK", null)
                        .show();
            }

            @Override
            public void onInstallerReady(String versionName) {
                resetUpdateButton();
                Toast.makeText(MainActivity.this,
                        "Version " + versionName + " verified. Confirm the Android install.",
                        Toast.LENGTH_LONG).show();
            }

            @Override
            public void onError(String message) {
                resetUpdateButton();
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("Update not installed")
                        .setMessage(message)
                        .setPositiveButton("OK", null)
                        .show();
            }
        });
    }

    private void resetUpdateButton() {
        if (updateButton == null) return;
        updateButton.setText("Update");
        updateButton.setEnabled(true);
    }

    private void showSettings() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("http://127.0.0.1:8765/?t=...");
        input.setText(storedEndpoint());
        input.setSelectAllOnFocus(true);
        int pad = dp(20);
        FrameLayout holder = new FrameLayout(this);
        holder.setPadding(pad, 0, pad, 0);
        holder.addView(input, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("This phone's engine")
                .setMessage("Paste the authenticated URL printed by flexfactor-engine start. Remote PC addresses are refused.")
                .setView(holder)
                .setNegativeButton("Cancel", null)
                .setNeutralButton("Retry", null)
                .setPositiveButton("Save", null)
                .create();
        dialog.setOnShowListener(ignored -> {
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(view -> {
                if (web != null) web.reload();
                dialog.dismiss();
            });
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
                try {
                    saveEndpoint(input.getText().toString());
                    loadedEndpoint = "";
                    render();
                    dialog.dismiss();
                } catch (IllegalArgumentException rejected) {
                    input.setError(rejected.getMessage());
                }
            });
        });
        dialog.show();
    }

    private TextView text(String value, int sp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        view.setPadding(0, dp(8), 0, dp(8));
        return view;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
