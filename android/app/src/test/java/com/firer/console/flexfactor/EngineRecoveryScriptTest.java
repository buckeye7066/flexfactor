package com.firer.console.flexfactor;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class EngineRecoveryScriptTest {
    @Test
    public void recoveryUsesFixedPrivateCheckoutAndFastForwardUpdate() {
        String script = EngineRecoveryScript.repairScript();
        assertTrue(script.contains("$HOME/phone-console/flexfactor"));
        assertTrue(script.contains("pull --ff-only origin main"));
        assertTrue(script.contains("FLEXFACTOR_NONINTERACTIVE=1"));
        assertFalse(script.contains("reset --hard"));
        assertFalse(script.contains("OPENAI_API_KEY"));
        assertFalse(script.contains("ANTHROPIC_API_KEY"));
    }

    @Test
    public void startDoesNotPerformNetworkOrPackageOperations() {
        String script = EngineRecoveryScript.startScript();
        assertTrue(script.contains("scripts/phone/engine.sh\" start"));
        assertFalse(script.contains("git "));
        assertFalse(script.contains("pkg "));
    }

    @Test
    public void oneTimeCommandOnlyEnablesTermuxExternalApps() {
        String command = EngineRecoveryScript.ENABLE_EXTERNAL_APPS_COMMAND;
        assertTrue(command.contains("allow-external-apps=true"));
        assertTrue(command.contains("termux-reload-settings"));
        assertFalse(command.contains("flexfactor"));
        assertFalse(command.contains("github"));
    }
}
