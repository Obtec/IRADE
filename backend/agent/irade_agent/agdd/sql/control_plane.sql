-- AGDD Control Plane (PostgreSQL 15+, DOC.md §4.1). MVP tables + PHASE_2 tables kept for forward compatibility.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    user_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email       text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    user_id      uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    role         text NOT NULL CHECK (role IN ('OWNER','EDITOR','APPROVER','VIEWER')),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS spec_snapshots (
    snapshot_id     bigserial PRIMARY KEY,
    workspace_id    uuid NOT NULL REFERENCES workspaces,
    version         integer NOT NULL,
    srs_json        jsonb NOT NULL,
    arc_json        jsonb NOT NULL,
    sch_json        jsonb NOT NULL,
    trace_json      jsonb NOT NULL,
    conflict_passed boolean NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, version)
);

CREATE TABLE IF NOT EXISTS graph_definitions (
    graph_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces,
    version         integer NOT NULL,
    definition_json jsonb NOT NULL,
    is_published    boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, version)
);

CREATE TABLE IF NOT EXISTS workspace_budgets (
    workspace_id    uuid NOT NULL REFERENCES workspaces,
    period_ym       char(7) NOT NULL,            -- 'YYYY-MM'
    limit_usd       numeric(14,6) NOT NULL CHECK (limit_usd >= 0),
    spent_usd       numeric(14,6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    fallback_policy text NOT NULL DEFAULT 'CHEAP_AT_80_HALT_AT_100',
    PRIMARY KEY (workspace_id, period_ym)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_id     uuid NOT NULL,
    graph_version integer NOT NULL,
    snapshot_id  bigint NOT NULL REFERENCES spec_snapshots,
    route_mode   text NOT NULL CHECK (route_mode IN ('FAST_TRACK','FULL_AGDD')),
    current_node text,
    status       text NOT NULL CHECK (status IN ('PENDING','RUNNING','WAITING_HITL','SUCCEEDED','FAILED','ESCALATED','HALTED')),
    version      integer NOT NULL DEFAULT 1,
    handoff_json jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (graph_id, graph_version) REFERENCES graph_definitions (graph_id, version)
) WITH (fillfactor = 80, autovacuum_vacuum_scale_factor = 0.05);
CREATE INDEX IF NOT EXISTS idx_runs_active ON workflow_runs (status) WHERE status IN ('PENDING','RUNNING','WAITING_HITL');

CREATE TABLE IF NOT EXISTS job_queue (
    job_id           bigserial PRIMARY KEY,
    run_id           uuid NOT NULL REFERENCES workflow_runs ON DELETE CASCADE,
    node_name        text NOT NULL,
    status           text NOT NULL CHECK (status IN ('PENDING','RUNNING','DONE','FAILED')),
    priority         smallint NOT NULL DEFAULT 5,
    attempt_no       integer NOT NULL DEFAULT 0,
    available_at     timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);
CREATE INDEX IF NOT EXISTS idx_job_pending ON job_queue (priority, available_at) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_job_lease ON job_queue (lease_expires_at) WHERE status = 'RUNNING';

CREATE TABLE IF NOT EXISTS node_executions (
    exec_id         bigserial,
    run_id          uuid NOT NULL,
    node_name       text NOT NULL,
    model_name      text NOT NULL,
    prompt_tokens   integer NOT NULL DEFAULT 0,
    cached_tokens   integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    thinking_tokens integer NOT NULL DEFAULT 0,
    cost_usd        numeric(12,6) NOT NULL DEFAULT 0,
    duration_ms     integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exec_id, created_at)
) PARTITION BY RANGE (created_at);
-- Monthly partitions are created by ops (e.g. node_executions_2026_11 FOR VALUES FROM ('2026-11-01') TO ('2026-12-01'));
-- expired ones: ALTER TABLE node_executions DETACH PARTITION ... CONCURRENTLY, archive to Parquet, drop.
CREATE TABLE IF NOT EXISTS node_executions_default PARTITION OF node_executions DEFAULT;

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id        bigserial PRIMARY KEY,
    run_id             uuid NOT NULL REFERENCES workflow_runs,
    exec_id            bigint,
    parent_artifact_id bigint REFERENCES artifacts,
    content_sha256     char(64) NOT NULL,
    storage_uri        text NOT NULL,          -- s3://... only, no large bodies
    summary_meta       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS hitl_decisions (
    decision_id        bigserial PRIMARY KEY,
    run_id             uuid NOT NULL REFERENCES workflow_runs,
    reversibility      text NOT NULL CHECK (reversibility IN ('REVERSIBLE','IRREVERSIBLE')),
    reason_code        text NOT NULL,
    status             text NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    decision_card_json jsonb NOT NULL,
    digest_batch_id    text,
    decided_by         uuid REFERENCES users,
    decided_at         timestamptz,
    CHECK (status = 'PENDING' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL))
) WITH (fillfactor = 80);
CREATE INDEX IF NOT EXISTS idx_hitl_pending ON hitl_decisions (run_id) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS semantic_contracts (
    contract_id            bigserial PRIMARY KEY,
    workspace_id           uuid NOT NULL REFERENCES workspaces,
    repo_key               text NOT NULL,
    symbol_fqn             text NOT NULL,
    linked_req_id          text,
    declarative_spec_json  jsonb NOT NULL,
    last_verified_ast_hash text,
    UNIQUE (repo_key, symbol_fqn)
);

