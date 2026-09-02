package com.firer.console.flexfactor;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/** Immutable, validated input for the managed FlexFactor Cloud runner. */
public final class MobileRunRequest {
    public enum Provider {
        AUTO("auto");

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
    private static final Set<String> CODE_HOSTS = Set.of(
            "github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gitea.com");
    private static final Set<String> NON_REPOSITORY_ROOTS = Set.of(
            "about", "collections", "customer-stories", "enterprise", "events",
            "explore", "features", "help", "marketplace", "pricing", "readme",
            "resources", "security", "site", "solutions", "sponsors", "topics");

    public final String requestId;
    public final Mode mode;
    public final Provider provider;
    public final String repository;
    public final String ref;
    public final String file;
    public final String goal;
    public final String scoutSource;
    public final boolean scoutApply;
    public final double maxCost;
    public final int threshold;
    public final int maxIterations;

    public MobileRunRequest(Mode mode, String repository, String ref, String file,
            String goal, boolean scoutApply, double maxCost) {
        this(UUID.randomUUID().toString(), mode, repository, ref, file, goal, "",
                scoutApply, maxCost, 90, 6);
    }

    public MobileRunRequest(Mode mode, String repository, String ref, String file,
            String goal, String scoutSource, boolean scoutApply, double maxCost) {
        this(UUID.randomUUID().toString(), mode, repository, ref, file, goal,
                scoutSource, scoutApply, maxCost, 90, 6);
    }

    public MobileRunRequest(Mode mode, String repository, String ref, String file,
            String goal, boolean scoutApply, double maxCost, int threshold,
            int maxIterations) {
        this(UUID.randomUUID().toString(), mode, repository, ref, file, goal, "",
                scoutApply, maxCost, threshold, maxIterations);
    }

    public MobileRunRequest(Mode mode, String repository, String ref, String file,
            String goal, String scoutSource, boolean scoutApply, double maxCost,
            int threshold, int maxIterations) {
        this(UUID.randomUUID().toString(), mode, repository, ref, file, goal,
                scoutSource, scoutApply, maxCost, threshold, maxIterations);
    }

    MobileRunRequest(String requestId, Mode mode, String repository, String ref,
            String file, String goal, boolean scoutApply, double maxCost) {
        this(requestId, mode, repository, ref, file, goal, "", scoutApply, maxCost,
                90, 6);
    }

    MobileRunRequest(String requestId, Mode mode, String repository, String ref,
            String file, String goal, String scoutSource, boolean scoutApply,
            double maxCost) {
        this(requestId, mode, repository, ref, file, goal, scoutSource, scoutApply,
                maxCost, 90, 6);
    }

    MobileRunRequest(String requestId, Mode mode, String repository, String ref,
            String file, String goal, boolean scoutApply, double maxCost, int threshold,
            int maxIterations) {
        this(requestId, mode, repository, ref, file, goal, "", scoutApply,
                maxCost, threshold, maxIterations);
    }

    MobileRunRequest(String requestId, Mode mode, String repository, String ref,
            String file, String goal, String scoutSource, boolean scoutApply,
            double maxCost, int threshold, int maxIterations) {
        this.requestId = clean(requestId);
        this.mode = mode;
        this.provider = Provider.AUTO;
        this.repository = clean(repository);
        this.ref = clean(ref);
        this.file = clean(file);
        this.goal = clean(goal);
        this.scoutSource = clean(scoutSource);
        this.scoutApply = scoutApply;
        this.maxCost = maxCost;
        this.threshold = threshold;
        this.maxIterations = maxIterations;
        validate();
    }

    private void validate() {
        if (!isCanonicalUuid(requestId)) {
            throw new IllegalArgumentException("The run identifier is invalid.");
        }
        if (mode == null) throw new IllegalArgumentException("Choose a FlexFactor mode.");
        if (!REPOSITORY.matcher(repository).matches() || repository.endsWith(".")) {
            throw new IllegalArgumentException("Repository must be written as owner/name.");
        }
        if (!REF.matcher(ref).matches() || ref.contains("..") || ref.endsWith("/")) {
            throw new IllegalArgumentException("The repository branch is invalid.");
        }
        if (!Double.isFinite(maxCost) || maxCost < 1 || maxCost > 150) {
            throw new IllegalArgumentException("The cost cap must be between $1 and $150.");
        }
        if (threshold < 0 || threshold > 100) {
            throw new IllegalArgumentException("The acceptance threshold must be between 0 and 100.");
        }
        if (maxIterations < 1 || maxIterations > 6) {
            throw new IllegalArgumentException("FlexFactor passes must be between 1 and 6.");
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
        if (mode == Mode.SCOUT) {
            if (scoutSource.length() > 2000 || !isPublicUrlShape(scoutSource)) {
                throw new IllegalArgumentException(
                        "Option 2 needs a public program or product website URL. Repo Rewards handles repositories.");
            }
        } else if (!scoutSource.isEmpty()) {
            throw new IllegalArgumentException("A scouted program is only valid for Option 2.");
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
        values.put("scout_source", scoutSource);
        values.put("scout_apply", Boolean.toString(scoutApply));
        values.put("max_cost", formatCost(maxCost));
        values.put("threshold", Integer.toString(threshold));
        values.put("max_iterations", Integer.toString(maxIterations));
        return values;
    }

    private static String formatCost(double value) {
        if (value == Math.rint(value)) return Long.toString((long) value);
        return Double.toString(value);
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    private static boolean isPublicUrlShape(String value) {
        if (value.isEmpty()) return false;
        try {
            URI uri = new URI(value);
            String scheme = uri.getScheme();
            return ("https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme))
                    && uri.getHost() != null && !uri.getHost().isEmpty()
                    && uri.getUserInfo() == null && !isCodeRepositoryUrl(uri);
        } catch (URISyntaxException invalid) {
            return false;
        }
    }

    private static boolean isCodeRepositoryUrl(URI uri) {
        String host = uri.getHost().toLowerCase();
        if (host.startsWith("www.")) host = host.substring(4);
        if (!CODE_HOSTS.contains(host)) return false;
        String[] raw = (uri.getPath() == null ? "" : uri.getPath()).split("/");
        int count = 0;
        String first = "";
        for (String part : raw) {
            if (part.isEmpty()) continue;
            if (count++ == 0) first = part.toLowerCase();
        }
        return count >= 2 && !NON_REPOSITORY_ROOTS.contains(first);
    }

    private static boolean isCanonicalUuid(String value) {
        try {
            return UUID.fromString(value).toString().equalsIgnoreCase(value);
        } catch (IllegalArgumentException invalid) {
            return false;
        }
    }
}
