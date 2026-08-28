package com.firer.console.flexfactor;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;

/** Immutable, validated input for the standalone GitHub Actions runner. */
public final class MobileRunRequest {
    public enum Provider {
        OLLAMA("ollama"),
        OPENAI("openai");

        final String wire;
        Provider(String wire) { this.wire = wire; }
    }

    public enum Mode {
        REFACTOR("refactor"),
        SCOUT("scout"),
        AUDIT("audit"),
        PRODREADY("prodready");

        final String wire;
        Mode(String wire) { this.wire = wire; }
    }

    private static final Pattern REPOSITORY = Pattern.compile(
            "[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/[A-Za-z0-9_.-]{1,100}");
    private static final Pattern REF = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._/-]{0,199}");
    private static final Pattern FILE = Pattern.compile("(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[^\\r\\n]{1,500}");

    public final String requestId;
    public final Mode mode;
    public final Provider provider;
    public final String repository;
    public final String ref;
    public final String file;
    public final String goal;
    public final boolean scoutApply;
    public final double maxCost;

    public MobileRunRequest(Mode mode, Provider provider, String repository, String ref, String file,
            String goal, boolean scoutApply, double maxCost) {
        this(UUID.randomUUID().toString(), mode, provider, repository, ref, file, goal,
                scoutApply, maxCost);
    }

    MobileRunRequest(String requestId, Mode mode, Provider provider, String repository, String ref,
            String file, String goal, boolean scoutApply, double maxCost) {
        this.requestId = clean(requestId);
        this.mode = mode;
        this.provider = provider;
        this.repository = clean(repository);
        this.ref = clean(ref);
        this.file = clean(file);
        this.goal = clean(goal);
        this.scoutApply = scoutApply;
        this.maxCost = maxCost;
        validate();
    }

    private void validate() {
        if (!requestId.matches("[0-9a-fA-F-]{36}")) {
            throw new IllegalArgumentException("The run identifier is invalid.");
        }
        if (mode == null) throw new IllegalArgumentException("Choose a FlexFactor mode.");
        if (provider == null) throw new IllegalArgumentException("Choose a model provider.");
        if (!REPOSITORY.matcher(repository).matches() || repository.endsWith(".")) {
            throw new IllegalArgumentException("Repository must be written as owner/name.");
        }
        if (!REF.matcher(ref).matches() || ref.contains("..") || ref.endsWith("/")) {
            throw new IllegalArgumentException("The repository branch is invalid.");
        }
        if (!Double.isFinite(maxCost) || maxCost < 1 || maxCost > 150) {
            throw new IllegalArgumentException("The cost cap must be between $1 and $150.");
        }
        if (mode == Mode.REFACTOR) {
            if (!FILE.matcher(file).matches()) {
                throw new IllegalArgumentException("Option 1 needs a repository-relative file path.");
            }
            if (goal.length() < 3 || goal.length() > 2000) {
                throw new IllegalArgumentException("Option 1 needs a clear refactoring goal.");
            }
        }
        if (mode != Mode.REFACTOR && (!file.isEmpty() || !goal.isEmpty())) {
            throw new IllegalArgumentException("File and goal are only valid for Option 1.");
        }
        if (mode != Mode.SCOUT && scoutApply) {
            throw new IllegalArgumentException("Scout apply is only valid for Option 2.");
        }
    }

    public Map<String, String> workflowInputs() {
        Map<String, String> values = new LinkedHashMap<>();
        values.put("request_id", requestId);
        values.put("mode", mode.wire);
        values.put("provider", provider.wire);
        values.put("target_repository", repository);
        values.put("target_ref", ref);
        values.put("file", file);
        values.put("goal", goal);
        values.put("scout_apply", Boolean.toString(scoutApply));
        values.put("max_cost", formatCost(maxCost));
        return values;
    }

    private static String formatCost(double value) {
        if (value == Math.rint(value)) return Long.toString((long) value);
        return Double.toString(value);
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }
}
