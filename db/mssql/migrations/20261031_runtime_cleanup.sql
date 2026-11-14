-- Administrator-only additive migration. Runtime users retain DML-only permissions.
USE [$(DB_NAME)];
GO
IF OBJECT_ID(N'dbo.workspace_runtime_cleanup',N'U') IS NULL
BEGIN
    CREATE TABLE dbo.workspace_runtime_cleanup (
        workspace_id INT NOT NULL PRIMARY KEY,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        attempt_count INT NOT NULL DEFAULT 0,
        next_attempt_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_workspace_runtime_cleanup_due ON dbo.workspace_runtime_cleanup(next_attempt_at,workspace_id);
END;
GO
