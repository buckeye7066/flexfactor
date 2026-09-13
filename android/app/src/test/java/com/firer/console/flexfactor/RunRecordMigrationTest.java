package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;

/** Exercises the record shared by stored history and the legacy last-run migration. */
public final class RunRecordMigrationTest {
    private static final String ID = "123e4567-e89b-12d3-a456-426614174000";
    private static final String URL = "https://github.com/owner/repo/actions/runs/42";

    @Test
    public void missingRequestIdStopsRetriesAndRetainsUsefulHistory() throws Exception {
        assertRecoverable(record("", false));
    }

    @Test
    public void noncanonicalRequestIdStopsRetriesWithoutInventingAnIdentity() throws Exception {
        for (String id : new String[]{"legacy", "1-1-1-1-1", " " + ID, ID + " ", null}) {
            Object record = record(id, false);
            assertRecoverable(record);
            assertEquals(id, field(record, "requestId"));
        }
    }

    @Test
    public void validInflightHistoryRetainsItsIdentityAndStatus() throws Exception {
        for (String id : new String[]{ID, ID.toUpperCase(java.util.Locale.ROOT)}) {
            Object record = record(id, false);
            assertFalse((boolean) field(record, "complete"));
            assertEquals("In progress", field(record, "status"));
            assertEquals(id, field(record, "requestId"));
        }
    }

    @Test
    public void completedLegacyHistoryKeepsItsRecordedOutcome() throws Exception {
        Object record = record("", true);
        assertTrue((boolean) field(record, "complete"));
        assertEquals("In progress", field(record, "status"));
        assertEquals(URL, field(record, "url"));
    }

    private static Object record(String requestId, boolean complete) throws Exception {
        Class<?> type = Class.forName("com.firer.console.flexfactor.MainActivity$RunRecord");
        Constructor<?> constructor = type.getDeclaredConstructor(long.class, String.class,
                String.class, String.class, String.class, String.class, boolean.class);
        constructor.setAccessible(true);
        return constructor.newInstance(42L, "owner/repo", requestId, "audit", URL,
                "In progress", complete);
    }

    private static Object field(Object record, String name) throws Exception {
        Field field = record.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field.get(record);
    }

    private static void assertRecoverable(Object record) throws Exception {
        assertTrue("Invalid legacy history must stop blocking queue admission",
                (boolean) field(record, "complete"));
        String status = (String) field(record, "status");
        assertTrue(status, status.startsWith("BLOCKED"));
        assertTrue(status, status.contains("GitHub"));
        assertTrue(status, status.contains("In progress"));
        assertEquals(42L, field(record, "id"));
        assertEquals("owner/repo", field(record, "repository"));
        assertEquals("audit", field(record, "mode"));
        assertEquals(URL, field(record, "url"));
    }
}
