"""Persistent per-workspace In-Memory SQLite Spec Engine store (DOC.md §3.1-3.2, §5.2)."""
from __future__ import annotations

import sqlite3
import threading

from . import pgstore
from .spec_engine import TABLES, SpecEngine

_entries: dict[str, tuple[int | None, SpecEngine, threading.RLock]] = {}
_global_lock = threading.Lock()

_ALLOWED_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _row_snapshot(row: dict | None) -> dict:
    if not row:
        return {}
    return {
        "srs": row.get("srs_json") or [],
        "arc": row.get("arc_json") or [],
        "sch": row.get("sch_json") or [],
        "trace": row.get("trace_json") or [],
    }


def _close_quietly(eng: SpecEngine | None) -> None:
    if eng is not None:
        try:
            eng.close()
        except Exception:
            pass


def get_engine(conn, ws: str) -> tuple[SpecEngine, threading.RLock, dict | None]:
    """Return a cached In-Memory SpecEngine hydrated to the workspace's latest snapshot."""
    ws_key = str(ws)
    latest = pgstore.latest_snapshot(conn, ws)
    snap_id = latest["snapshot_id"] if latest else None

    with _global_lock:
        hit = _entries.get(ws_key)
        if hit is not None and hit[0] == snap_id:
            return hit[1], hit[2], latest

    eng = SpecEngine.hydrate(_row_snapshot(latest))
    lock = threading.RLock()
    old_eng: SpecEngine | None = None
    with _global_lock:
        prev = _entries.get(ws_key)
        if prev is not None and prev[0] == snap_id:
            _close_quietly(eng)
            return prev[1], prev[2], latest
        if prev is not None:
            old_eng = prev[1]
        _entries[ws_key] = (snap_id, eng, lock)
    _close_quietly(old_eng)
    return eng, lock, latest


def update(ws: str, snapshot_id: int, snapshot: dict) -> None:
    """Write-through refresh of the cached In-Memory SpecEngine after a snapshot commit."""
    ws_key = str(ws)
    eng = SpecEngine.hydrate(snapshot or {})
    lock = threading.RLock()
    old_eng: SpecEngine | None = None
    with _global_lock:
        prev = _entries.get(ws_key)
        if prev is not None:
            old_eng = prev[1]
        _entries[ws_key] = (snapshot_id, eng, lock)
    _close_quietly(old_eng)


def invalidate(ws: str) -> None:
    ws_key = str(ws)
    old_eng: SpecEngine | None = None
    with _global_lock:
        prev = _entries.pop(ws_key, None)
        if prev is not None:
            old_eng = prev[1]
    _close_quietly(old_eng)


def summary(conn, ws: str) -> dict:
    eng, lock, latest = get_engine(conn, ws)
    with lock:
        accept = {t.lower(): len(eng.rows(t, accept_only=True)) for t in TABLES}
        total = {t.lower(): len(eng.rows(t, accept_only=False)) for t in TABLES}
        trace_count = eng.db.execute("SELECT count(*) FROM spec_traceability").fetchone()[0]
    return {
        "snapshot_id": latest["snapshot_id"] if latest else None,
        "version": latest["version"] if latest else 0,
        "conflict_passed": bool(latest["conflict_passed"]) if latest else False,
        "accept": accept,
        "total": total,
        "trace_count": trace_count,
    }


def query_readonly(conn, ws: str, sql: str, params=(), limit: int = 200) -> dict:
    """Execute a read-only SELECT / WITH query against the workspace's In-Memory SQLite engine."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise ValueError("sql query is required")
    head = text.lstrip("(").lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError("only read-only SELECT / WITH queries are permitted")
    cap = max(1, min(500, int(limit)))
    eng, lock, _ = get_engine(conn, ws)

    def _authorizer(action, arg1, arg2, dbname, source):
        if action in _ALLOWED_READ_ACTIONS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "data_version" and arg2 is None:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    with lock:
        eng.db.set_authorizer(_authorizer)
        try:
            cur = eng.db.execute(text, tuple(params or ()))
            cols = [d[0] for d in (cur.description or [])]
            fetched = cur.fetchmany(cap + 1)
        finally:
            eng.db.set_authorizer(None)
    truncated = len(fetched) > cap
    rows = [dict(zip(cols, tuple(r))) for r in fetched[:cap]]
    return {"columns": cols, "rows": rows, "truncated": truncated}


def search_fts(conn, ws: str, query: str, limit: int = 20) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    cap = max(1, min(100, int(limit)))
    eng, lock, _ = get_engine(conn, ws)
    with lock:
        hits = eng.search(q, cap)
        out = []
        for spec_type, spec_id in hits:
            row = eng.get(spec_type, spec_id)
            if row is not None:
                out.append({"spec_type": spec_type, "spec_id": spec_id, "row": row})
        return out
