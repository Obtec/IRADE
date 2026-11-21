"""AGDD MVP implementation (DOC.md v2.0, target_phase = MVP).

Modules:
- spec_engine     In-Memory SQLite spec RDB (FK + FTS5), hydrate / write-through, executable scope
- preflight       4-stage Pre-Flight Conflict Gate (SQL -> batching -> cheap-model semantic -> gate)
- router          Fast-Track entry router + AMBIGUITY_GATE
- budget          80% fallback / 100% halt budget gate
- gateway         prefix-ordered prompts, output parser, 2.5k hard cap, batch tool_result clearing
- worker_db       worker-local SQLite (FTS5 offload, symbol index, watermarks, session_state)
- semantic_verify per-symbol incremental semantic lifting + contract comparison
- orchestrator    JSON graph validation/interpretation + SKIP LOCKED queue SQL
- sql/control_plane.sql  PostgreSQL control-plane DDL

Cheap-model calls are injected as ``llm(prompt, json_schema) -> dict`` so any provider can be used.
"""
