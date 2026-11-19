SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.proof_definitions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.proof_definitions (
        definition_id  BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_proof_definitions PRIMARY KEY,
        workspace_id   INT NULL,
        workspace_key  NVARCHAR(128) NOT NULL,
        owner_user_id  BIGINT NULL,
        prover_engine  VARCHAR(16) NOT NULL,
        symbol_name    NVARCHAR(190) NOT NULL,
        definition_kind VARCHAR(32) NOT NULL,
        signature_text NVARCHAR(1000) NOT NULL,
        source_code    NVARCHAR(MAX) NOT NULL,
        content_hash   VARCHAR(64) NOT NULL,
        source_cell_id NVARCHAR(128) NULL,
        created_at     DATETIME2 NOT NULL CONSTRAINT DF_proof_definitions_created_at DEFAULT SYSUTCDATETIME(),
        updated_at     DATETIME2 NOT NULL CONSTRAINT DF_proof_definitions_updated_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_proof_definitions_scope UNIQUE (workspace_key, prover_engine, symbol_name),
        CONSTRAINT CK_proof_definitions_engine CHECK (prover_engine IN ('LEAN4', 'ROCQ'))
    );

    CREATE INDEX IX_proof_definitions_workspace
        ON dbo.proof_definitions (workspace_key, prover_engine, updated_at DESC);
END;

IF OBJECT_ID(N'dbo.proof_theorems', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.proof_theorems (
        theorem_id          BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_proof_theorems PRIMARY KEY,
        workspace_id        INT NULL,
        workspace_key       NVARCHAR(128) NOT NULL,
        owner_user_id       BIGINT NULL,
        prover_engine       VARCHAR(16) NOT NULL,
        theorem_name        NVARCHAR(190) NOT NULL,
        statement_kind      VARCHAR(32) NOT NULL,
        statement_text      NVARCHAR(1000) NOT NULL,
        proof_code          NVARCHAR(MAX) NOT NULL,
        verification_status VARCHAR(16) NOT NULL CONSTRAINT DF_proof_theorems_status DEFAULT 'VERIFIED',
        verifier_output     NVARCHAR(4000) NOT NULL,
        source_hash         VARCHAR(64) NOT NULL,
        source_cell_id      NVARCHAR(128) NULL,
        verified_at         DATETIME2 NOT NULL CONSTRAINT DF_proof_theorems_verified_at DEFAULT SYSUTCDATETIME(),
        created_at          DATETIME2 NOT NULL CONSTRAINT DF_proof_theorems_created_at DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2 NOT NULL CONSTRAINT DF_proof_theorems_updated_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_proof_theorems_scope UNIQUE (workspace_key, prover_engine, theorem_name),
        CONSTRAINT CK_proof_theorems_engine CHECK (prover_engine IN ('LEAN4', 'ROCQ')),
        CONSTRAINT CK_proof_theorems_kind CHECK (statement_kind IN ('THEOREM', 'LEMMA', 'COROLLARY', 'PROPOSITION', 'EXAMPLE')),
        CONSTRAINT CK_proof_theorems_status CHECK (verification_status = 'VERIFIED')
    );

    CREATE INDEX IX_proof_theorems_workspace
        ON dbo.proof_theorems (workspace_key, prover_engine, verified_at DESC);
END;

IF OBJECT_ID(N'dbo.proof_theorem_lemma_relations', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.proof_theorem_lemma_relations (
        relation_id       BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_proof_theorem_lemma_relations PRIMARY KEY,
        theorem_id        BIGINT NOT NULL CONSTRAINT FK_proof_thm_lemma_theorem FOREIGN KEY REFERENCES dbo.proof_theorems (theorem_id) ON DELETE CASCADE,
        lemma_theorem_id  BIGINT NULL CONSTRAINT FK_proof_thm_lemma_target FOREIGN KEY REFERENCES dbo.proof_theorems (theorem_id) ON DELETE NO ACTION,
        lemma_name        NVARCHAR(190) NOT NULL,
        relation_type     VARCHAR(32) NOT NULL,
        reference_context NVARCHAR(500) NULL,
        created_at        DATETIME2 NOT NULL CONSTRAINT DF_proof_thm_lemma_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_proof_thm_lemma_edge UNIQUE (theorem_id, lemma_name, relation_type),
        CONSTRAINT CK_proof_thm_lemma_type CHECK (relation_type IN ('USES_LEMMA', 'DEPENDS_ON', 'REWRITES_WITH', 'APPLIES')),
        CONSTRAINT CK_proof_theorem_lemma_no_self CHECK (lemma_theorem_id IS NULL OR theorem_id <> lemma_theorem_id)
    );

    CREATE INDEX IX_proof_thm_lemma_target
        ON dbo.proof_theorem_lemma_relations (lemma_theorem_id, lemma_name);
END;

IF OBJECT_ID(N'dbo.proof_theorem_definition_relations', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.proof_theorem_definition_relations (
        relation_id       BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_proof_theorem_definition_relations PRIMARY KEY,
        theorem_id        BIGINT NOT NULL CONSTRAINT FK_proof_thm_def_theorem FOREIGN KEY REFERENCES dbo.proof_theorems (theorem_id) ON DELETE CASCADE,
        definition_id     BIGINT NOT NULL CONSTRAINT FK_proof_thm_def_definition FOREIGN KEY REFERENCES dbo.proof_definitions (definition_id) ON DELETE CASCADE,
        usage_role        VARCHAR(32) NOT NULL,
        reference_context NVARCHAR(500) NULL,
        created_at        DATETIME2 NOT NULL CONSTRAINT DF_proof_thm_def_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_proof_thm_def_edge UNIQUE (theorem_id, definition_id),
        CONSTRAINT CK_proof_thm_def_role CHECK (usage_role IN ('IN_STATEMENT', 'IN_PROOF', 'BOTH', 'REFERENCED'))
    );

    CREATE INDEX IX_proof_thm_def_definition
        ON dbo.proof_theorem_definition_relations (definition_id, theorem_id);
END;

IF DATABASE_PRINCIPAL_ID(N'irade_runtime') IS NOT NULL
BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.proof_definitions TO [irade_runtime];
    GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.proof_theorems TO [irade_runtime];
    GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.proof_theorem_lemma_relations TO [irade_runtime];
    GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.proof_theorem_definition_relations TO [irade_runtime];
END;

COMMIT TRANSACTION;
GO
