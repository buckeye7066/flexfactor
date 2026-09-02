package com.firer.console.flexfactor;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/** Durable, strictly sequential queue owned by the mobile orchestrator. */
public final class MobileRunQueue {
    public static final int MAX_TARGETS = 30;
    private static final int SCHEMA = 1;

    public final String queueId;
    private final List<MobileRunRequest> requests;
    private int nextIndex;
    private long activeRunId;

    public MobileRunQueue(List<MobileRunRequest> requests) {
        this(UUID.randomUUID().toString(), requests, 0, 0L);
    }

    private MobileRunQueue(String queueId, List<MobileRunRequest> requests,
            int nextIndex, long activeRunId) {
        if (requests == null || requests.isEmpty() || requests.size() > MAX_TARGETS) {
            throw new IllegalArgumentException("Choose from 1 through 30 targets.");
        }
        if (!canonicalUuid(queueId)) {
            throw new IllegalArgumentException("The saved FlexFactor queue identifier is invalid.");
        }
        Set<String> requestIds = new HashSet<>();
        for (MobileRunRequest request : requests) {
            if (request == null || !requestIds.add(request.requestId)) {
                throw new IllegalArgumentException(
                        "Every queued target must have a distinct run identifier.");
            }
        }
        this.queueId = queueId;
        this.requests = Collections.unmodifiableList(new ArrayList<>(requests));
        this.nextIndex = nextIndex;
        this.activeRunId = activeRunId;
        if (nextIndex < 0 || nextIndex > requests.size() || activeRunId < 0
                || (activeRunId > 0L && nextIndex >= requests.size())) {
            throw new IllegalArgumentException("The saved FlexFactor queue is invalid.");
        }
    }

    public int size() { return requests.size(); }
    public int completedCount() { return nextIndex; }
    public long activeRunId() { return activeRunId; }
    public boolean hasActiveRun() { return activeRunId > 0L; }
    public boolean isComplete() { return nextIndex >= requests.size() && !hasActiveRun(); }

    public MobileRunRequest nextRequest() {
        return isComplete() || hasActiveRun() ? null : requests.get(nextIndex);
    }

    public MobileRunRequest activeRequest() {
        return hasActiveRun() && nextIndex < requests.size() ? requests.get(nextIndex) : null;
    }

    public void markDispatched(long runId) {
        if (runId <= 0L || hasActiveRun() || nextIndex >= requests.size()) {
            throw new IllegalStateException("Only the next queued target may be dispatched.");
        }
        activeRunId = runId;
    }

    public void markActiveComplete(long runId) {
        if (runId <= 0L || activeRunId != runId) {
            throw new IllegalStateException("Only the active queued run may complete.");
        }
        activeRunId = 0L;
        nextIndex++;
    }

    public String toJson() {
        try {
            JSONObject root = new JSONObject();
            root.put("schema", SCHEMA);
            root.put("queue_id", queueId);
            root.put("next_index", nextIndex);
            root.put("active_run_id", activeRunId);
            JSONArray rows = new JSONArray();
            for (MobileRunRequest request : requests) {
                JSONObject row = new JSONObject();
                row.put("request_id", request.requestId);
                row.put("mode", request.mode.name());
                row.put("repository", request.repository);
                row.put("ref", request.ref);
                row.put("file", request.file);
                row.put("goal", request.goal);
                row.put("scout_apply", request.scoutApply);
                row.put("max_cost", request.maxCost);
                row.put("threshold", request.threshold);
                row.put("max_iterations", request.maxIterations);
                rows.put(row);
            }
            root.put("requests", rows);
            return root.toString();
        } catch (Exception impossible) {
            throw new IllegalStateException("The FlexFactor queue could not be saved.", impossible);
        }
    }

    public static MobileRunQueue fromJson(String raw) {
        try {
            JSONObject root = new JSONObject(raw);
            if (root.getInt("schema") != SCHEMA) {
                throw new IllegalArgumentException("Unsupported queue schema.");
            }
            JSONArray rows = root.getJSONArray("requests");
            List<MobileRunRequest> requests = new ArrayList<>();
            for (int i = 0; i < rows.length(); i++) {
                JSONObject row = rows.getJSONObject(i);
                requests.add(new MobileRunRequest(
                        row.getString("request_id"),
                        MobileRunRequest.Mode.valueOf(row.getString("mode")),
                        row.getString("repository"), row.getString("ref"),
                        row.optString("file", ""), row.optString("goal", ""),
                        row.optBoolean("scout_apply", false), row.getDouble("max_cost"),
                        row.optInt("threshold", 90), row.optInt("max_iterations", 6)));
            }
            return new MobileRunQueue(
                    root.getString("queue_id"), requests,
                    root.getInt("next_index"), root.optLong("active_run_id", 0L));
        } catch (IllegalArgumentException error) {
            throw error;
        } catch (Exception error) {
            throw new IllegalArgumentException("The saved FlexFactor queue is invalid.", error);
        }
    }

    private static boolean canonicalUuid(String value) {
        try {
            return UUID.fromString(value).toString().equalsIgnoreCase(value);
        } catch (IllegalArgumentException | NullPointerException invalid) {
            return false;
        }
    }
}
