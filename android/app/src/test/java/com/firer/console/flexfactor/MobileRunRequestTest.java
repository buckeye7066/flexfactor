package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.Map;

public final class MobileRunRequestTest {
    private static final String ID = "123e4567-e89b-12d3-a456-426614174000";

    @Test
    public void allFourOriginalModesHaveStableWorkflowValues() {
        assertEquals("refactor", request(MobileRunRequest.Mode.REFACTOR,
                "src/app.py", "Make startup deterministic", false).workflowInputs().get("mode"));
        assertEquals("scout", request(MobileRunRequest.Mode.SCOUT,
                "", "", false).workflowInputs().get("mode"));
        assertEquals("audit", request(MobileRunRequest.Mode.AUDIT,
                "", "", false).workflowInputs().get("mode"));
        assertEquals("prodready", request(MobileRunRequest.Mode.PRODREADY,
                "", "", false).workflowInputs().get("mode"));
    }

    @Test
    public void requestCarriesExactTargetAndBudget() {
        Map<String, String> values = request(MobileRunRequest.Mode.AUDIT,
                "", "", false).workflowInputs();
        assertEquals(ID, values.get("request_id"));
        assertEquals("buckeye7066/flexfactor", values.get("target_repository"));
        assertEquals("main", values.get("target_ref"));
        assertEquals("25", values.get("max_cost"));
        assertEquals("false", values.get("scout_apply"));
    }

    @Test
    public void scoutApplyCannotLeakIntoAnotherMode() {
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT, "buckeye7066/flexfactor", "main",
                "", "", true, 25));
        assertTrue(request(MobileRunRequest.Mode.SCOUT, "", "", true)
                .scoutApply);
        assertFalse(request(MobileRunRequest.Mode.SCOUT, "", "", false)
                .scoutApply);
    }

    @Test
    public void refactorRequiresContainedFileAndGoal() {
        assertThrows(IllegalArgumentException.class, () -> request(
                MobileRunRequest.Mode.REFACTOR, "../secret", "change it", false));
        assertThrows(IllegalArgumentException.class, () -> request(
                MobileRunRequest.Mode.REFACTOR, "/etc/passwd", "change it", false));
        assertThrows(IllegalArgumentException.class, () -> request(
                MobileRunRequest.Mode.REFACTOR, "src/app.py", "", false));
    }

    @Test
    public void invalidRepositoryRefAndBudgetFailClosed() {
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT, "not-a-repo", "main", "", "", false, 25));
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT, "owner/repo", "../main", "", "", false, 25));
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT, "owner/repo", "main", "", "", false, 151));
    }

    private MobileRunRequest request(MobileRunRequest.Mode mode, String file,
            String goal, boolean scoutApply) {
        return new MobileRunRequest(ID, mode, "buckeye7066/flexfactor", "main",
                file, goal, scoutApply, 25);
    }
}
