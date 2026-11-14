package com.endpoint.frelog.domain.canvas.runtime;

import com.endpoint.frelog.domain.canvas.dto.DocumentScope;
import com.endpoint.frelog.domain.canvas.repository.WorkspaceInfoRepository;
import com.endpoint.frelog.domain.canvas.service.CanvasRuntimeService;
import javax.sql.DataSource;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;
import org.springframework.test.context.junit.jupiter.SpringJUnitConfig;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.TransactionTemplate;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@SpringJUnitConfig(RuntimeCleanupTransactionTest.Config.class)
class RuntimeCleanupTransactionTest {
    @Configuration @EnableTransactionManagement
    static class Config {
        @Bean DataSource dataSource() {
            var ds=new DriverManagerDataSource("jdbc:h2:mem:cleanup_tx;MODE=MSSQLServer;DB_CLOSE_DELAY=-1","sa","");
            new ResourceDatabasePopulator(new ClassPathResource("runtime-cleanup-test.sql")).execute(ds);return ds;
        }
        @Bean JdbcTemplate jdbc(DataSource ds) { return new JdbcTemplate(ds); }
        @Bean PlatformTransactionManager tx(DataSource ds) { return new DataSourceTransactionManager(ds); }
        @Bean CanvasRuntimeService runtime() { return mock(CanvasRuntimeService.class); }
        @Bean WorkspaceInfoRepository workspaces() { return mock(WorkspaceInfoRepository.class); }
        @Bean RuntimeCleanupQueue queue(JdbcTemplate jdbc) { return new RuntimeCleanupQueue(jdbc); }
        @Bean RuntimeCleanupWorker worker(RuntimeCleanupQueue q,CanvasRuntimeService r,WorkspaceInfoRepository w) { return new RuntimeCleanupWorker(q,r,w); }
        @Bean RuntimeCleanupCoordinator coordinator(RuntimeCleanupQueue q,RuntimeCleanupWorker w) { return new RuntimeCleanupCoordinator(q,w); }
    }
    @Autowired RuntimeCleanupQueue queue;
    @Autowired CanvasRuntimeService runtime;
    @Autowired ApplicationEventPublisher events;
    @Autowired PlatformTransactionManager tx;
    @Autowired JdbcTemplate jdbc;
    @BeforeEach void clear() { reset(runtime);jdbc.update("DELETE FROM dbo.workspace_runtime_cleanup"); }
    @Test void rollbackRemovesTaskAndNeverDeletesVm() {
        new TransactionTemplate(tx).executeWithoutResult(status->{queue.enqueue(DocumentScope.WORKSPACE, 1);events.publishEvent(new RuntimeCleanupCoordinator.DeletedSpace(DocumentScope.WORKSPACE, 1));status.setRollbackOnly();});
        assertFalse(queue.contains(DocumentScope.WORKSPACE, 1));verifyNoInteractions(runtime);
    }
    @Test void commitDeletesVmAndCommitsTaskCompletionInNewTransaction() {
        new TransactionTemplate(tx).executeWithoutResult(status->{queue.enqueue(DocumentScope.WORKSPACE, 2);events.publishEvent(new RuntimeCleanupCoordinator.DeletedSpace(DocumentScope.WORKSPACE, 2));verifyNoInteractions(runtime);});
        verify(runtime).delete(1_000_000_002);assertFalse(queue.contains(DocumentScope.WORKSPACE, 2));
    }
    @Test void unavailableVmKeepsCommittedRetryTask() {
        doThrow(new IllegalStateException("busy")).when(runtime).delete(1_000_000_003);
        new TransactionTemplate(tx).executeWithoutResult(status->{queue.enqueue(DocumentScope.WORKSPACE, 3);events.publishEvent(new RuntimeCleanupCoordinator.DeletedSpace(DocumentScope.WORKSPACE, 3));});
        assertTrue(queue.contains(DocumentScope.WORKSPACE, 3));assertEquals(1,jdbc.queryForObject("SELECT attempt_count FROM dbo.workspace_runtime_cleanup WHERE workspace_id=3",Integer.class));
        assertFalse(queue.due(DocumentScope.WORKSPACE).contains(3));
    }
}
