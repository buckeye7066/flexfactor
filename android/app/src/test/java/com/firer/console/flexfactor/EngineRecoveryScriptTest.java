package com.firer.console.flexfactor;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class EngineRecoveryScriptTest {
    private static final String NONCE = "01234567-89ab-cdef-0123-456789abcdef";

    @Test
    public void recoveryUsesFixedPrivateCheckoutAndFastForwardUpdate() {
        String script = EngineRecoveryScript.repairScript(NONCE);
        assertTrue(script.contains("$HOME/phone-console/flexfactor"));
        assertTrue(script.contains("pull --ff-only origin main"));
        assertTrue(script.contains("FLEXFACTOR_NONINTERACTIVE=1"));
        assertFalse(script.contains("reset --hard"));
        assertFalse(script.contains("OPENAI_API_KEY"));
        assertFalse(script.contains("ANTHROPIC_API_KEY"));
        assertTrue(script.contains("trap finish EXIT"));
        assertTrue(script.contains("--es recovery_nonce \"$nonce\""));
        assertTrue(script.contains("command -v node"));
        assertTrue(script.contains("command -v curl"));
    }

    @Test
    public void startDoesNotPerformNetworkOrPackageOperations() {
        String script = EngineRecoveryScript.startScript(NONCE);
        assertTrue(script.contains("scripts/phone/engine.sh\" start"));
        assertFalse(script.contains("git "));
        assertFalse(script.contains("pkg "));
    }

    @Test(expected = IllegalArgumentException.class)
    public void rejectsNonUuidRecoveryNonce() {
        EngineRecoveryScript.repairScript("attacker-controlled shell text");
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
