package com.firer.console.flexfactor;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.graphics.Color;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
    public static final String RECOVERY_STATUS_KEY = "recovery_status";
    private static final String RECOVERED_VERSION_KEY = "recovered_version";
    private static final int RUN_COMMAND_PERMISSION_REQUEST = 410;
    private static final long RECOVERY_POLL_MS = 1000L;
    private static final long COMMAND_ACCEPT_TIMEOUT_MS = 12000L;
    private static final long RECOVERY_TIMEOUT_MS = 300000L;

    private FrameLayout root;
    private WebView web;
    private String loadedEndpoint = "";
    private Button updateButton;
    private boolean handoffDialogVisible;
    private boolean recoveryMode;
    private boolean permissionPromptShown;
    private boolean repairAfterPermission;
    private boolean externalSetupPending;
    private long recoveryStartedAt;
    private String displayedRecoveryStatus = "";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable recoveryPoll = this::pollRecovery;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(11, 15, 20));
        setContentView(root);
        acceptActivityHandoff();
        render();
        confirmPendingHandoff();
        maybeRecoverForThisVersion();
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
        if (externalSetupPending) {
            externalSetupPending = false;
            handler.postDelayed(() -> requestEngineRecovery(true), 500L);
        }
        String current = storedEndpoint();
        if (!current.equals(loadedEndpoint)) render();
        confirmPendingHandoff();
        if (recoveryMode) pollRecovery();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(recoveryPoll);
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
        if (pending.equals(storedEndpoint())) {
            clearPendingHandoff();
            return;
        }
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
        if (recoveryMode) {
            showRecovery(displayedRecoveryStatus);
            return;
        }
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
                "No on-phone engine is paired yet. FlexFactor can install, update, and start it on this phone.",
                16,
                Color.rgb(170, 181, 194));
        detail.setGravity(Gravity.CENTER);
        content.addView(title);
        content.addView(detail, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        Button start = new Button(this);
        start.setText("Start on this phone");
        start.setOnClickListener(view -> requestEngineRecovery(true));
        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        buttonParams.setMargins(0, dp(20), 0, 0);
        content.addView(start, buttonParams);
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

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                    android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) requestEngineRecovery(false);
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
                .setNeutralButton("Start / repair", null)
                .setPositiveButton("Save", null)
                .create();
        dialog.setOnShowListener(ignored -> {
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(view -> {
                dialog.dismiss();
                requestEngineRecovery(true);
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

    private void maybeRecoverForThisVersion() {
        int recovered = getSharedPreferences(PREFERENCES, MODE_PRIVATE)
                .getInt(RECOVERED_VERSION_KEY, 0);
        if (recovered >= BuildConfig.VERSION_CODE || !isTermuxInstalled()) return;
        if (checkSelfPermission(EngineRecoveryScript.TERMUX_PERMISSION)
                == PackageManager.PERMISSION_GRANTED) {
            requestEngineRecovery(true);
            return;
        }
        if (permissionPromptShown) return;
        permissionPromptShown = true;
        new AlertDialog.Builder(this)
                .setTitle("Finish on-phone setup")
                .setMessage("FlexFactor needs Android's Run commands in Termux permission so its icon can start and repair the on-phone engine.")
                .setNegativeButton("Later", null)
                .setPositiveButton("Continue", (dialog, which) -> requestEngineRecovery(true))
                .show();
    }

    private boolean isTermuxInstalled() {
        try {
            getPackageManager().getApplicationInfo(EngineRecoveryScript.TERMUX_PACKAGE, 0);
            return true;
        } catch (PackageManager.NameNotFoundException missing) {
            return false;
        }
    }

    private void requestEngineRecovery(boolean repair) {
        if (recoveryMode) return;
        if (!isTermuxInstalled()) {
            new AlertDialog.Builder(this)
                    .setTitle("Termux is required")
                    .setMessage("Install the current F-Droid or official GitHub build of Termux, then return to FlexFactor.")
                    .setPositiveButton("OK", null)
                    .show();
            return;
        }
        if (checkSelfPermission(EngineRecoveryScript.TERMUX_PERMISSION)
                != PackageManager.PERMISSION_GRANTED) {
            repairAfterPermission = repair;
            requestPermissions(new String[]{EngineRecoveryScript.TERMUX_PERMISSION},
                    RUN_COMMAND_PERMISSION_REQUEST);
            return;
        }
        runTermuxCommand(repair);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
            int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != RUN_COMMAND_PERMISSION_REQUEST) return;
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            runTermuxCommand(repairAfterPermission);
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Permission still needed")
                .setMessage("Open FlexFactor App info → Permissions → Additional permissions and allow Run commands in Termux environment.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Open App info", (dialog, which) -> startActivity(new Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + getPackageName()))))
                .show();
    }

    private void runTermuxCommand(boolean repair) {
        getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
                .putString(RECOVERY_STATUS_KEY, "requested").apply();
        recoveryMode = true;
        recoveryStartedAt = android.os.SystemClock.elapsedRealtime();
        displayedRecoveryStatus = "requested";
        showRecovery(displayedRecoveryStatus);

        Intent command = new Intent();
        command.setComponent(new ComponentName(
                EngineRecoveryScript.TERMUX_PACKAGE, EngineRecoveryScript.TERMUX_SERVICE));
        command.setAction(EngineRecoveryScript.TERMUX_ACTION);
        command.putExtra("com.termux.RUN_COMMAND_PATH", EngineRecoveryScript.BASH);
        command.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{"-s"});
        command.putExtra("com.termux.RUN_COMMAND_STDIN",
                repair ? EngineRecoveryScript.repairScript() : EngineRecoveryScript.startScript());
        command.putExtra("com.termux.RUN_COMMAND_WORKDIR", EngineRecoveryScript.HOME);
        command.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        command.putExtra("com.termux.RUN_COMMAND_COMMAND_LABEL", "FlexFactor engine recovery");
        command.putExtra("com.termux.RUN_COMMAND_COMMAND_DESCRIPTION",
                "Updates and starts the FlexFactor engine on this phone.");
        try {
            startService(command);
            handler.removeCallbacks(recoveryPoll);
            handler.postDelayed(recoveryPoll, RECOVERY_POLL_MS);
        } catch (RuntimeException blocked) {
            showExternalAppsSetup();
        }
    }

    private void pollRecovery() {
        if (!recoveryMode || isFinishing() || isDestroyed()) return;
        handler.removeCallbacks(recoveryPoll);
        confirmPendingHandoff();
        String status = getSharedPreferences(PREFERENCES, MODE_PRIVATE)
                .getString(RECOVERY_STATUS_KEY, "requested");
        long elapsed = android.os.SystemClock.elapsedRealtime() - recoveryStartedAt;
        if ("ready".equals(status)) {
            getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
                    .putInt(RECOVERED_VERSION_KEY, BuildConfig.VERSION_CODE).apply();
            recoveryMode = false;
            loadedEndpoint = "";
            render();
            confirmPendingHandoff();
            return;
        }
        if (isRecoveryFailure(status)) {
            recoveryMode = false;
            displayedRecoveryStatus = status;
            showRecovery(status);
            return;
        }
        if ("requested".equals(status) && elapsed >= COMMAND_ACCEPT_TIMEOUT_MS) {
            showExternalAppsSetup();
            return;
        }
        if (elapsed >= RECOVERY_TIMEOUT_MS) {
            recoveryMode = false;
            displayedRecoveryStatus = "timed-out";
            showRecovery(displayedRecoveryStatus);
            return;
        }
        if (!status.equals(displayedRecoveryStatus)) {
            displayedRecoveryStatus = status;
            showRecovery(status);
        }
        handler.postDelayed(recoveryPoll, RECOVERY_POLL_MS);
    }

    private boolean isRecoveryFailure(String status) {
        return "failed".equals(status) || "missing-engine".equals(status)
                || "github-auth-required".equals(status) || "checkout-dirty".equals(status);
    }

    private void showRecovery(String status) {
        handler.removeCallbacks(recoveryPoll);
        destroyWebView();
        root.removeAllViews();
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER);
        content.setPadding(dp(28), dp(28), dp(28), dp(90));
        content.addView(text("FlexFactor", 28, Color.WHITE));

        String message;
        boolean retry = false;
        if ("updating".equals(status)) {
            message = "Updating the on-phone engine…";
        } else if ("starting".equals(status)) {
            message = "Starting the on-phone engine…";
        } else if ("github-auth-required".equals(status)) {
            message = "GitHub needs to be signed in once in Termux. Run gh auth login --web --git-protocol https, then retry.";
            retry = true;
        } else if ("checkout-dirty".equals(status)) {
            message = "The managed FlexFactor checkout has local changes, so the app preserved them. Resolve them in Termux, then retry.";
            retry = true;
        } else if ("failed".equals(status) || "missing-engine".equals(status)
                || "timed-out".equals(status)) {
            message = "The engine did not become ready. Retry the safe repair; details are in ~/.phone-console/app-recovery.log in Termux.";
            retry = true;
        } else {
            message = "Connecting to Termux and preparing the on-phone engine…";
        }
        TextView detail = text(message, 16, Color.rgb(170, 181, 194));
        detail.setGravity(Gravity.CENTER);
        content.addView(detail);
        if (retry) {
            Button button = new Button(this);
            button.setText("Retry repair");
            button.setOnClickListener(view -> requestEngineRecovery(true));
            content.addView(button);
            Button termux = new Button(this);
            termux.setText("Open Termux");
            termux.setOnClickListener(view -> openTermux());
            content.addView(termux);
        }
        root.addView(content, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        addSettingsButton();
        addUpdateButton();
    }

    private void showExternalAppsSetup() {
        handler.removeCallbacks(recoveryPoll);
        recoveryMode = false;
        ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        new AlertDialog.Builder(this)
                .setTitle("One-time Termux approval")
                .setMessage("Termux requires its owner to enable external app commands once. Tap Copy & open Termux, paste the command, press Enter, then return here.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Copy & open Termux", (dialog, which) -> {
                    clipboard.setPrimaryClip(ClipData.newPlainText(
                            "Enable FlexFactor icon control",
                            EngineRecoveryScript.ENABLE_EXTERNAL_APPS_COMMAND));
                    Toast.makeText(this, "Command copied", Toast.LENGTH_SHORT).show();
                    externalSetupPending = true;
                    openTermux();
                })
                .show();
        loadedEndpoint = "";
        render();
    }

    private void openTermux() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(
                EngineRecoveryScript.TERMUX_PACKAGE);
        if (launch != null) startActivity(launch);
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
