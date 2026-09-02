package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.Map;

public final class MobileRunRequestTest {
    @Test
    public void rejectsNonCanonicalRunIdentifier() {
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                "000000000000-0000-0000-0000-00000000",
                MobileRunRequest.Mode.AUDIT, "owner/project", "main",
                "", "", false, 25));
    }

    private static final String ID = "123e4567-e89b-12d3-a456-426614174000";

    @Test
    public void allFourModesHaveStableWorkflowValues() {
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
    public void requestCarriesOneAutomaticPolicyAndSixPasses() {
        Map<String, String> values = request(MobileRunRequest.Mode.AUDIT,
                "", "", false).workflowInputs();
        assertEquals(ID, values.get("request_id"));
        assertEquals("buckeye7066/flexfactor", values.get("target_repository"));
        assertEquals("main", values.get("target_ref"));
        assertEquals("25", values.get("max_cost"));
        assertEquals("auto", values.get("provider"));
        assertEquals("6", values.get("max_iterations"));
        assertFalse(values.containsKey("economy"));
        assertFalse(values.containsKey("use_both"));
    }

    @Test
    public void onlyTheAutomaticProviderPolicyExists() {
        assertEquals(1, MobileRunRequest.Provider.values().length);
        assertEquals(MobileRunRequest.Provider.AUTO,
                MobileRunRequest.Provider.values()[0]);
    }

    @Test
    public void scoutApplyCannotLeakIntoAnotherMode() {
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT,
                "buckeye7066/flexfactor", "main", "", "", true, 25));
        assertTrue(request(MobileRunRequest.Mode.SCOUT, "", "", true).scoutApply);
        assertFalse(request(MobileRunRequest.Mode.SCOUT, "", "", false).scoutApply);
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
    public void passSevenAndInvalidTargetsFailClosed() {
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT,
                "not-a-repo", "main", "", "", false, 25));
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                ID, MobileRunRequest.Mode.AUDIT,
                "owner/repo", "../main", "", "", false, 25));
        assertThrows(IllegalArgumentException.class, () -> new MobileRunRequest(
                MobileRunRequest.Mode.REFACTOR,
                "owner/repo", "main", "src/app.py", "Improve it", false, 25,
                90, 7));
    }

    private MobileRunRequest request(MobileRunRequest.Mode mode, String file,
            String goal, boolean scoutApply) {
        return new MobileRunRequest(ID, mode,
                "buckeye7066/flexfactor", "main",
                file, goal, scoutApply, 25);
    }
}
