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
-- Subscription-CLI usage scrapes are no longer collected (agents use the gateway API key); drop old data.
DROP TABLE IF EXISTS cli_usage_snapshots;

-- IRADE Studio workspace link: an AGDD workspace addressed as "irade-<id>" is the same workspace as
-- Studio workspace <id> (same members via the gateway, same runtime VM canvas).
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS irade_workspace_id integer UNIQUE;

-- Collaborative agent chat / Conversational AGDD message log per workspace ('cli_chat' = direct agent chat).
CREATE TABLE IF NOT EXISTS collab_messages (
    msg_id       bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    role         text NOT NULL CHECK (role IN ('user','assistant','system','event')),
    mode         text NOT NULL DEFAULT 'agdd' CHECK (mode IN ('agdd','cli_chat','ux_canvas')),
    content      text NOT NULL,
    meta_json    jsonb NOT NULL DEFAULT '{}',
    run_id       uuid REFERENCES workflow_runs ON DELETE SET NULL,
    created_by   uuid REFERENCES users,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_collab_ws ON collab_messages (workspace_id, msg_id DESC);

-- ---------------- Multi-agent teams (canvas-orchestrated) ----------------
CREATE TABLE IF NOT EXISTS agent_teams (
    team_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    name         text NOT NULL,
    graph_json   jsonb NOT NULL,             -- validated by multi_agent.validate_team_graph
    updated_by   uuid REFERENCES users,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_teams_ws ON agent_teams (workspace_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS agent_team_runs (
    team_run_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id      uuid REFERENCES agent_teams ON DELETE SET NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    graph_json   jsonb NOT NULL,             -- snapshot of the graph that ran
    request      text NOT NULL DEFAULT '',
    status       text NOT NULL CHECK (status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','STEP_LIMIT','BLOCKED')),
    node_state   jsonb NOT NULL DEFAULT '{}',
    result_json  jsonb NOT NULL DEFAULT '{}',
    created_by   uuid REFERENCES users,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_team_runs_ws ON agent_team_runs (workspace_id, created_at DESC);
CREATE TABLE IF NOT EXISTS agent_team_events (
    event_id     bigserial PRIMARY KEY,
    team_run_id  uuid NOT NULL REFERENCES agent_team_runs ON DELETE CASCADE,
    seq          integer NOT NULL,
    type         text NOT NULL,
    node         text,
    target       text,
    content      text NOT NULL DEFAULT '',
    meta_json    jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_team_events_run ON agent_team_events (team_run_id, seq);
-- BLOCKED (guardrail tripwire) was added after the first release: refresh the CHECK on existing databases.
ALTER TABLE agent_team_runs DROP CONSTRAINT IF EXISTS agent_team_runs_status_check;
ALTER TABLE agent_team_runs ADD CONSTRAINT agent_team_runs_status_check
    CHECK (status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','STEP_LIMIT','BLOCKED'));
ALTER TABLE agent_team_runs ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]';  -- run-level media
-- Multimodal team inputs (images, video, audio, PDF, text …). Uploaded in <= 4 MiB chunks (nginx body limit),
-- assembled lazily: the VM copy is streamed chunk by chunk and cached by sha256.
CREATE TABLE IF NOT EXISTS agent_team_attachments (
    attachment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    name          text NOT NULL,
    mime          text NOT NULL,
    modality      text NOT NULL CHECK (modality IN ('text','image','video','audio','pdf','file')),
    size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
    chunk_size    integer NOT NULL,
    sha256        text,
    status        text NOT NULL DEFAULT 'UPLOADING' CHECK (status IN ('UPLOADING','READY')),
    excerpt       text,                      -- decoded text for text-like files (prompt context without a VM)
    created_by    uuid REFERENCES users,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_team_attachments_ws ON agent_team_attachments (workspace_id, created_at DESC);
CREATE TABLE IF NOT EXISTS agent_team_attachment_chunks (
    attachment_id uuid NOT NULL REFERENCES agent_team_attachments ON DELETE CASCADE,
    idx           integer NOT NULL,
    data          bytea NOT NULL,
    PRIMARY KEY (attachment_id, idx)
);

-- ---------------- Prompt dependency graphs & nested pre-prompt groups ----------------
CREATE TABLE IF NOT EXISTS prompt_graphs (
    graph_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    name         text NOT NULL,
    graph_json   jsonb NOT NULL,
    updated_by   uuid REFERENCES users,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prompt_graphs_ws ON prompt_graphs (workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS prompt_graph_runs (
    run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_id     uuid REFERENCES prompt_graphs ON DELETE SET NULL,
    workspace_id uuid NOT NULL REFERENCES workspaces ON DELETE CASCADE,
    target_type  text NOT NULL CHECK (target_type IN ('all','node','group','subset')),
    target_id    text,
    graph_json   jsonb NOT NULL,
    status       text NOT NULL CHECK (status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')),
    node_state   jsonb NOT NULL DEFAULT '{}',
    result_json  jsonb NOT NULL DEFAULT '{}',
    created_by   uuid REFERENCES users,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prompt_graph_runs_ws ON prompt_graph_runs (workspace_id, created_at DESC);
ALTER TABLE prompt_graph_runs DROP CONSTRAINT IF EXISTS prompt_graph_runs_target_type_check;
ALTER TABLE prompt_graph_runs ADD CONSTRAINT prompt_graph_runs_target_type_check
    CHECK (target_type IN ('all','node','group','subset'));

CREATE TABLE IF NOT EXISTS prompt_graph_events (
    event_id     bigserial PRIMARY KEY,
    run_id       uuid NOT NULL REFERENCES prompt_graph_runs ON DELETE CASCADE,
    seq          integer NOT NULL,
    type         text NOT NULL,
    node         text,
    target       text,
    content      text NOT NULL DEFAULT '',
    meta_json    jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prompt_graph_events_run ON prompt_graph_events (run_id, seq);
