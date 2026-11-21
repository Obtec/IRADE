"""PostgreSQL implementations of the stores used by the services (DOC.md §4.1)."""
from __future__ import annotations

import json

from psycopg.types.json import Jsonb

from . import infra
from .spec_engine import SpecEngine


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ---------------- RBAC ----------------
ROLE_RANK = {"VIEWER": 0, "EDITOR": 1, "APPROVER": 2, "OWNER": 3}


def role_of(conn, workspace_id: str, user_id: str) -> str | None:
    r = conn.execute("SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
                     (workspace_id, user_id)).fetchone()
    return r["role"] if r else None


def require_role(conn, workspace_id, user_id, minimum: str) -> str:
    role = role_of(conn, workspace_id, user_id)
    if role is None or ROLE_RANK[role] < ROLE_RANK[minimum]:
        raise PermissionError(f"requires {minimum}")
    return role


# ---------------- spec snapshots (Hydrate / Write-Through) ----------------
def latest_snapshot(conn, workspace_id: str) -> dict | None:
    return conn.execute("SELECT * FROM spec_snapshots WHERE workspace_id=%s ORDER BY version DESC LIMIT 1",
                        (workspace_id,)).fetchone()


def save_snapshot(conn, workspace_id: str, snap: dict, passed: bool) -> int:
    r = conn.execute(
        "INSERT INTO spec_snapshots (workspace_id, version, srs_json, arc_json, sch_json, trace_json, conflict_passed) "
        "SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s, %s FROM spec_snapshots WHERE workspace_id=%s "
        "RETURNING snapshot_id",
        (workspace_id, Jsonb(snap["srs"]), Jsonb(snap["arc"]), Jsonb(snap["sch"]), Jsonb(snap["trace"]), passed,
         workspace_id)).fetchone()
    return r["snapshot_id"]


def hydrate_engine(conn, workspace_id: str) -> SpecEngine:
    row = latest_snapshot(conn, workspace_id)
    snap = {"srs": row["srs_json"], "arc": row["arc_json"], "sch": row["sch_json"], "trace": row["trace_json"]} \
        if row else {}
    return SpecEngine.hydrate(snap)  # unique shared-cache name per request


# ---------------- UX overrides ----------------
class PgOverrideStore:
    def get(self, workspace_id, view_key, selector):
        with infra.pg() as c:
            return c.execute("SELECT * FROM ux_overrides WHERE workspace_id=%s AND view_key=%s AND component_selector=%s",
                             (workspace_id, view_key, selector)).fetchone()

    def put(self, row):
        with infra.pg() as c:
            c.execute(
                "INSERT INTO ux_overrides (workspace_id, view_key, component_selector, config_json, locked_by_user, updated_by) "
                "VALUES (%(workspace_id)s, %(view_key)s, %(component_selector)s, %(cfg)s, %(locked_by_user)s, "
                "  CASE WHEN %(updated_by)s ~ '^[0-9a-f-]{36}$' THEN %(updated_by)s::uuid END) "
                "ON CONFLICT (workspace_id, view_key, component_selector) DO UPDATE SET config_json=EXCLUDED.config_json, "
                "locked_by_user=EXCLUDED.locked_by_user, updated_by=EXCLUDED.updated_by",
                dict(row, cfg=Jsonb(row["config_json"])))
            c.commit()

    def list(self, workspace_id, view_key=None):
        with infra.pg() as c:
            if view_key is None:
                return c.execute("SELECT * FROM ux_overrides WHERE workspace_id=%s ORDER BY view_key, component_selector",
                                 (workspace_id,)).fetchall()
            return c.execute("SELECT * FROM ux_overrides WHERE workspace_id=%s AND view_key=%s ORDER BY component_selector",
                             (workspace_id, view_key)).fetchall()

    def delete(self, workspace_id, view_key, selector) -> bool:
        with infra.pg() as c:
            n = c.execute("DELETE FROM ux_overrides WHERE workspace_id=%s AND view_key=%s AND component_selector=%s",
                          (workspace_id, view_key, selector)).rowcount
            c.commit()
            return n > 0

    def list_views(self, workspace_id) -> list[dict]:
        with infra.pg(readonly=True) as c:
            return c.execute(
                "SELECT view_key, count(*) AS count, count(*) FILTER (WHERE locked_by_user) AS locked_count "
                "FROM ux_overrides WHERE workspace_id=%s GROUP BY view_key ORDER BY view_key",
                (workspace_id,),
            ).fetchall()

    def locked_views(self, workspace_id) -> list[str]:
        with infra.pg(readonly=True) as c:
            return [
                r["view_key"] for r in c.execute(
                    "SELECT DISTINCT view_key FROM ux_overrides WHERE workspace_id=%s AND locked_by_user ORDER BY view_key",
                    (workspace_id,),
                ).fetchall()
            ]


