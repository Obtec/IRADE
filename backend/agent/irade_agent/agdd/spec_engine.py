"""In-Memory relational spec engine (DOC.md §3.1-3.2, §5.2).

Specs live in a shared-cache in-memory SQLite DB with FK + FTS5. The engine is
hydrated from a snapshot dict (the PostgreSQL ``spec_snapshots`` row) and every
mutation is written through to an optional ``sink`` callable.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable

ADOPTION = ("ACCEPT_NOW", "DEFER", "REJECT", "HOLD")
PHASES = ("MVP", "PHASE_2", "PHASE_3", "NONE")

DDL = """
PRAGMA foreign_keys = ON;
CREATE TABLE srs_requirements (
    req_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    req_type TEXT NOT NULL CHECK (req_type IN ('FUNCTIONAL','NON_FUNCTIONAL','CONSTRAINT')),
    title TEXT NOT NULL,
    requirement_content TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    adoption_status TEXT NOT NULL CHECK (adoption_status IN ('ACCEPT_NOW','DEFER','REJECT','HOLD')),
    target_phase TEXT NOT NULL CHECK (target_phase IN ('MVP','PHASE_2','PHASE_3','NONE')),
    priority TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE arch_definitions (
    arch_id TEXT PRIMARY KEY,
    layer TEXT NOT NULL CHECK (layer IN ('CLIENT','CONTROL_PLANE','IN_MEMORY_SPEC','WORKER','GATEWAY','DATA_PLANE','INFRA')),
    component_name TEXT NOT NULL,
    role_content TEXT NOT NULL,
    tech_stack TEXT NOT NULL,
    interface_contract TEXT NOT NULL,
    adoption_status TEXT NOT NULL CHECK (adoption_status IN ('ACCEPT_NOW','DEFER','REJECT','HOLD')),
    target_phase TEXT NOT NULL CHECK (target_phase IN ('MVP','PHASE_2','PHASE_3','NONE')),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE schema_definitions (
    schema_id TEXT PRIMARY KEY,
    storage_engine TEXT NOT NULL CHECK (storage_engine IN ('POSTGRES_RDS','SQLITE_IN_MEMORY','SQLITE_WORKER','VALKEY','OBJECT_STORAGE')),
    table_or_key_name TEXT NOT NULL,
    role_content TEXT NOT NULL,
    columns_and_ddl_json TEXT NOT NULL,
    adoption_status TEXT NOT NULL CHECK (adoption_status IN ('ACCEPT_NOW','DEFER','REJECT','HOLD')),
    target_phase TEXT NOT NULL CHECK (target_phase IN ('MVP','PHASE_2','PHASE_3','NONE')),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE spec_traceability (
    trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK (source_type IN ('SRS','ARC','SCH')),
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('SRS','ARC','SCH')),
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('IMPLEMENTS','DEPENDS_ON','PERSISTS_IN','CONSTRAINS')),
    UNIQUE (source_type, source_id, target_type, target_id, relation_type)
);
CREATE TABLE spec_conflict_reports (
    conflict_id TEXT PRIMARY KEY,
    check_stage TEXT NOT NULL CHECK (check_stage IN ('SQL_DETERMINISTIC','LLM_SEMANTIC')),
    severity TEXT NOT NULL CHECK (severity IN ('CRITICAL','WARNING')),
    conflict_type TEXT NOT NULL CHECK (conflict_type IN ('ADOPTION_MISMATCH','ORPHAN_SPEC','DUPLICATE_SCHEMA',
        'REQ_VS_REQ','REQ_VS_ARC','ARC_VS_SCH','IMPLICIT_SCOPE_CONFLICT')),
    involved_ids_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    resolution_options TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','WAIVED')),
    resolved_by TEXT,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE spec_fts USING fts5(
    spec_type, spec_id UNINDEXED, domain_or_layer, content_text, tokenize = 'unicode61'
);
CREATE INDEX idx_srs_accept ON srs_requirements (domain, priority) WHERE adoption_status = 'ACCEPT_NOW';
CREATE INDEX idx_arc_accept ON arch_definitions (layer) WHERE adoption_status = 'ACCEPT_NOW';
CREATE INDEX idx_sch_accept ON schema_definitions (storage_engine) WHERE adoption_status = 'ACCEPT_NOW';
"""

# spec_type -> (table, pk, domain/layer column, text columns)
TABLES = {
    "SRS": ("srs_requirements", "req_id", "domain", ("title", "requirement_content", "acceptance_criteria")),
    "ARC": ("arch_definitions", "arch_id", "layer", ("component_name", "role_content", "tech_stack", "interface_contract")),
    "SCH": ("schema_definitions", "schema_id", "storage_engine", ("table_or_key_name", "role_content", "columns_and_ddl_json")),
}
SNAPSHOT_KEYS = {"SRS": "srs", "ARC": "arc", "SCH": "sch"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SpecEngine:
    def __init__(self, run_id: str | None = None, sink: Callable[[str, dict], None] | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        uri = f"file:spec_engine_{self.run_id}?mode=memory&cache=shared"
        self.db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self.sink = sink  # write-through hook: sink(event, payload)

    # ---------- mutation ----------
    def upsert(self, spec_type: str, row: dict) -> None:
        table, pk, dom, texts = TABLES[spec_type]
        row = dict(row)
        row.setdefault("updated_at", now_iso())
        if spec_type == "SCH" and not isinstance(row.get("columns_and_ddl_json", ""), str):
            row["columns_and_ddl_json"] = json.dumps(row["columns_and_ddl_json"], ensure_ascii=False, sort_keys=True)
        old = self.db.execute(f"SELECT version FROM {table} WHERE {pk}=?", (row[pk],)).fetchone()
        if old is not None and "version" not in row:
            row["version"] = old["version"] + 1
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.db:
            self.db.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
            self.db.execute("DELETE FROM spec_fts WHERE spec_type=? AND spec_id=?", (spec_type, row[pk]))
            self.db.execute(
                "INSERT INTO spec_fts (spec_type, spec_id, domain_or_layer, content_text) VALUES (?,?,?,?)",
                (spec_type, row[pk], row[dom], " ".join(str(row.get(c, "")) for c in texts)),
            )
        self._emit("upsert", {"spec_type": spec_type, "row": self.get(spec_type, row[pk])})

    def set_adoption(self, spec_type: str, spec_id: str, status: str) -> None:
        if status not in ADOPTION:
            raise ValueError(status)
        row = self.get(spec_type, spec_id)
        if row is None:
            raise KeyError(spec_id)
        row["adoption_status"] = status
        row.pop("version")
        row.pop("updated_at")
        self.upsert(spec_type, row)

    def trace(self, source_type, source_id, target_type, target_id, relation_type) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO spec_traceability (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES (?,?,?,?,?)", (source_type, source_id, target_type, target_id, relation_type))
        self._emit("trace", {"source": [source_type, source_id], "target": [target_type, target_id], "rel": relation_type})

    def _emit(self, event, payload):
        if self.sink:
            self.sink(event, payload)

    # ---------- query ----------
    def get(self, spec_type: str, spec_id: str) -> dict | None:
        table, pk, _, _ = TABLES[spec_type]
        r = self.db.execute(f"SELECT * FROM {table} WHERE {pk}=?", (spec_id,)).fetchone()
        return dict(r) if r else None

    def rows(self, spec_type: str, accept_only: bool = True) -> list[dict]:
        table, pk, _, _ = TABLES[spec_type]
        where = "WHERE adoption_status='ACCEPT_NOW'" if accept_only else ""
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} {where} ORDER BY {pk}")]

    def linked(self, spec_type: str, spec_id: str) -> list[tuple[str, str, str]]:
        """1-hop traceability neighbours as (type, id, relation)."""
        out = self.db.execute(
            "SELECT target_type, target_id, relation_type FROM spec_traceability WHERE source_type=? AND source_id=? "
            "UNION SELECT source_type, source_id, relation_type FROM spec_traceability WHERE target_type=? AND target_id=?",
            (spec_type, spec_id, spec_type, spec_id)).fetchall()
        return [tuple(r) for r in out]

    def search(self, query: str, limit: int = 10) -> list[tuple[str, str]]:
        try:
            out = self.db.execute(
                "SELECT spec_type, spec_id FROM spec_fts WHERE spec_fts MATCH ? ORDER BY bm25(spec_fts) LIMIT ?",
                (query, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [tuple(r) for r in out]

    def executable_scope(self, domain: str | None = None) -> dict:
        """§5.2: ACCEPT_NOW SRS rows (optionally one domain) + linked ACCEPT_NOW ARC/SCH rows only."""
        sql = "SELECT * FROM srs_requirements WHERE adoption_status='ACCEPT_NOW'"
        args: tuple = ()
        if domain:
            sql += " AND domain=?"
            args = (domain,)
        reqs = [dict(r) for r in self.db.execute(sql + " ORDER BY priority, req_id", args)]
        arc, sch = {}, {}
        for req in reqs:
            for typ, sid, _ in self.linked("SRS", req["req_id"]):
                if typ in ("ARC", "SCH"):
                    row = self.get(typ, sid)
                    if row and row["adoption_status"] == "ACCEPT_NOW":
                        (arc if typ == "ARC" else sch)[sid] = row
        return {"requirements": reqs, "architecture": list(arc.values()), "schemas": list(sch.values())}

    # ---------- snapshot (Hydrate / Write-Through) ----------
    def snapshot(self) -> dict:
        snap = {SNAPSHOT_KEYS[t]: self.rows(t, accept_only=False) for t in TABLES}
        snap["trace"] = [dict(r) for r in self.db.execute(
            "SELECT source_type, source_id, target_type, target_id, relation_type FROM spec_traceability ORDER BY trace_id")]
        return snap

    @classmethod
    def hydrate(cls, snapshot: dict, run_id: str | None = None, sink=None) -> "SpecEngine":
        eng = cls(run_id)
        for t, key in SNAPSHOT_KEYS.items():
            for row in snapshot.get(key, []):
                eng.upsert(t, row)
        for tr in snapshot.get("trace", []):
            eng.trace(tr["source_type"], tr["source_id"], tr["target_type"], tr["target_id"], tr["relation_type"])
        eng.sink = sink
        return eng

    # ---------- conflict reports ----------
    def record_conflicts(self, conflicts: Iterable[dict]) -> None:
        with self.db:
            for c in conflicts:
                self.db.execute(
                    "INSERT OR REPLACE INTO spec_conflict_reports (conflict_id, check_stage, severity, conflict_type, "
                    "involved_ids_json, explanation, resolution_options, status, created_at) VALUES (?,?,?,?,?,?,?,'OPEN',?)",
                    (c["conflict_id"], c["check_stage"], c["severity"], c["conflict_type"],
                     json.dumps(c["involved_ids"]), c["explanation"],
                     json.dumps(c["resolution_options"], ensure_ascii=False), now_iso()))

    def open_conflicts(self, severity: str | None = None) -> list[dict]:
        sql = "SELECT * FROM spec_conflict_reports WHERE status='OPEN'"
        args: tuple = ()
        if severity:
            sql += " AND severity=?"
            args = (severity,)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY conflict_id", args)]

    def resolve_conflict(self, conflict_id: str, user: str, waive: bool = False) -> None:
        with self.db:
            self.db.execute("UPDATE spec_conflict_reports SET status=?, resolved_by=? WHERE conflict_id=?",
                            ("WAIVED" if waive else "RESOLVED", user, conflict_id))

    def close(self):
        self.db.close()