CREATE TABLE IF NOT EXISTS resolved_facts (
    fact_id       bigserial PRIMARY KEY,
    workspace_id  uuid NOT NULL REFERENCES workspaces,
    repo_key      text NOT NULL,
    fact_key      text NOT NULL,
    fact_value    text NOT NULL,
    valid_until   timestamptz,
    source_commit text,
    UNIQUE (workspace_id, repo_key, fact_key)
);

CREATE TABLE IF NOT EXISTS semantic_cache (
    cache_id         bigserial PRIMARY KEY,
    workspace_id     uuid NOT NULL REFERENCES workspaces,
    task_type        text NOT NULL,
    embedding        vector(1536) NOT NULL,
    response_payload jsonb NOT NULL,
    schema_hash      text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semcache_hnsw ON semantic_cache USING hnsw (embedding vector_cosine_ops);

-- PHASE_2 (DEFER) tables: created now so promotion to ACCEPT_NOW needs no migration.
CREATE TABLE IF NOT EXISTS ux_overrides (
    override_id        bigserial PRIMARY KEY,
    workspace_id       uuid NOT NULL REFERENCES workspaces,
    view_key           text NOT NULL,
    component_selector text NOT NULL,
    config_json        jsonb NOT NULL,
    locked_by_user     boolean NOT NULL DEFAULT false,
    updated_by         uuid REFERENCES users,
    UNIQUE (workspace_id, view_key, component_selector)
);
CREATE TABLE IF NOT EXISTS research_sessions (
    research_id       bigserial PRIMARY KEY,
    run_id            uuid REFERENCES workflow_runs,
    trigger_source    text NOT NULL CHECK (trigger_source IN ('WATCHDOG','HUMAN','ERROR_SIGNATURE')),
    problem_signature text NOT NULL,
    query_embedding   vector(1536),
    sources_json      jsonb NOT NULL DEFAULT '[]',
    insight_brief_md  text
);
CREATE INDEX IF NOT EXISTS idx_research_hnsw ON research_sessions USING hnsw (query_embedding vector_cosine_ops);

-- idempotent upgrades
DO $$ BEGIN
  IF (SELECT numeric_scale FROM information_schema.columns
       WHERE table_name = 'workspace_budgets' AND column_name = 'spent_usd') <> 6 THEN
    ALTER TABLE workspace_budgets ALTER COLUMN limit_usd TYPE numeric(14,6), ALTER COLUMN spent_usd TYPE numeric(14,6);
  END IF;
END $$;

-- ---------------- Workspace VMs (Agora runtime) & CLI agents ----------------
CREATE SEQUENCE IF NOT EXISTS agdd_vm_seq START 1;
CREATE TABLE IF NOT EXISTS workspace_vms (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces ON DELETE CASCADE,
    canvas_id    integer NOT NULL UNIQUE,      -- Agora runtime id from the reserved AGDD range
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspace_agent_settings (
    workspace_id  uuid PRIMARY KEY REFERENCES workspaces ON DELETE CASCADE,
    settings_json jsonb NOT NULL,
    updated_by    uuid REFERENCES users,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cli_usage_snapshots (
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    cli          text NOT NULL,
    captured_at  timestamptz NOT NULL DEFAULT now(),
    rate_limits  jsonb,
    raw_text     text,
    PRIMARY KEY (workspace_id, cli)
);
