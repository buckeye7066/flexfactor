package com.firer.console.flexfactor;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

public final class MobileRunQueueTest {
    private MobileRunRequest request(int index) {
        return new MobileRunRequest(
                MobileRunRequest.Mode.AUDIT,
                "owner/repo-" + index, "main", "", "", false, 25);
    }

    @Test
    public void acceptsThirtyAndRejectsThirtyOne() {
        List<MobileRunRequest> requests = new ArrayList<>();
        for (int i = 0; i < 30; i++) requests.add(request(i));
        assertEquals(30, new MobileRunQueue(requests).size());
        requests.add(request(30));
        assertThrows(IllegalArgumentException.class, () -> new MobileRunQueue(requests));
    }

    @Test
    public void permitsExactlyOneActiveRunAndPreservesOrder() {
        MobileRunQueue queue = new MobileRunQueue(List.of(request(1), request(2)));
        assertEquals("owner/repo-1", queue.nextRequest().repository);
        queue.markDispatched(101L);
        assertNull(queue.nextRequest());
        assertThrows(IllegalStateException.class, () -> queue.markDispatched(102L));
        queue.markActiveComplete(101L);
        assertEquals("owner/repo-2", queue.nextRequest().repository);
        queue.markDispatched(102L);
        queue.markActiveComplete(102L);
        assertTrue(queue.isComplete());
    }

    @Test
    public void persistenceRetainsTheActiveAuthority() {
        MobileRunQueue queue = new MobileRunQueue(List.of(request(1), request(2)));
        queue.markDispatched(77L);
        MobileRunQueue restored = MobileRunQueue.fromJson(queue.toJson());
        assertTrue(restored.hasActiveRun());
        assertEquals(77L, restored.activeRunId());
        assertEquals(0, restored.completedCount());
        assertEquals("owner/repo-1", restored.activeRequest().repository);
        restored.markActiveComplete(77L);
        assertFalse(restored.isComplete());
        assertEquals("owner/repo-2", restored.nextRequest().repository);
    }

    @Test
    public void rejectsDuplicateRequestIdsAndImpossibleActivePointers() {
        String requestId = "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
        MobileRunRequest first = new MobileRunRequest(requestId,
                MobileRunRequest.Mode.AUDIT, "owner/one", "main", "", "", false, 25);
        MobileRunRequest second = new MobileRunRequest(requestId,
                MobileRunRequest.Mode.AUDIT, "owner/two", "main", "", "", false, 25);
        assertThrows(IllegalArgumentException.class,
                () -> new MobileRunQueue(List.of(first, second)));

        MobileRunQueue queue = new MobileRunQueue(List.of(request(1)));
        String impossible = queue.toJson()
                .replace("\"next_index\":0", "\"next_index\":1")
                .replace("\"active_run_id\":0", "\"active_run_id\":99");
        assertThrows(IllegalArgumentException.class,
                () -> MobileRunQueue.fromJson(impossible));
    }

    @Test
    public void rejectsMalformedQueueIdentifiers() {
        MobileRunQueue queue = new MobileRunQueue(List.of(request(1)));
        String malformed = queue.toJson().replace(queue.queueId,
                "000000000000-0000-0000-0000-00000000");
        assertThrows(IllegalArgumentException.class,
                () -> MobileRunQueue.fromJson(malformed));
    }
}
