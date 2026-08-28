package com.firer.console.flexfactor;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class MobileWorkflowTest {
    @Test
    public void callerIsPinnedAndSupportsPrivateTargetExecution() {
        String workflow = MobileWorkflow.content();
        assertTrue(workflow.contains("@android-v3.2.0"));
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
