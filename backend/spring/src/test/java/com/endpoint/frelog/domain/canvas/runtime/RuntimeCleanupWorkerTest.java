package com.endpoint.frelog.domain.canvas.runtime;

import com.endpoint.frelog.domain.canvas.dto.DocumentScope;
import com.endpoint.frelog.domain.canvas.repository.WorkspaceInfoRepository;
import com.endpoint.frelog.domain.canvas.service.CanvasRuntimeService;
import org.junit.jupiter.api.Test;
import static org.mockito.Mockito.*;

class RuntimeCleanupWorkerTest {
    private final RuntimeCleanupQueue queue=mock(RuntimeCleanupQueue.class);
    private final CanvasRuntimeService runtime=mock(CanvasRuntimeService.class);
    private final WorkspaceInfoRepository workspaces=mock(WorkspaceInfoRepository.class);
    private final RuntimeCleanupWorker worker=new RuntimeCleanupWorker(queue,runtime,workspaces);
    @Test void successDeletesDurableTaskAfterRuntimeAck() {
        when(queue.contains(DocumentScope.WORKSPACE, 12)).thenReturn(true);
        worker.cleanup(DocumentScope.WORKSPACE, 12);
        var order=inOrder(runtime,queue);
        order.verify(runtime).delete(1_000_000_012);
        order.verify(queue).complete(DocumentScope.WORKSPACE, 12);
        verify(queue,never()).retry(any(), anyInt());
    }
    @Test void failureKeepsTaskForRetry() {
        when(queue.contains(DocumentScope.WORKSPACE, 12)).thenReturn(true);
        doThrow(new IllegalStateException()).when(runtime).delete(1_000_000_012);
        worker.cleanup(DocumentScope.WORKSPACE, 12);
        verify(queue).retry(DocumentScope.WORKSPACE, 12);
        verify(queue,never()).complete(any(), anyInt());
    }
    @Test void existingWorkspaceOrCompletedTaskNeverDeletesRuntime() {
        when(queue.contains(DocumentScope.WORKSPACE, 12)).thenReturn(true);
        when(workspaces.existsById(12)).thenReturn(true);
        worker.cleanup(DocumentScope.WORKSPACE, 12);
        worker.cleanup(DocumentScope.WORKSPACE, 13);
        verifyNoInteractions(runtime);
        verify(queue,never()).complete(any(), anyInt());
    }
}
