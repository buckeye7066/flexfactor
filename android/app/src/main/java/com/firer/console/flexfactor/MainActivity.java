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

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
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
    private static final String PROVIDER = "selected_provider";
    private static final String LAST_RUN_ID = "last_run_id";
    private static final String LAST_RUN_REPOSITORY = "last_run_repository";
    private static final String LAST_RUN_REQUEST_ID = "last_run_request_id";
    private static final String LAST_RUN_MODE = "last_run_mode";
    private static final String LAST_RUN_URL = "last_run_url";
    private static final String LAST_RUN_STATUS = "last_run_status";
    private static final String RUN_HISTORY = "run_history";
    private static final long POLL_MS = 5_000L;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final GitHubApi api = new GitHubApi();
    private final Runnable pollRun = this::pollLastRun;

    private SecureStore secrets;
    private SharedPreferences preferences;
    private LinearLayout content;
    private Button repositoryButton;
    private Button providerButton;
    private Button updateButton;
    private TextView accountState;
    private TextView runState;
    private boolean destroyed;
    private boolean polling;
    private boolean pendingStartupUpdate;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences(PREFERENCES, MODE_PRIVATE);
        secrets = new SecureStore(this);
        if (preferences.getLong(LAST_RUN_ID, 0L) > 0L
                && !preferences.contains(LAST_RUN_REPOSITORY)) {
            preferences.edit().putString(
                    LAST_RUN_REPOSITORY, GitHubApi.CONTROL_REPOSITORY).apply();
        }
        renderHome();
        if (!configured()) main.postDelayed(this::showCredentialSetup, 350L);
        if (directUpdatesEnabled()) {
            main.postDelayed(this::checkForUpdateOnLaunch, 1_500L);
        }
        if (preferences.getLong(LAST_RUN_ID, 0L) > 0L) pollLastRun();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshHeader();
        if (preferences.getLong(LAST_RUN_ID, 0L) > 0L) pollLastRun();
        if (directUpdatesEnabled() && pendingStartupUpdate
                && (Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                || getPackageManager().canRequestPackageInstalls())) {
            pendingStartupUpdate = false;
            startUpdate();
        }
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
        if (directUpdatesEnabled()) top.addView(updateButton, weighted());
        content.addView(top, margins(0, 12, 0, 8));

        accountState = text("", 14, Color.rgb(170, 181, 194));
        content.addView(accountState);

        content.addView(section("Repository"));
        repositoryButton = button("Choose repository");
        repositoryButton.setContentDescription("Choose a GitHub repository");
        repositoryButton.setOnClickListener(view -> chooseRepository());
        content.addView(repositoryButton, margins(0, 4, 0, 12));

        content.addView(section("Model provider"));
        providerButton = button("Choose provider");
        providerButton.setContentDescription("Choose a model provider");
        providerButton.setOnClickListener(view -> chooseProvider());
        content.addView(providerButton, margins(0, 4, 0, 12));

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
        Button viewResults = button("View results and error ledger");
        viewResults.setOnClickListener(view -> viewLastRunResults());
        content.addView(viewResults, margins(0, 6, 0, 0));
        Button steerRun = button("Steer this build");
        steerRun.setOnClickListener(view -> steerLastRun());
        content.addView(steerRun, margins(0, 6, 0, 0));
        Button recentRuns = button("Active and recent runs");
        recentRuns.setOnClickListener(view -> showRunHistory());
        content.addView(recentRuns, margins(0, 6, 0, 0));
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
                    + " · Provider: " + providerLabel(selectedProvider())
                    + " · No PC or Termux required");
            accountState.setTextColor(Color.rgb(63, 185, 80));
        } else {
            accountState.setText("One-time setup needed: GitHub token · OpenAI/Anthropic optional");
            accountState.setTextColor(Color.rgb(248, 81, 73));
        }
        if (repositoryButton != null) {
            String repo = preferences.getString(REPOSITORY, "");
            String ref = preferences.getString(REF, "main");
            repositoryButton.setText(repo.isEmpty() ? "Choose repository" : repo + " · " + ref);
        }
        if (providerButton != null) providerButton.setText(providerLabel(selectedProvider()));
    }

    private MobileRunRequest.Provider selectedProvider() {
        String saved = preferences.getString(PROVIDER, "");
        for (MobileRunRequest.Provider provider : MobileRunRequest.Provider.values()) {
            if (provider.wire.equals(saved)) return provider;
        }
        return secrets.contains(SecureStore.OPENAI_KEY)
                ? MobileRunRequest.Provider.OPENAI : MobileRunRequest.Provider.OLLAMA;
    }

    private static String providerLabel(MobileRunRequest.Provider provider) {
        if (provider == MobileRunRequest.Provider.OPENAI) return "OpenAI";
        if (provider == MobileRunRequest.Provider.ANTHROPIC) return "Anthropic";
        if (provider == MobileRunRequest.Provider.COPILOT) return "GitHub Copilot";
        return "Hosted open model";
    }

    private void chooseProvider() {
        String[] labels = {"OpenAI", "Anthropic", "GitHub Copilot", "Hosted open model"};
        MobileRunRequest.Provider[] providers = {
                MobileRunRequest.Provider.OPENAI,
                MobileRunRequest.Provider.ANTHROPIC,
                MobileRunRequest.Provider.COPILOT,
                MobileRunRequest.Provider.OLLAMA,
        };
        new AlertDialog.Builder(this)
                .setTitle("Choose provider")
                .setSingleChoiceItems(labels, indexOfProvider(providers, selectedProvider()),
                        (dialog, which) -> {
                            preferences.edit().putString(PROVIDER, providers[which].wire).apply();
                            dialog.dismiss();
                            refreshHeader();
                        })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private static int indexOfProvider(MobileRunRequest.Provider[] values,
            MobileRunRequest.Provider selected) {
        for (int i = 0; i < values.length; i++) if (values[i] == selected) return i;
        return 0;
    }

    private boolean configured() {
        return secrets.contains(SecureStore.GITHUB_TOKEN);
    }

    private void showCredentialSetup() {
        LinearLayout form = form();
        TextView guidance = text(
                "Enter your GitHub token once. OpenAI and Anthropic keys are optional; GitHub Copilot and the hosted open model need no vendor key.",
                14, Color.rgb(170, 181, 194));
        EditText github = secretInput("GitHub token (repo and workflow access)");
        EditText openAi = secretInput("OpenAI API key (optional)");
        EditText anthropic = secretInput("Anthropic API key (optional)");
        if (secrets.contains(SecureStore.GITHUB_TOKEN)) github.setHint("GitHub token already saved");
        if (secrets.contains(SecureStore.OPENAI_KEY)) openAi.setHint("OpenAI key already saved");
        if (secrets.contains(SecureStore.ANTHROPIC_KEY)) {
            anthropic.setHint("Anthropic key already saved");
        }
        form.addView(guidance);
        form.addView(github);
        form.addView(openAi);
        form.addView(anthropic);

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
                String anthropicValue = anthropic.getText().toString().trim();
                if (githubValue.isEmpty()) githubValue = secrets.get(SecureStore.GITHUB_TOKEN);
                if (openAiValue.isEmpty()) openAiValue = secrets.get(SecureStore.OPENAI_KEY);
                if (anthropicValue.isEmpty()) {
                    anthropicValue = secrets.get(SecureStore.ANTHROPIC_KEY);
                }
                if (githubValue.isEmpty()) {
                    github.setError("GitHub token is required.");
                    return;
                }
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setText("Verifying…");
                configureCredentials(dialog, githubValue, openAiValue, anthropicValue);
            });
        });
        dialog.show();
    }

    private void configureCredentials(AlertDialog dialog, String github, String openAi,
            String anthropic) {
        worker.execute(() -> {
            try {
                GitHubApi.ConfigurationResult result = api.configure(github, openAi, anthropic);
                secrets.put(SecureStore.GITHUB_TOKEN, github);
                secrets.put(SecureStore.OPENAI_KEY, openAi);
                secrets.put(SecureStore.ANTHROPIC_KEY, anthropic);
                preferences.edit().putString(LOGIN, result.login).apply();
                post(() -> {
                    dialog.dismiss();
                    refreshHeader();
                    Toast.makeText(this, "FlexFactor credentials are ready",
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
                .setItems(new String[]{"Open GitHub token page", "Open OpenAI API key page",
                                "Open Anthropic API key page"},
                        (dialog, which) -> openExternal(which == 0
                                ? "https://github.com/settings/tokens/new?scopes=repo,workflow&description=FlexFactor%20Android"
                                : which == 1 ? "https://platform.openai.com/api-keys"
                                : "https://console.anthropic.com/settings/keys"))
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
            showError("No writable repositories",
                    "The GitHub token did not return a repository FlexFactor can update.");
            return;
        }
        String[] labels = new String[repositories.size()];
        for (int i = 0; i < repositories.size(); i++) {
            GitHubApi.Repository repository = repositories.get(i);
            labels[i] = repository.fullName + (repository.isPrivate ? " · private" : " · public");
        }
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
        EditText threshold = input("Acceptance threshold (0–100)");
        threshold.setInputType(InputType.TYPE_CLASS_NUMBER);
        threshold.setText("90");
        EditText iterations = input("Maximum refactor iterations (1–20)");
        iterations.setInputType(InputType.TYPE_CLASS_NUMBER);
        iterations.setText("5");
        form.addView(file);
        form.addView(goal);
        form.addView(threshold);
        form.addView(iterations);
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
                        MobileRunRequest request = new MobileRunRequest(
                                MobileRunRequest.Mode.REFACTOR, selectedProvider(),
                                preferences.getString(REPOSITORY, ""),
                                preferences.getString(REF, "main"),
                                file.getText().toString(), goal.getText().toString(), false, 25,
                                Integer.parseInt(threshold.getText().toString().trim()),
                                Integer.parseInt(iterations.getText().toString().trim()),
                                true, true);
                        dialog.dismiss();
                        confirmAndDispatch(request);
                    } catch (NumberFormatException rejected) {
                        showError("Check refactor settings", "Threshold and iterations must be numbers.");
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
        CheckBox economy = new CheckBox(this);
        economy.setText("Economy author model (desktop default)");
        economy.setTextColor(Color.WHITE);
        economy.setChecked(true);
        CheckBox useBoth = new CheckBox(this);
        useBoth.setText("Use independent cross-model verification when available");
        useBoth.setTextColor(Color.WHITE);
        useBoth.setChecked(true);
        CheckBox batch = new CheckBox(this);
        batch.setText("Run up to 10 repositories in parallel");
        batch.setTextColor(Color.WHITE);
        batch.setChecked(false);
        form.addView(economy);
        form.addView(useBoth);
        form.addView(batch);
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
                        if (batch.isChecked()) {
                            chooseBatchRepositories(mode, cap,
                                    economy.isChecked(), useBoth.isChecked());
                        } else {
                            confirmAndDispatch(request(mode, "", "", false, cap,
                                    economy.isChecked(), useBoth.isChecked()));
                        }
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
        return request(mode, file, goal, scoutApply, cost, true, true);
    }

    private MobileRunRequest request(MobileRunRequest.Mode mode, String file, String goal,
            boolean scoutApply, double cost, boolean economy, boolean useBoth) {
        return new MobileRunRequest(mode, selectedProvider(),
                preferences.getString(REPOSITORY, ""),
                preferences.getString(REF, "main"),
                file, goal, scoutApply, cost, 90, 5, economy, useBoth);
    }

    private void confirmAndDispatch(MobileRunRequest request) {
        String detail = request.repository + " · " + request.ref;
        detail += "\nProvider: " + providerLabel(request.provider);
        if (request.mode == MobileRunRequest.Mode.AUDIT
                || request.mode == MobileRunRequest.Mode.PRODREADY) {
            detail += "\nMaximum provider cost: $"
                    + String.format(Locale.US, "%.2f", request.maxCost);
            detail += "\nCross-model verification: " + (request.useBoth ? "on" : "off");
        }
        new AlertDialog.Builder(this)
                .setTitle("Start " + request.mode.wire + "?")
                .setMessage(detail)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Start", (dialog, which) -> dispatch(request))
                .show();
    }

    private void chooseBatchRepositories(MobileRunRequest.Mode mode, double cost,
            boolean economy, boolean useBoth) {
        if (!requireConfiguration()) return;
        runState.setText("Loading repositories for the parallel run…");
        worker.execute(() -> {
            try {
                List<GitHubApi.Repository> repositories = api.repositories(
                        secrets.get(SecureStore.GITHUB_TOKEN));
                post(() -> showBatchRepositoryList(
                        repositories, mode, cost, economy, useBoth));
            } catch (Exception failed) {
                post(() -> {
                    refreshRunLabel();
                    showError("Repositories could not be loaded", safeMessage(failed));
                });
            }
        });
    }

    private void showBatchRepositoryList(List<GitHubApi.Repository> repositories,
            MobileRunRequest.Mode mode, double cost, boolean economy, boolean useBoth) {
        refreshRunLabel();
        if (repositories.isEmpty()) {
            showError("No writable repositories",
                    "The GitHub token did not return a repository FlexFactor can update.");
            return;
        }
        String[] labels = new String[repositories.size()];
        boolean[] checked = new boolean[repositories.size()];
        String selected = preferences.getString(REPOSITORY, "");
        for (int i = 0; i < repositories.size(); i++) {
            GitHubApi.Repository repository = repositories.get(i);
            labels[i] = repository.fullName + (repository.isPrivate ? " · private" : " · public");
            checked[i] = repository.fullName.equals(selected);
        }
        new AlertDialog.Builder(this)
                .setTitle("Choose up to 10 repositories")
                .setMultiChoiceItems(labels, checked,
                        (dialog, which, isChecked) -> checked[which] = isChecked)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Continue", (dialog, which) -> {
                    List<MobileRunRequest> requests = new ArrayList<>();
                    for (int i = 0; i < checked.length; i++) {
                        if (!checked[i]) continue;
                        GitHubApi.Repository repository = repositories.get(i);
                        requests.add(new MobileRunRequest(mode, selectedProvider(),
                                repository.fullName, repository.defaultBranch,
                                "", "", false, cost, 90, 5, economy, useBoth));
                    }
                    if (requests.isEmpty() || requests.size() > 10) {
                        showError("Check the parallel run",
                                "Choose from 1 through 10 repositories.");
                        return;
                    }
                    confirmAndDispatchBatch(requests);
                })
                .show();
    }

    private void confirmAndDispatchBatch(List<MobileRunRequest> requests) {
        MobileRunRequest first = requests.get(0);
        new AlertDialog.Builder(this)
                .setTitle("Start " + requests.size() + " parallel " + first.mode.wire + " runs?")
                .setMessage("Each repository gets an independent, private-aware FlexFactor workflow and run. The app will keep every run in Active and recent runs.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Start all", (dialog, which) -> dispatchBatch(requests))
                .show();
    }

    private void dispatchBatch(List<MobileRunRequest> requests) {
        runState.setText("Starting 0 of " + requests.size() + " repositories…");
        worker.execute(() -> {
            int started = 0;
            List<String> failures = new ArrayList<>();
            for (MobileRunRequest request : requests) {
                try {
                    GitHubApi.RunState state = api.dispatch(
                            secrets.get(SecureStore.GITHUB_TOKEN),
                            secrets.get(SecureStore.OPENAI_KEY),
                            secrets.get(SecureStore.ANTHROPIC_KEY), request);
                    recordRun(state, request);
                    preferences.edit()
                            .putLong(LAST_RUN_ID, state.id)
                            .putString(LAST_RUN_REPOSITORY, request.repository)
                            .putString(LAST_RUN_REQUEST_ID, request.requestId)
                            .putString(LAST_RUN_MODE, request.mode.wire)
                            .putString(LAST_RUN_URL, state.htmlUrl)
                            .putString(LAST_RUN_STATUS, "Queued · " + request.repository)
                            .apply();
                    started++;
                    int progress = started;
                    post(() -> runState.setText("Started " + progress + " of "
                            + requests.size() + " repositories…"));
                } catch (Exception failed) {
                    failures.add(request.repository + ": " + safeMessage(failed));
                }
            }
            int totalStarted = started;
            post(() -> {
                refreshRunLabel();
                pollLastRun();
                if (!failures.isEmpty()) {
                    showError("Some parallel runs did not start",
                            totalStarted + " started.\n\n" + String.join("\n", failures));
                } else {
                    Toast.makeText(this, "All " + totalStarted
                            + " FlexFactor runs started", Toast.LENGTH_LONG).show();
                }
            });
        });
    }

    private void dispatch(MobileRunRequest request) {
        runState.setText("Submitting " + request.mode.wire + " to GitHub Actions…");
        worker.execute(() -> {
            try {
                GitHubApi.RunState state = api.dispatch(
                        secrets.get(SecureStore.GITHUB_TOKEN),
                        secrets.get(SecureStore.OPENAI_KEY),
                        secrets.get(SecureStore.ANTHROPIC_KEY), request);
                preferences.edit()
                        .putLong(LAST_RUN_ID, state.id)
                        .putString(LAST_RUN_REPOSITORY, request.repository)
                        .putString(LAST_RUN_REQUEST_ID, request.requestId)
                        .putString(LAST_RUN_MODE, request.mode.wire)
                        .putString(LAST_RUN_URL, state.htmlUrl)
                        .putString(LAST_RUN_STATUS, "Queued · " + request.repository)
                        .apply();
                recordRun(state, request);
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
        if (!configured() || destroyed || polling) return;
        List<RunRecord> records = runHistory();
        if (records.isEmpty()) {
            long id = preferences.getLong(LAST_RUN_ID, 0L);
            String repository = lastRunRepository();
            if (id > 0 && !repository.isEmpty()) {
                records.add(new RunRecord(id, repository,
                        preferences.getString(LAST_RUN_REQUEST_ID, ""),
                        preferences.getString(LAST_RUN_MODE, ""),
                        preferences.getString(LAST_RUN_URL, ""),
                        preferences.getString(LAST_RUN_STATUS, "Queued"), false));
            }
        }
        if (records.isEmpty()) return;
        polling = true;
        worker.execute(() -> {
            boolean active = false;
            boolean latestSucceeded = false;
            long latestId = preferences.getLong(LAST_RUN_ID, 0L);
            List<RunRecord> updated = new ArrayList<>();
            for (RunRecord record : records) {
                RunRecord next = record;
                if (!record.complete) {
                    try {
                        GitHubApi.RunState state = api.run(
                                secrets.get(SecureStore.GITHUB_TOKEN), record.repository,
                                record.id);
                        String label = state.complete()
                                ? ("success".equals(state.conclusion)
                                ? "Completed successfully" : "Completed: " + state.conclusion)
                                : capitalize(state.status) + " · " + state.currentStep;
                        next = new RunRecord(record.id, record.repository, record.requestId,
                                record.mode, state.htmlUrl, label, state.complete());
                        if (record.id == latestId) {
                            preferences.edit()
                                    .putString(LAST_RUN_URL, state.htmlUrl)
                                    .putString(LAST_RUN_STATUS, label).apply();
                            latestSucceeded = state.complete()
                                    && "success".equals(state.conclusion);
                        }
                    } catch (Exception failed) {
                        next = new RunRecord(record.id, record.repository, record.requestId,
                                record.mode, record.url, "Status check will retry", false);
                    }
                }
                if (!next.complete) active = true;
                updated.add(next);
            }
            saveRunHistory(updated);
            boolean pollAgain = active;
            boolean toast = latestSucceeded;
            post(() -> {
                polling = false;
                refreshRunLabel();
                if (pollAgain) main.postDelayed(pollRun, POLL_MS);
                if (toast) Toast.makeText(this, "FlexFactor completed successfully",
                        Toast.LENGTH_LONG).show();
            });
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

    private void showRunHistory() {
        List<RunRecord> records = runHistory();
        if (records.isEmpty()) {
            Toast.makeText(this, "No FlexFactor runs have been started", Toast.LENGTH_LONG).show();
            return;
        }
        String[] labels = new String[records.size()];
        for (int i = 0; i < records.size(); i++) {
            RunRecord record = records.get(i);
            labels[i] = record.repository + " · " + record.mode + "\n" + record.status
                    + " · run #" + record.id;
        }
        new AlertDialog.Builder(this)
                .setTitle("Active and recent runs")
                .setItems(labels, (dialog, which) -> selectRun(records.get(which)))
                .setNegativeButton("Close", null)
                .show();
    }

    private void selectRun(RunRecord record) {
        preferences.edit()
                .putLong(LAST_RUN_ID, record.id)
                .putString(LAST_RUN_REPOSITORY, record.repository)
                .putString(LAST_RUN_REQUEST_ID, record.requestId)
                .putString(LAST_RUN_MODE, record.mode)
                .putString(LAST_RUN_URL, record.url)
                .putString(LAST_RUN_STATUS, record.status)
                .apply();
        refreshRunLabel();
        new AlertDialog.Builder(this)
                .setTitle(record.repository + " · #" + record.id)
                .setMessage(record.status)
                .setNegativeButton("Close", null)
                .setNeutralButton("Open on GitHub", (dialog, which) -> openLastRun())
                .setPositiveButton("View result", (dialog, which) -> viewLastRunResults())
                .show();
    }

    private synchronized void recordRun(GitHubApi.RunState state, MobileRunRequest request) {
        List<RunRecord> records = runHistory();
        records.removeIf(item -> item.id == state.id);
        records.add(0, new RunRecord(state.id, request.repository, request.requestId,
                request.mode.wire, state.htmlUrl, "Queued · " + request.repository, false));
        saveRunHistory(records);
    }

    private synchronized List<RunRecord> runHistory() {
        List<RunRecord> records = new ArrayList<>();
        String raw = preferences.getString(RUN_HISTORY, "[]");
        try {
            JSONArray rows = new JSONArray(raw);
            for (int i = 0; i < rows.length() && records.size() < 10; i++) {
                JSONObject row = rows.optJSONObject(i);
                if (row == null) continue;
                long id = row.optLong("id", 0L);
                String repository = row.optString("repository", "");
                if (id <= 0 || repository.isEmpty()) continue;
                records.add(new RunRecord(id, repository,
                        row.optString("request_id", ""), row.optString("mode", ""),
                        row.optString("url", ""), row.optString("status", "Queued"),
                        row.optBoolean("complete", false)));
            }
        } catch (Exception ignored) {
            // A damaged local history never prevents a new authoritative run.
        }
        return records;
    }

    private synchronized void saveRunHistory(List<RunRecord> records) {
        JSONArray rows = new JSONArray();
        for (int i = 0; i < records.size() && i < 10; i++) {
            RunRecord record = records.get(i);
            JSONObject row = new JSONObject();
            try {
                row.put("id", record.id);
                row.put("repository", record.repository);
                row.put("request_id", record.requestId);
                row.put("mode", record.mode);
                row.put("url", record.url);
                row.put("status", record.status);
                row.put("complete", record.complete);
                rows.put(row);
            } catch (Exception ignored) {
                // org.json only rejects non-finite numeric values; none are stored here.
            }
        }
        preferences.edit().putString(RUN_HISTORY, rows.toString()).apply();
    }

    private static final class RunRecord {
        final long id;
        final String repository;
        final String requestId;
        final String mode;
        final String url;
        final String status;
        final boolean complete;

        RunRecord(long id, String repository, String requestId, String mode,
                String url, String status, boolean complete) {
            this.id = id;
            this.repository = repository;
            this.requestId = requestId;
            this.mode = mode;
            this.url = url;
            this.status = status;
            this.complete = complete;
        }
    }

    private void viewLastRunResults() {
        long id = preferences.getLong(LAST_RUN_ID, 0L);
        String repository = lastRunRepository();
        if (id <= 0 || repository.isEmpty()) {
            Toast.makeText(this, "No FlexFactor run is available yet", Toast.LENGTH_LONG).show();
            return;
        }
        runState.setText("Loading the run result and error ledger…");
        worker.execute(() -> {
            try {
                GitHubApi.RunDetails details = api.runDetails(
                        secrets.get(SecureStore.GITHUB_TOKEN), repository, id);
                post(() -> {
                    refreshRunLabel();
                    TextView body = text(details.displayText(), 14, Color.WHITE);
                    body.setTextIsSelectable(true);
                    body.setPadding(dp(12), dp(8), dp(12), dp(8));
                    ScrollView scroll = new ScrollView(this);
                    scroll.addView(body);
                    new AlertDialog.Builder(this)
                            .setTitle("FlexFactor run #" + id)
                            .setView(scroll)
                            .setNegativeButton("Close", null)
                            .setPositiveButton("Open on GitHub", (dialog, which) -> openLastRun())
                            .show();
                });
            } catch (Exception failed) {
                post(() -> {
                    refreshRunLabel();
                    showError("Run details are not ready", safeMessage(failed));
                });
            }
        });
    }

    private void steerLastRun() {
        long id = preferences.getLong(LAST_RUN_ID, 0L);
        String repository = preferences.getString(LAST_RUN_REPOSITORY, "");
        String requestId = preferences.getString(LAST_RUN_REQUEST_ID, "");
        String mode = preferences.getString(LAST_RUN_MODE, "");
        if (id <= 0 || repository.isEmpty() || requestId.isEmpty()) {
            Toast.makeText(this, "No active FlexFactor build is available", Toast.LENGTH_LONG)
                    .show();
            return;
        }
        if (!"audit".equals(mode) && !"prodready".equals(mode)) {
            showError("Steering is not available for this mode",
                    "Live steering applies to audit and production-ready builds.");
            return;
        }
        EditText comment = input("Tell FlexFactor what this build needs");
        comment.setMinLines(4);
        comment.setSingleLine(false);
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Steer run #" + id)
                .setMessage("Your authenticated comment will be interpreted at the next audit phase boundary and kept inside this repository's verification gates.")
                .setView(comment)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Send to active build", null)
                .create();
        dialog.setOnShowListener(ignored -> dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(view -> {
                    String value = comment.getText().toString().trim();
                    if (value.isEmpty()) {
                        comment.setError("Enter a steering comment.");
                        return;
                    }
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
                    worker.execute(() -> {
                        try {
                            api.submitSteering(secrets.get(SecureStore.GITHUB_TOKEN),
                                    repository, requestId, value);
                            post(() -> {
                                dialog.dismiss();
                                Toast.makeText(this,
                                        "Steering queued for the active FlexFactor build",
                                        Toast.LENGTH_LONG).show();
                            });
                        } catch (Exception failed) {
                            post(() -> {
                                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(true);
                                showError("Steering was not queued", safeMessage(failed));
                            });
                        }
                    });
                }));
        dialog.show();
    }

    private String lastRunRepository() {
        if (!preferences.contains(LAST_RUN_REPOSITORY)) {
            return GitHubApi.CONTROL_REPOSITORY;
        }
        return preferences.getString(LAST_RUN_REPOSITORY, GitHubApi.CONTROL_REPOSITORY);
    }

    private void startUpdate() {
        if (!directUpdatesEnabled()) return;
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

    private void checkForUpdateOnLaunch() {
        if (!directUpdatesEnabled()) return;
        if (destroyed || isFinishing()) return;
        new AppUpdater(this).check(new AppUpdater.CheckCallback() {
            @Override public void onUpToDate(String versionName) {
                // Startup checks are silent when the installed package is current.
            }
            @Override public void onUpdateAvailable(String versionName) {
                if (destroyed || isFinishing()) return;
                pendingStartupUpdate = true;
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                        || getPackageManager().canRequestPackageInstalls()) {
                    pendingStartupUpdate = false;
                    startUpdate();
                    return;
                }
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("FlexFactor " + versionName + " is available")
                        .setMessage("Android needs Allow from this source before FlexFactor can install its verified signed update.")
                        .setNegativeButton("Later", null)
                        .setPositiveButton("Allow updates", (dialog, which) -> startActivity(
                                new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                        Uri.parse("package:" + getPackageName()))))
                        .show();
            }
            @Override public void onError(String message) {
                // The explicit Update button remains the visible recovery path.
            }
        });
    }

    private void resetUpdateButton() {
        if (updateButton != null) {
            updateButton.setText("Update");
            updateButton.setEnabled(true);
        }
    }

    private static boolean directUpdatesEnabled() {
        return !"play".equals(BuildConfig.BUILD_TYPE);
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
