package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class MobileWorkflowTest {
    /**
     * The caller must name the engine tag THIS build is released as. Asserting a
     * hand-typed literal here is what let the shipped 3.2.1 app install a caller
     * pinned to the 3.2.0 engine: the test held a second copy of the same typed
     * string, so it agreed with the drift instead of catching it.
     */
    @Test
    public void callerPinsTheEngineTagThisBuildIsReleasedAs() {
        assertEquals("android-v" + BuildConfig.VERSION_NAME, MobileWorkflow.ENGINE_REF);
        assertTrue(MobileWorkflow.content().contains(
                "mobile-run.yml@android-v" + BuildConfig.VERSION_NAME));
    }

    @Test
    public void callerIsPinnedAndSupportsPrivateTargetExecution() {
        String workflow = MobileWorkflow.content();
        assertTrue(workflow.contains("@" + MobileWorkflow.ENGINE_REF));
        assertTrue(workflow.contains("target_repository: ${{ github.repository }}"));
        assertTrue(workflow.contains("contents: write"));
        assertTrue(workflow.contains("pull-requests: write"));
        assertTrue(workflow.contains("copilot-requests: write"));
        assertFalse(workflow.contains("FLEXFACTOR_MOBILE_GITHUB_TOKEN"));
        assertFalse(workflow.contains("buckeye7066/flexfactor/actions/workflows"));
    }

    @Test
    public void callerExposesEveryDesktopProviderAndMode() {
        String workflow = MobileWorkflow.content();
        for (String mode : new String[]{"refactor", "scout", "audit", "prodready"}) {
            assertTrue(workflow.contains(mode));
        }
        for (String provider : new String[]{"ollama", "openai", "anthropic", "copilot"}) {
            assertTrue(workflow.contains(provider));
        }
    }
}