# ---------------- collaborative CLI / chat messages ----------------
def add_collab_message(
    conn,
    workspace_id,
    role: str,
    mode: str,
    content: str,
    meta: dict | None = None,
    run_id=None,
    user_id=None,
) -> dict:
    uid = user_id if (user_id and len(str(user_id)) == 36) else None
    return conn.execute(
        "INSERT INTO collab_messages (workspace_id, role, mode, content, meta_json, run_id, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (workspace_id, role, mode, content, Jsonb(meta or {}), run_id, uid),
    ).fetchone()


def list_collab_messages(conn, workspace_id, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM collab_messages WHERE workspace_id=%s ORDER BY msg_id DESC LIMIT %s",
        (workspace_id, max(1, min(300, int(limit)))),
    ).fetchall()
    rows.reverse()
    return rows


def clear_collab_messages(conn, workspace_id) -> None:
    conn.execute("DELETE FROM collab_messages WHERE workspace_id=%s", (workspace_id,))


# ---------------- research sessions (pgvector) ----------------
class PgResearchStore:
    def nearest(self, embedding, k, min_sim):
        with infra.pg(readonly=True) as c:
            rows = c.execute(
                "SELECT research_id, insight_brief_md, 1 - (query_embedding <=> %s::vector) AS similarity "
                "FROM research_sessions WHERE insight_brief_md IS NOT NULL "
                "ORDER BY query_embedding <=> %s::vector LIMIT %s", (_vec(embedding), _vec(embedding), k)).fetchall()
        return [r for r in rows if r["similarity"] >= min_sim]

    def save(self, row):
        with infra.pg() as c:
            r = c.execute(
                "INSERT INTO research_sessions (run_id, trigger_source, problem_signature, query_embedding, sources_json, "
                "insight_brief_md) VALUES (%s,%s,%s,%s::vector,%s,%s) RETURNING research_id",
                (row.get("run_id"), row["trigger_source"], row["problem_signature"], _vec(row["query_embedding"]),
                 Jsonb({"sources": row["sources_json"], "objects": row.get("object_uris", [])}),
                 row["insight_brief_md"])).fetchone()
            c.commit()
            return r["research_id"]


# ---------------- semantic contracts ----------------
def load_contracts(conn, repo_key: str) -> dict[str, dict]:
    return {r["symbol_fqn"]: r["declarative_spec_json"] for r in conn.execute(
        "SELECT symbol_fqn, declarative_spec_json FROM semantic_contracts WHERE repo_key=%s", (repo_key,))}


def mark_verified(conn, repo_key: str, fqn: str, ast_hash: str) -> None:
    conn.execute("UPDATE semantic_contracts SET last_verified_ast_hash=%s WHERE repo_key=%s AND symbol_fqn=%s",
                 (ast_hash, repo_key, fqn))


def record_execution(conn, run_id, node, model, usage: dict, cost: float, ms: int) -> None:
    conn.execute(
        "INSERT INTO node_executions (run_id, node_name, model_name, prompt_tokens, cached_tokens, output_tokens, "
        "thinking_tokens, cost_usd, duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (run_id, node, model, usage.get("prompt_tokens", 0), usage.get("cached_tokens", 0),
         usage.get("output_tokens", 0), usage.get("thinking_tokens", 0), cost, ms))


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)
