package com.firer.console.flexfactor;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Standalone Android control plane for all four FlexFactor modes. */
public final class MainActivity extends Activity {
    private static final String PREFERENCES = "flexfactor_mobile";
    private static final String LOGIN = "github_login";
    private static final String REPOSITORY = "selected_repository";
    private static final String REF = "selected_ref";
    private static final String LAST_RUN_ID = "last_run_id";
    private static final String LAST_RUN_URL = "last_run_url";
    private static final String LAST_RUN_STATUS = "last_run_status";
    private static final long POLL_MS = 5_000L;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final GitHubApi api = new GitHubApi();
    private final Runnable pollRun = this::pollLastRun;

    private SecureStore secrets;
    private SharedPreferences preferences;
    private LinearLayout content;
    private Button repositoryButton;
    private Button updateButton;
    private TextView accountState;
    private TextView runState;
    private boolean destroyed;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences(PREFERENCES, MODE_PRIVATE);
        secrets = new SecureStore(this);
        renderHome();
        if (!configured()) main.postDelayed(this::showCredentialSetup, 350L);
        if (preferences.getLong(LAST_RUN_ID, 0L) > 0L) pollLastRun();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshHeader();
        if (preferences.getLong(LAST_RUN_ID, 0L) > 0L) pollLastRun();
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        main.removeCallbacks(pollRun);
        worker.shutdownNow();
        super.onDestroy();
    }

    private void renderHome() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(Color.rgb(11, 15, 20));
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(20), dp(24), dp(20), dp(28));
        scroll.addView(content, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(scroll);

        content.addView(text("FlexFactor", 30, Color.WHITE));
        content.addView(text("Standalone phone app · version " + installedVersion(),
                14, Color.rgb(139, 151, 165)));

        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        Button settingsButton = button("Credentials");
        settingsButton.setOnClickListener(view -> showCredentialSetup());
        updateButton = button("Update");
        updateButton.setOnClickListener(view -> startUpdate());
        top.addView(settingsButton, weighted());
        top.addView(updateButton, weighted());
        content.addView(top, margins(0, 12, 0, 8));

        accountState = text("", 14, Color.rgb(170, 181, 194));
        content.addView(accountState);

        content.addView(section("Repository"));
        repositoryButton = button("Choose repository");
        repositoryButton.setContentDescription("Choose a GitHub repository");
        repositoryButton.setOnClickListener(view -> chooseRepository());
        content.addView(repositoryButton, margins(0, 4, 0, 12));

        content.addView(section("What do you want FlexFactor to do?"));
        addMode("1 · Refactor a file",
                "Improve one selected file toward a stated goal.",
                () -> showRefactorDialog());
        addMode("2 · Scout improvements",
                "Find useful competitive and open-source capabilities.",
                () -> showScoutDialog());
        addMode("3 · Audit and repair",
                "Review the repository, fix verified defects, test, and land green work.",
                () -> showRunDialog(MobileRunRequest.Mode.AUDIT));
        addMode("4 · Make production ready",
                "Run the complete purpose, build, test, UX, and readiness pipeline.",
                () -> showRunDialog(MobileRunRequest.Mode.PRODREADY));

        content.addView(section("Latest run"));
        runState = text("No run has been started from this phone.", 15,
                Color.rgb(170, 181, 194));
        content.addView(runState);
        Button openRun = button("Open run details");
        openRun.setOnClickListener(view -> openLastRun());
        content.addView(openRun, margins(0, 6, 0, 0));
        refreshHeader();
        refreshRunLabel();
    }

    private void addMode(String title, String description, Runnable action) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(10), dp(16), dp(12));
        card.setBackgroundColor(Color.rgb(19, 26, 34));
        TextView heading = text(title, 18, Color.WHITE);
        TextView detail = text(description, 14, Color.rgb(156, 168, 181));
        Button launch = button("Open");
        launch.setOnClickListener(view -> action.run());
        card.addView(heading);
        card.addView(detail);
        card.addView(launch);
        content.addView(card, margins(0, 5, 0, 8));
    }

    private void refreshHeader() {
        if (accountState == null) return;
        String login = preferences.getString(LOGIN, "");
        if (configured()) {
            accountState.setText("GitHub: " + (login.isEmpty() ? "configured" : login)
                    + (secrets.contains(SecureStore.OPENAI_KEY)
                    ? " · Provider: OpenAI" : " · Provider: GitHub Copilot")
                    + " · No PC or Termux required");
            accountState.setTextColor(Color.rgb(63, 185, 80));
        } else {
            accountState.setText("One-time setup needed: GitHub token · OpenAI key optional");
            accountState.setTextColor(Color.rgb(248, 81, 73));
        }
        if (repositoryButton != null) {
            String repo = preferences.getString(REPOSITORY, "");
            String ref = preferences.getString(REF, "main");
            repositoryButton.setText(repo.isEmpty() ? "Choose repository" : repo + " · " + ref);
        }
    }

    private boolean configured() {
        return secrets.contains(SecureStore.GITHUB_TOKEN);
    }

    private void showCredentialSetup() {
        LinearLayout form = form();
        TextView guidance = text(
                "Enter your GitHub token once. FlexFactor uses GitHub Copilot by default. An OpenAI API key is optional and switches runs to OpenAI.",
                14, Color.rgb(170, 181, 194));
        EditText github = secretInput("GitHub token (repo and workflow access)");
        EditText openAi = secretInput("OpenAI API key (optional)");
        CheckBox useCopilot = new CheckBox(this);
        useCopilot.setText("Use GitHub Copilot (no OpenAI key)");
        useCopilot.setTextColor(Color.WHITE);
        useCopilot.setChecked(!secrets.contains(SecureStore.OPENAI_KEY));
        if (secrets.contains(SecureStore.GITHUB_TOKEN)) github.setHint("GitHub token already saved");
        if (secrets.contains(SecureStore.OPENAI_KEY)) openAi.setHint("OpenAI key already saved");
        form.addView(guidance);
        form.addView(github);
        form.addView(useCopilot);
        form.addView(openAi);

        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("FlexFactor setup")
                .setView(form)
                .setNegativeButton("Cancel", null)
                .setNeutralButton("Credential pages", null)
                .setPositiveButton("Verify and save", null)
                .create();
        dialog.setOnShowListener(ignored -> {
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(view ->
                    showCredentialLinks());
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
                String githubValue = github.getText().toString().trim();
                String openAiValue = openAi.getText().toString().trim();
                if (githubValue.isEmpty()) githubValue = secrets.get(SecureStore.GITHUB_TOKEN);
                if (useCopilot.isChecked()) {
                    openAiValue = "";
                } else if (openAiValue.isEmpty()) {
                    openAiValue = secrets.get(SecureStore.OPENAI_KEY);
                }
                if (githubValue.isEmpty()) {
                    github.setError("GitHub token is required.");
                    return;
                }
                if (!useCopilot.isChecked() && openAiValue.isEmpty()) {
                    openAi.setError("Enter an OpenAI key or select GitHub Copilot.");
                    return;
                }
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setText("Verifying…");
                configureCredentials(dialog, githubValue, openAiValue);
            });
        });
        dialog.show();
    }

    private void configureCredentials(AlertDialog dialog, String github, String openAi) {
        worker.execute(() -> {
            try {
                GitHubApi.ConfigurationResult result = api.configure(github, openAi);
                secrets.put(SecureStore.GITHUB_TOKEN, github);
                secrets.put(SecureStore.OPENAI_KEY, openAi);
                preferences.edit().putString(LOGIN, result.login).apply();
                post(() -> {
                    dialog.dismiss();
                    refreshHeader();
                    Toast.makeText(this, openAi.isEmpty()
                            ? "GitHub Copilot is selected" : "GitHub and OpenAI are ready",
                            Toast.LENGTH_LONG).show();
                });
            } catch (Exception failed) {
                post(() -> {
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(true);
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setText("Verify and save");
                    showError("Setup was not saved", safeMessage(failed));
                });
            }
        });
    }

    private void showCredentialLinks() {
        new AlertDialog.Builder(this)
                .setTitle("Create credentials")
                .setItems(new String[]{"Open GitHub token page", "Open OpenAI API key page"},
                        (dialog, which) -> openExternal(which == 0
                                ? "https://github.com/settings/tokens/new?scopes=repo,workflow&description=FlexFactor%20Android"
                                : "https://platform.openai.com/api-keys"))
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void chooseRepository() {
        if (!requireConfiguration()) return;
        repositoryButton.setEnabled(false);
        repositoryButton.setText("Loading repositories…");
        worker.execute(() -> {
            try {
                List<GitHubApi.Repository> repos = api.repositories(
                        secrets.get(SecureStore.GITHUB_TOKEN));
                post(() -> showRepositoryList(repos));
            } catch (Exception failed) {
                post(() -> {
                    repositoryButton.setEnabled(true);
                    refreshHeader();
                    showError("Repositories could not be loaded", safeMessage(failed));
                });
            }
        });
    }

    private void showRepositoryList(List<GitHubApi.Repository> repositories) {
        repositoryButton.setEnabled(true);
        refreshHeader();
        if (repositories.isEmpty()) {
            showError("No writable public repositories",
                    "FlexFactor Mobile runs only public targets from its public control repository.");
            return;
        }
        String[] labels = new String[repositories.size()];
        for (int i = 0; i < repositories.size(); i++) labels[i] = repositories.get(i).fullName;
        new AlertDialog.Builder(this)
                .setTitle("Choose repository")
                .setItems(labels, (dialog, which) -> {
                    GitHubApi.Repository selected = repositories.get(which);
                    preferences.edit()
                            .putString(REPOSITORY, selected.fullName)
                            .putString(REF, selected.defaultBranch)
                            .apply();
                    refreshHeader();
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void showRefactorDialog() {
        if (!requireReadyTarget()) return;
        LinearLayout form = form();
        EditText file = input("Repository-relative file, for example src/app.ts");
        EditText goal = input("What should this file do better?");
        goal.setMinLines(3);
        goal.setSingleLine(false);
        form.addView(file);
        form.addView(goal);
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Option 1 · Refactor")
                .setMessage("FlexFactor will refactor this file, verify the result, and publish the green change.")
                .setView(form)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Run FlexFactor", null)
                .create();
        dialog.setOnShowListener(ignored -> dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(view -> {
                    try {
                        MobileRunRequest request = request(MobileRunRequest.Mode.REFACTOR,
                                file.getText().toString(), goal.getText().toString(), false, 25);
                        dialog.dismiss();
                        confirmAndDispatch(request);
                    } catch (IllegalArgumentException rejected) {
                        showError("Check the run details", rejected.getMessage());
                    }
                }));
        dialog.show();
    }

    private void showScoutDialog() {
        if (!requireReadyTarget()) return;
        LinearLayout form = form();
        CheckBox apply = new CheckBox(this);
        apply.setText("Prepare and apply approved integration proposals");
        apply.setTextColor(Color.WHITE);
        apply.setChecked(false);
        form.addView(text("Report mode researches improvements without changing the target. Enable apply to process proposals through FlexFactor's approval and verification gates.",
                14, Color.rgb(170, 181, 194)));
        form.addView(apply);
        new AlertDialog.Builder(this)
                .setTitle("Option 2 · Scout")
                .setView(form)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Run FlexFactor", (dialog, which) -> {
                    try {
                        confirmAndDispatch(request(MobileRunRequest.Mode.SCOUT,
                                "", "", apply.isChecked(), 25));
                    } catch (IllegalArgumentException rejected) {
                        showError("Check the run details", rejected.getMessage());
                    }
                })
                .show();
    }

    private void showRunDialog(MobileRunRequest.Mode mode) {
        if (!requireReadyTarget()) return;
        EditText cost = input(secrets.contains(SecureStore.OPENAI_KEY)
                ? "Maximum OpenAI cost in USD (1–150)"
                : "Maximum provider budget (1–150)");
        cost.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
        cost.setText(mode == MobileRunRequest.Mode.PRODREADY ? "150" : "50");
        LinearLayout form = form();
        form.addView(cost);
        String title = mode == MobileRunRequest.Mode.AUDIT
                ? "Option 3 · Audit and repair" : "Option 4 · Production ready";
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage("Verified green work may be committed, pushed, and merged by the FlexFactor engine.")
                .setView(form)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Run FlexFactor", (dialog, which) -> {
                    try {
                        double cap = Double.parseDouble(cost.getText().toString().trim());
                        confirmAndDispatch(request(mode, "", "", false, cap));
                    } catch (NumberFormatException rejected) {
                        showError("Check the cost cap", "Enter a number from 1 through 150.");
                    } catch (IllegalArgumentException rejected) {
                        showError("Check the run details", rejected.getMessage());
                    }
                })
                .show();
    }

    private MobileRunRequest request(MobileRunRequest.Mode mode, String file, String goal,
            boolean scoutApply, double cost) {
        MobileRunRequest.Provider provider = secrets.contains(SecureStore.OPENAI_KEY)
                ? MobileRunRequest.Provider.OPENAI : MobileRunRequest.Provider.COPILOT;
        return new MobileRunRequest(mode, provider,
                preferences.getString(REPOSITORY, ""),
                preferences.getString(REF, "main"),
                file, goal, scoutApply, cost);
    }

    private void confirmAndDispatch(MobileRunRequest request) {
        String detail = request.repository + " · " + request.ref;
        if (request.mode == MobileRunRequest.Mode.AUDIT
                || request.mode == MobileRunRequest.Mode.PRODREADY) {
            detail += "\nMaximum provider cost: $"
                    + String.format(Locale.US, "%.2f", request.maxCost);
        } else {
            detail += "\nProvider: " + (request.provider == MobileRunRequest.Provider.COPILOT
                    ? "GitHub Copilot" : "OpenAI");
        }
        new AlertDialog.Builder(this)
                .setTitle("Start " + request.mode.wire + "?")
                .setMessage(detail)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Start", (dialog, which) -> dispatch(request))
                .show();
    }

    private void dispatch(MobileRunRequest request) {
        runState.setText("Submitting " + request.mode.wire + " to GitHub Actions…");
        worker.execute(() -> {
            try {
                GitHubApi.RunState state = api.dispatch(
                        secrets.get(SecureStore.GITHUB_TOKEN), request);
                preferences.edit()
                        .putLong(LAST_RUN_ID, state.id)
                        .putString(LAST_RUN_URL, state.htmlUrl)
                        .putString(LAST_RUN_STATUS, "Queued · " + request.repository)
                        .apply();
                post(() -> {
                    refreshRunLabel();
                    pollLastRun();
                });
            } catch (Exception failed) {
                post(() -> showError("FlexFactor did not start", safeMessage(failed)));
            }
        });
    }

    private void pollLastRun() {
        main.removeCallbacks(pollRun);
        long id = preferences.getLong(LAST_RUN_ID, 0L);
        if (id <= 0 || !configured() || destroyed) return;
        worker.execute(() -> {
            try {
                GitHubApi.RunState state = api.run(secrets.get(SecureStore.GITHUB_TOKEN), id);
                String label = state.complete()
                        ? ("success".equals(state.conclusion) ? "Completed successfully" :
                        "Completed: " + state.conclusion)
                        : capitalize(state.status) + " · " + state.currentStep;
                preferences.edit()
                        .putString(LAST_RUN_URL, state.htmlUrl)
                        .putString(LAST_RUN_STATUS, label)
                        .apply();
                post(() -> {
                    refreshRunLabel();
                    if (!state.complete()) {
                        main.postDelayed(pollRun, POLL_MS);
                    } else if ("success".equals(state.conclusion)) {
                        Toast.makeText(this, "FlexFactor completed successfully",
                                Toast.LENGTH_LONG).show();
                    }
                });
            } catch (Exception failed) {
                preferences.edit().putString(LAST_RUN_STATUS,
                        "Status check failed · tap run details").apply();
                post(() -> {
                    refreshRunLabel();
                    main.postDelayed(pollRun, POLL_MS * 2);
                });
            }
        });
    }

    private void refreshRunLabel() {
        if (runState == null) return;
        long id = preferences.getLong(LAST_RUN_ID, 0L);
        String status = preferences.getString(LAST_RUN_STATUS, "");
        runState.setText(id <= 0 ? "No run has been started from this phone."
                : (status.isEmpty() ? "Run #" + id : status + " · run #" + id));
        runState.setTextColor(status.startsWith("Completed successfully")
                ? Color.rgb(63, 185, 80) : Color.rgb(170, 181, 194));
    }

    private boolean requireConfiguration() {
        if (configured()) return true;
        showCredentialSetup();
        return false;
    }

    private boolean requireReadyTarget() {
        if (!requireConfiguration()) return false;
        if (!preferences.getString(REPOSITORY, "").isEmpty()) return true;
        Toast.makeText(this, "Choose a repository first", Toast.LENGTH_LONG).show();
        chooseRepository();
        return false;
    }

    private void openLastRun() {
        String url = preferences.getString(LAST_RUN_URL, "");
        if (url.isEmpty()) {
            Toast.makeText(this, "No GitHub run is available yet", Toast.LENGTH_LONG).show();
            return;
        }
        openExternal(url);
    }

    private void startUpdate() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && !getPackageManager().canRequestPackageInstalls()) {
            new AlertDialog.Builder(this)
                    .setTitle("Allow FlexFactor updates")
                    .setMessage("Enable Allow from this source, then tap Update again. Android will still ask you to confirm every signed installation.")
                    .setNegativeButton("Cancel", null)
                    .setPositiveButton("Open settings", (dialog, which) -> startActivity(new Intent(
                            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + getPackageName()))))
                    .show();
            return;
        }
        updateButton.setEnabled(false);
        updateButton.setText("Checking…");
        new AppUpdater(this).checkAndInstall(new AppUpdater.Callback() {
            @Override public void onUpToDate(String versionName) {
                resetUpdateButton();
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("FlexFactor is current")
                        .setMessage("Version " + versionName + " is the latest signed release.")
                        .setPositiveButton("OK", null).show();
            }
            @Override public void onInstallerReady(String versionName) {
                resetUpdateButton();
                Toast.makeText(MainActivity.this,
                        "Version " + versionName + " verified. Confirm the Android install.",
                        Toast.LENGTH_LONG).show();
            }
            @Override public void onError(String message) {
                resetUpdateButton();
                showError("Update not installed", message);
            }
        });
    }

    private void resetUpdateButton() {
        if (updateButton != null) {
            updateButton.setText("Update");
            updateButton.setEnabled(true);
        }
    }

    private void showError(String title, String message) {
        if (destroyed || isFinishing()) return;
        new AlertDialog.Builder(this).setTitle(title).setMessage(message)
                .setPositiveButton("OK", null).show();
    }

    private void openExternal(String value) {
        Uri uri = Uri.parse(value);
        if (!"https".equals(uri.getScheme())) {
            showError("Link blocked", "FlexFactor opens HTTPS links only.");
            return;
        }
        startActivity(new Intent(Intent.ACTION_VIEW, uri));
    }

    private String installedVersion() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            return info.versionName == null ? "unknown" : info.versionName;
        } catch (PackageManager.NameNotFoundException impossible) {
            return "unknown";
        }
    }

    private void post(Runnable action) {
        main.post(() -> {
            if (!destroyed && !isFinishing()) action.run();
        });
    }

    private static String safeMessage(Exception error) {
        String value = error.getMessage();
        if (value == null || value.trim().isEmpty()) return error.getClass().getSimpleName();
        return value.length() > 300 ? value.substring(0, 300) : value;
    }

    private static String capitalize(String value) {
        if (value == null || value.isEmpty()) return "Unknown";
        return Character.toUpperCase(value.charAt(0)) + value.substring(1);
    }

    private LinearLayout form() {
        LinearLayout form = new LinearLayout(this);
        form.setOrientation(LinearLayout.VERTICAL);
        form.setPadding(dp(22), 0, dp(22), 0);
        return form;
    }

    private EditText input(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setTextColor(Color.WHITE);
        input.setHintTextColor(Color.rgb(125, 133, 144));
        input.setSingleLine(true);
        return input;
    }

    private EditText secretInput(String hint) {
        EditText input = input(hint);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        return input;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        return button;
    }

    private TextView section(String value) {
        TextView view = text(value, 14, Color.rgb(88, 166, 255));
        view.setAllCaps(true);
        view.setLetterSpacing(0.06f);
        view.setPadding(0, dp(18), 0, dp(4));
        return view;
    }

    private TextView text(String value, int sp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        view.setPadding(0, dp(5), 0, dp(5));
        return view;
    }

    private LinearLayout.LayoutParams weighted() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        params.setMargins(dp(4), 0, dp(4), 0);
        return params;
    }

    private LinearLayout.LayoutParams margins(int left, int top, int right, int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.setMargins(dp(left), dp(top), dp(right), dp(bottom));
        return params;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
