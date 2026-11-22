"""End-to-end tests for In-Memory Spec DB, VM file cells/IDE, pure CLI & AGDD collab, and 2D UX Canvas."""
from __future__ import annotations

import contextlib
import copy
import json
import os
import sqlite3
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    pg_mod = types.ModuleType("psycopg")
    rows_mod = types.ModuleType("psycopg.rows")
    types_mod = types.ModuleType("psycopg.types")
    json_mod = types.ModuleType("psycopg.types.json")

    class Jsonb:
        def __init__(self, obj):
            self.obj = obj

    rows_mod.dict_row = lambda *a, **k: None
    json_mod.Jsonb = Jsonb
    types_mod.json = json_mod
    pg_mod.rows = rows_mod
    pg_mod.types = types_mod
    pg_mod.connect = lambda *a, **k: None
    sys.modules["psycopg"] = pg_mod
    sys.modules["psycopg.rows"] = rows_mod
    sys.modules["psycopg.types"] = types_mod
    sys.modules["psycopg.types.json"] = json_mod

from tests.fake_runtime import FakeRuntime  # noqa: E402
from irade_agent.agdd import (  # noqa: E402
    api,
    infra,
    pgstore,
    router,
    spec_store,
    ux_workbench,
    vm,
    vm_service,
    vm_sync,
    worker_vm,
)
from irade_agent.agdd.seed import PLATFORM_SNAPSHOT  # noqa: E402
from irade_agent.agdd.vm import RuntimeClient, VmShell  # noqa: E402


@contextlib.contextmanager
def _fake_rt(tmp_path: Path, monkeypatch, canvas_id: int):
    sock = tmp_path / "rt.sock"
    vms = tmp_path / "vms"
    vm_home = vms / str(canvas_id)
    vm_home.mkdir(parents=True, exist_ok=True)
    rt = FakeRuntime(str(sock), str(vms))
    monkeypatch.setenv("AGORA_RUNTIME_SOCKET", str(sock))
    monkeypatch.setenv("AGDD_EXECUTION", "vm")
    OrigVmShell = VmShell

    class _LocalVmShell(OrigVmShell):
        def __init__(self, client, cid, name="agdd", owner=vm.SYSTEM_OWNER, home=None):
            super().__init__(client, cid, name, owner=owner, home=home or str(vms / str(cid)))

    shells: dict[tuple[str, str], OrigVmShell] = {}
    monkeypatch.setattr(vm, "VmShell", _LocalVmShell)
    monkeypatch.setattr(
        vm_service,
        "shell",
        lambda ws, name="agdd": shells.setdefault(
            (str(ws), name),
            _LocalVmShell(RuntimeClient(), vm_service.canvas_for(ws), name),
        ),
    )
    try:
        yield rt, vm_home
    finally:
        for sh in shells.values():
            try:
                sh.close()
            except Exception:
                pass
        rt.stop()

USER_ID = "11111111-1111-1111-1111-111111111111"
WS_ID = "22222222-2222-2222-2222-222222222222"


def _unwrap(val):
    return val.obj if hasattr(val, "obj") else val


class _Cursor:
    def __init__(self, rows=None, rowcount=None):
        self._rows = list(rows or [])
        self.rowcount = rowcount if rowcount is not None else len(self._rows)

    def fetchone(self):
        return copy.deepcopy(self._rows[0]) if self._rows else None

    def fetchall(self):
        return copy.deepcopy(self._rows)


class FakeState:
    """In-memory state backing FakePgConn and monkeypatched pgstore helpers."""

    def __init__(self, ws_id: str = WS_ID, canvas_id: int = 1):
        self.ws_id = ws_id
        self.canvas_id = canvas_id
        self.snapshots: list[dict] = []
        self.next_snap_id = 1
        self.overrides: dict[tuple[str, str], dict] = {}
        self.settings = {
            "cli": "claude",
            "model": "sonnet",
            "effort": "high",
            "session": "per-run",
            "permission": "sandbox",
            "timeout_s": 1200,
            "env": {},
            "instructions": "Always cite SRS IDs before editing.",
            "default_repo": "workspace",
            "ux_auto_canvas": True,
        }
        self.graphs: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.hitl: dict[int, dict] = {}
        self.next_hitl_id = 1
        self.jobs: list[dict] = []
        self.collab_messages: list[dict] = []
        self.next_msg_id = 1
        self.budgets: dict[tuple[str, str], dict] = {}
        self.members: dict[str, dict] = {
            USER_ID: {"user_id": USER_ID, "email": f"{USER_ID}@agdd.local", "role": "OWNER"}
        }
        self.resolved_facts: list[dict] = []

    def add_snapshot(self, snap: dict, passed: bool = False) -> int:
        sid = self.next_snap_id
        self.next_snap_id += 1
        self.snapshots.append({
            "snapshot_id": sid,
            "workspace_id": self.ws_id,
            "version": sid,
            "srs_json": copy.deepcopy(snap.get("srs", [])),
            "arc_json": copy.deepcopy(snap.get("arc", [])),
            "sch_json": copy.deepcopy(snap.get("sch", [])),
            "trace_json": copy.deepcopy(snap.get("trace", [])),
            "conflict_passed": passed,
        })
        return sid

    def latest_snapshot(self) -> dict | None:
        return copy.deepcopy(self.snapshots[-1]) if self.snapshots else None


class FakePgConn:
    def __init__(self, state: FakeState):
        self.s = state

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        return None

    def execute(self, sql: str, params=()):
        q = " ".join(sql.split())
        if isinstance(params, dict):
            if "INSERT INTO job_queue" in q:
                self.s.jobs.append({
                    "run_id": params["run_id"],
                    "node_name": params["node_name"],
                    "priority": params.get("priority", 5),
                })
                return _Cursor()
            return _Cursor()

        if "SELECT DISTINCT ON (graph_id)" in q and "FROM graph_definitions" in q:
            return _Cursor(self.s.graphs[-1:])
        if "INSERT INTO graph_definitions" in q:
            gid = str(uuid.uuid4())
            g = {"graph_id": gid, "version": 1, "definition_json": copy.deepcopy(_unwrap(params[1]))}
            self.s.graphs.append(g)
            return _Cursor([g])
        if "INSERT INTO workflow_runs" in q:
            rid = str(uuid.uuid4())
            row = {
                "run_id": rid,
                "graph_id": params[0],
                "graph_version": params[1],
                "snapshot_id": params[2],
                "route_mode": params[3],
                "current_node": params[4],
                "status": "PENDING",
                "handoff_json": copy.deepcopy(_unwrap(params[5])),
                "version": 1,
                "created_at": "2026-11-22T00:00:00Z",
                "updated_at": "2026-11-22T00:00:00Z",
            }
            self.s.runs[rid] = row
            return _Cursor([{"run_id": rid}])
        if "FROM workflow_runs r JOIN graph_definitions g" in q and "LIMIT 15" in q:
            return _Cursor(list(reversed(list(self.s.runs.values()))))
        if "SELECT meta_json FROM collab_messages" in q:
            chats = [m for m in reversed(self.s.collab_messages) if m["mode"] == "cli_chat" and m["role"] == "assistant"]
            return _Cursor([{"meta_json": chats[0]["meta_json"]}] if chats else [])
        if "SELECT h.decision_id FROM hitl_decisions" in q:
            pend = [h for h in reversed(list(self.s.hitl.values()))
                    if h["status"] == "PENDING" and h.get("reason_code") == "UX_VISUAL_REQUEST"]
            return _Cursor([{"decision_id": pend[0]["decision_id"]}] if pend else [])
        if "SELECT h.decision_id, h.run_id" in q and "FROM hitl_decisions" in q:
            pend = [h for h in reversed(list(self.s.hitl.values()))
                    if h["status"] == "PENDING" and h.get("reason_code") == "UX_VISUAL_REQUEST"]
            rows = []
            for h in pend:
                run = self.s.runs.get(h["run_id"], {})
                rows.append({
                    "decision_id": h["decision_id"],
                    "run_id": h["run_id"],
                    "decision_card_json": h.get("decision_card_json"),
                    "handoff_json": run.get("handoff_json"),
                })
            return _Cursor(rows)
        if "UPDATE hitl_decisions SET status='APPROVED'" in q:
            did = int(params[1])
            h = self.s.hitl.get(did)
            if h and h["status"] == "PENDING":
                h["status"] = "APPROVED"
                h["decided_by"] = params[0]
                return _Cursor([{"run_id": h["run_id"]}])
            return _Cursor([])
        if "SELECT run_id, current_node, status, handoff_json FROM workflow_runs WHERE run_id=%s" in q:
            r = self.s.runs.get(str(params[0]))
            return _Cursor([r] if r else [])
        if "SELECT g.workspace_id FROM workflow_runs r JOIN graph_definitions g" in q:
            r = self.s.runs.get(str(params[0]))
            return _Cursor([{"workspace_id": self.s.ws_id}] if r else [])
        if "UPDATE workflow_runs SET status='HALTED'" in q:
            r = self.s.runs.get(str(params[1]))
            if r and r.get("status") in ("PENDING", "RUNNING", "WAITING_HITL"):
                r["status"] = "HALTED"
                r["handoff_json"] = {**(r.get("handoff_json") or {}), **copy.deepcopy(_unwrap(params[0]))}
                r["version"] = r.get("version", 1) + 1
                return _Cursor([{"run_id": r["run_id"]}])
            return _Cursor([])
        if "UPDATE job_queue SET status='FAILED'" in q:
            rid = str(params[0])
            for j in self.s.jobs:
                if str(j.get("run_id")) == rid and j.get("status", "PENDING") == "PENDING":
                    j["status"] = "FAILED"
            return _Cursor([])
        if "UPDATE hitl_decisions SET status='REJECTED', decided_by=%s" in q:
            by, rid = params[0], str(params[1])
            for h in self.s.hitl.values():
                if str(h.get("run_id")) == rid and h.get("status") == "PENDING":
                    h["status"] = "REJECTED"
                    h["decided_by"] = by
            return _Cursor([])
        if "SELECT run_id FROM hitl_decisions WHERE decision_id=%s" in q:
            h = self.s.hitl.get(int(params[0]))
            return _Cursor([{"run_id": h["run_id"]}] if h else [])
        if "UPDATE hitl_decisions SET status=%s, decided_by=%s" in q:
            status, user, card_patch, did = params[0], params[1], _unwrap(params[2]), int(params[3])
            h = self.s.hitl.get(did)
            if h and h.get("status") == "PENDING":
                h["status"] = status
                h["decided_by"] = user
                h["decision_card_json"] = {**(h.get("decision_card_json") or {}), **copy.deepcopy(card_patch)}
                return _Cursor([{"run_id": h["run_id"]}])
            return _Cursor([])
        if "SELECT count(*) AS n FROM hitl_decisions WHERE run_id=%s" in q:
            rid = str(params[0])
            cnt = sum(
                1 for h in self.s.hitl.values()
                if str(h.get("run_id")) == rid and h.get("status") == "PENDING" and h.get("reversibility") == "IRREVERSIBLE"
            )
            return _Cursor([{"n": cnt}])
        if "SELECT current_node, status FROM workflow_runs WHERE run_id=%s" in q:
            r = self.s.runs.get(str(params[0]))
            return _Cursor([r] if r else [])
        if "UPDATE workflow_runs SET status=%s, version=version+1" in q:
            new_status, patch, rid = params[0], _unwrap(params[1]), str(params[2])
            r = self.s.runs.get(rid)
            if r:
                r["status"] = new_status
                r["handoff_json"] = {**(r.get("handoff_json") or {}), **copy.deepcopy(patch)}
                r["version"] = r.get("version", 1) + 1
            return _Cursor([])
        if "UPDATE workflow_runs SET status='RUNNING', handoff_json=%s" in q:
            rid = str(params[1])
            r = self.s.runs[rid]
            r["status"] = "RUNNING"
            r["handoff_json"] = copy.deepcopy(_unwrap(params[0]))
            r["version"] += 1
            return _Cursor([])
        if "INSERT INTO workspace_budgets" in q:
            ws_k, period_k, lim = str(params[0]), str(params[1]), float(params[2])
            self.s.budgets[(ws_k, period_k)] = {
                "workspace_id": ws_k,
                "period_ym": period_k,
                "limit_usd": lim,
                "spent_usd": 0.0,
            }
            return _Cursor([])
        if "SELECT * FROM workspace_budgets" in q:
            ws_k = str(params[0])
            rows = [v for (w, _), v in sorted(self.s.budgets.items(), reverse=True) if w == ws_k]
            return _Cursor(rows[:1])
        if "INSERT INTO resolved_facts" in q:
            ws_k, fkey, fval = str(params[0]), str(params[1]), str(params[2])
            self.s.resolved_facts = [
                f for f in self.s.resolved_facts
                if not (f["workspace_id"] == ws_k and f["repo_key"] == "_research" and f["fact_key"] == fkey)
            ]
            self.s.resolved_facts.append({
                "workspace_id": ws_k,
                "repo_key": "_research",
                "fact_key": fkey,
                "fact_value": fval,
            })
            return _Cursor([])
        if "FROM resolved_facts WHERE workspace_id=%s AND repo_key='_research'" in q:
            ws_k = str(params[0])
            rows = [f for f in reversed(self.s.resolved_facts) if f["workspace_id"] == ws_k and f["repo_key"] == "_research"]
            return _Cursor(rows[:20])
        if "SELECT m.user_id, u.email, m.role FROM workspace_members" in q:
            rank = {"OWNER": 0, "APPROVER": 1, "EDITOR": 2, "VIEWER": 3}
            rows = sorted(self.s.members.values(), key=lambda m: (rank.get(m["role"], 9), m["user_id"]))
            return _Cursor(rows)
        if "SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s" in q:
            m = self.s.members.get(str(params[1]))
            return _Cursor([{"role": m["role"]}] if m else [])
        if "SELECT count(*) AS n FROM workspace_members WHERE workspace_id=%s AND role='OWNER'" in q:
            n = sum(1 for m in self.s.members.values() if m["role"] == "OWNER")
            return _Cursor([{"n": n}])
        if "INSERT INTO workspace_members VALUES" in q:
            uid, role = str(params[1]), str(params[2])
            self.s.members[uid] = {"user_id": uid, "email": f"{uid}@agdd.local", "role": role}
            return _Cursor([])
        if "DELETE FROM workspace_members WHERE workspace_id=%s AND user_id=%s" in q:
            removed = self.s.members.pop(str(params[1]), None) is not None
            return _Cursor([], rowcount=1 if removed else 0)
        return _Cursor([])


def _patch_state(monkeypatch, state: FakeState, runtime_srv=None):
    monkeypatch.setattr(infra, "pg", lambda readonly=False: FakePgConn(state))
    monkeypatch.setattr(pgstore, "require_role", lambda c, ws, u, role: None)
    monkeypatch.setattr(pgstore, "latest_snapshot", lambda c, ws: state.latest_snapshot())

    def _save_snap(c, ws, snap, passed):
        return state.add_snapshot(snap, passed)

    monkeypatch.setattr(pgstore, "save_snapshot", _save_snap)

    class _MemOverride:
        def list(self, ws, view=None):
            rows = list(state.overrides.values())
            if view is not None:
                rows = [r for r in rows if r["view_key"] == view]
            return copy.deepcopy(rows)

        def list_views(self, ws):
            return sorted({r["view_key"] for r in state.overrides.values()})

        def locked_views(self, ws):
            return sorted({r["view_key"] for r in state.overrides.values() if r.get("locked_by_user")})

        def get(self, ws, view, sel):
            row = state.overrides.get((view, sel))
            return copy.deepcopy(row) if row else None

        def put(self, row):
            state.overrides[(row["view_key"], row["component_selector"])] = copy.deepcopy(row)

        def delete(self, ws, view, sel):
            return state.overrides.pop((view, sel), None) is not None

    monkeypatch.setattr(pgstore, "PgOverrideStore", _MemOverride)

    def _add_collab(c, ws, role, mode, content, meta=None, run_id=None, user_id=None):
        mid = state.next_msg_id
        state.next_msg_id += 1
        row = {
            "msg_id": mid,
            "workspace_id": str(ws),
            "role": role,
            "mode": mode,
            "content": content,
            "meta_json": copy.deepcopy(meta or {}),
            "run_id": run_id,
            "created_by": user_id,
            "created_at": "2026-11-22T00:00:00Z",
        }
        state.collab_messages.append(row)
        return copy.deepcopy(row)

    monkeypatch.setattr(pgstore, "add_collab_message", _add_collab)
    monkeypatch.setattr(pgstore, "list_collab_messages", lambda c, ws, limit=120: copy.deepcopy(state.collab_messages[-limit:]))
    monkeypatch.setattr(pgstore, "clear_collab_messages", lambda c, ws: state.collab_messages.clear())

    monkeypatch.setattr(vm_service, "canvas_for", lambda ws: state.canvas_id)
    monkeypatch.setattr(vm_service, "enabled", lambda: bool(runtime_srv))
    monkeypatch.setattr(vm_service, "budget_gate", lambda ws: (lambda est=0.0: None))
    monkeypatch.setattr(vm_service, "record_usage", lambda *a, **k: None)

    def _load_settings(ws, override=None):
        from irade_agent.agdd.cli_agents import AgentSettings
        return AgentSettings.from_row({**state.settings, **(override or {})})

    def _save_settings(ws, data, user_id=None):
        from irade_agent.agdd.cli_agents import AgentSettings
        merged = {**state.settings, **(data or {})}
        s = AgentSettings.from_row(merged)
        state.settings = {
            "cli": s.cli,
            "model": s.model,
            "effort": s.effort,
            "session": s.session,
            "permission": s.permission,
            "instructions": s.instructions,
            "default_repo": s.default_repo,
            "ux_auto_canvas": s.ux_auto_canvas,
        }
        return s

    monkeypatch.setattr(vm_service, "load_settings", _load_settings)
    monkeypatch.setattr(vm_service, "save_settings", _save_settings)
    monkeypatch.setattr(vm_sync, "sync_workspace_context_async", lambda *a, **k: None)


def _sample_snapshot():
    return copy.deepcopy(PLATFORM_SNAPSHOT)


def _http_req(base_url: str, method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "authorization": f"Bearer {os.environ['API_TOKEN']}",
            "x-user-id": USER_ID,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            ctype = r.headers.get("content-type", "")
            return r.status, ctype, (json.loads(raw) if "application/json" in ctype else raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        return e.code, e.headers.get("content-type", ""), payload


def test_spec_store_readonly_queries_fts_and_write_denial(monkeypatch):
    ws = str(uuid.uuid4())
    state = FakeState(ws_id=ws)
    state.add_snapshot(_sample_snapshot(), passed=True)
    _patch_state(monkeypatch, state)
    spec_store.invalidate(ws)

    joined = spec_store.query_readonly(
        None,
        ws,
        "SELECT r.req_id, a.arch_id FROM srs_requirements r "
        "JOIN spec_traceability t ON t.source_id=r.req_id "
        "JOIN arch_definitions a ON a.arch_id=t.target_id "
        "WHERE r.adoption_status='ACCEPT_NOW' AND r.req_id='REQ-001'",
    )
    assert joined["columns"] == ["req_id", "arch_id"]
    assert joined["rows"] == [{"req_id": "REQ-001", "arch_id": "ARC-002"}]

    hits = spec_store.search_fts(None, ws, "Orchestrator")
    assert any(h["spec_id"] == "ARC-002" for h in hits)

    sm = spec_store.summary(None, ws)
    assert sm["accept"] == {"srs": 3, "arc": 3, "sch": 3}
    assert sm["total"] == {"srs": 6, "arc": 5, "sch": 4}
    assert sm["trace_count"] == 11

    for blocked_sql in (
        "DELETE FROM srs_requirements",
        "DROP TABLE srs_requirements",
        "PRAGMA user_version = 1",
    ):
        with pytest.raises(ValueError):
            spec_store.query_readonly(None, ws, blocked_sql)

    with pytest.raises(sqlite3.DatabaseError):
        spec_store.query_readonly(None, ws, "WITH x AS (SELECT 1) DELETE FROM srs_requirements")

    spec_store.invalidate(ws)


def test_vm_sync_and_agdd_spec_cli_in_vm(tmp_path, monkeypatch):
    ws = str(uuid.uuid4())
    state = FakeState(ws_id=ws, canvas_id=7)
    state.add_snapshot(_sample_snapshot(), passed=True)
    state.overrides[("home", "#hero")] = {
        "view_key": "home",
        "component_selector": "#hero",
        "config_json": {"layout": {"x": 40, "y": 40}, "size": {"w": 420, "h": 180}},
        "locked_by_user": True,
        "updated_by": "human",
    }
    _patch_state(monkeypatch, state)
    vm_sync._VM_SCRIPTS_READY.clear()

    with _fake_rt(tmp_path, monkeypatch, 7) as (rt, vm_home):
        _patch_state(monkeypatch, state, runtime_srv=rt)

        sh = vm.VmShell(RuntimeClient(), 7, "agdd-test", home=str(vm_home))
        try:
            sync_cell_res = vm_sync.sync_cells(
                ws,
                [{"id": "c1", "kind": "python", "title": "hello cell", "text": "print(42)\n"}],
                shell=sh,
            )
            assert sync_cell_res["ok"] is True
            assert sync_cell_res["count"] == 1

            info = vm_sync.sync_workspace_context(ws, shell=sh)
            assert info["ok"] is True
            assert info["db_path"].startswith("/dev/shm/agdd-spec-")
            assert os.path.islink(os.path.join(sh.home, ".agdd", "spec.db"))

            ctx_md = (Path(sh.home) / ".agdd" / "WORKSPACE_CONTEXT.md").read_text(encoding="utf-8")
            assert "Always cite SRS IDs before editing." in ctx_md
            assert "ux-overrides.json" in ctx_md
            ux_disk = (Path(sh.home) / ".agdd" / "ux-overrides.json").read_text(encoding="utf-8")
            assert "#hero" in ux_disk

            cli = f"{sh.home}/.local/bin/agdd-spec"
            st = json.loads(sh.run_inline(f"{cli} status", timeout=30).output)
            assert st["in_memory_ram"] is True
            assert st["counts"]["srs_accept"] == 3
            assert st["counts"]["cells"] == 1
            assert st["counts"]["ux_overrides"] == 1

            sc = json.loads(sh.run_inline(f"{cli} scope GRAPH_ENGINE", timeout=30).output)
            assert [r["req_id"] for r in sc["requirements"]] == ["REQ-001"]
            assert [a["arch_id"] for a in sc["architecture"]] == ["ARC-002"]

            sql_out = json.loads(sh.run_inline(f"{cli} sql \"SELECT req_id, domain FROM srs_requirements ORDER BY req_id\"", timeout=30).output)
            assert len(sql_out["rows"]) == 6

            search_out = json.loads(sh.run_inline(f"{cli} search Orchestrator", timeout=30).output)
            assert any(item["spec_id"] == "ARC-002" for item in search_out)

            ux_out = json.loads(sh.run_inline(f"{cli} ux home", timeout=30).output)
            assert ux_out[0]["component_selector"] == "#hero"

            files_out = json.loads(sh.run_inline(f"{cli} files", timeout=30).output)
            assert len(files_out) == 1 and files_out[0]["exists"] is True

            req_res = sh.run_inline(
                f"{cli} request-ux --view home --prompt 'Arrange cards' "
                "--elements '[{\"selector\":\"#hero\",\"kind\":\"hero\",\"label\":\"Hero\",\"x\":40,\"y\":40,\"w\":400,\"h\":160}]'",
                timeout=30,
            )
            assert req_res.exit_code == 0
            assert "<<AGDD_UX_REQUEST:" in req_res.output
            parsed_cli = ux_workbench.parse_ux_request(req_res.output)
            assert parsed_cli is not None and parsed_cli["elements"][0]["selector"] == "#hero"

            pending = vm_sync.read_pending_vm_ux_request(ws, shell=sh)
            assert pending is not None and pending["view_key"] == "home" and pending["prompt"] == "Arrange cards"
        finally:
            sh.close()


def test_http_ide_cells_specs_ux_and_collab(tmp_path, monkeypatch):
    ws = str(uuid.uuid4())
    state = FakeState(ws_id=ws, canvas_id=11)
    state.add_snapshot(_sample_snapshot(), passed=True)
    _patch_state(monkeypatch, state)
    vm_sync._VM_SCRIPTS_READY.clear()
    api._IDE_READY.clear()

    monkeypatch.setenv("API_TOKEN", "test-api-token")

    with _fake_rt(tmp_path, monkeypatch, 11) as (rt, vm_home):
        local_bin = vm_home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        claude_stub = local_bin / "claude"
        claude_stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, subprocess\n"
            "with open('app.py', 'w', encoding='utf-8') as f:\n"
            "    f.write('def greet():\\n    return \"hi\"\\n')\n"
            "cli = pathlib.Path(os.environ['HOME']) / '.local' / 'bin' / 'agdd-spec'\n"
            "if cli.exists():\n"
            "    subprocess.run([str(cli), 'request-ux', '--view', 'home', '--prompt', 'Place sidebar & card',\n"
            "                    '--elements', '[{\"selector\":\"#card\",\"kind\":\"card\",\"label\":\"Card\",\"x\":60,\"y\":80,\"w\":320,\"h\":160}]'], check=False)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 'sess-stub-1',\n"
            "                  'result': 'Created app.py and requested 2D layout.',\n"
            "                  'usage': {'input_tokens': 12, 'output_tokens': 8}, 'total_cost_usd': 0.001}))\n",
            encoding="utf-8",
        )
        claude_stub.chmod(0o755)
        _patch_state(monkeypatch, state, runtime_srv=rt)
        monkeypatch.setattr(router, "route_request", lambda req, llm: router.RouteDecision("FULL_AGDD", ["test route"]))

        srv = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            # Static assets
            for asset, expected_ctype in (("ide.js", "javascript"), ("collab.js", "javascript"), ("ux_canvas.js", "javascript"), ("ux_canvas.css", "text/css")):
                code, ctype, txt = _http_req(base, "GET", f"/static/{asset}")
                assert code == 200 and expected_ctype in ctype and len(txt) > 100
            code, ctype, html = _http_req(base, "GET", "/console")
            assert code == 200 and "text/html" in ctype
            assert "static/ux_canvas.css" in html and "static/ux_canvas.js" in html

            # Editable IDE + real-file Cells sync
            code, _, res = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos", {"repo": "workspace"})
            assert code == 200 and res["repo"] == "workspace"

            code, _, sync_res = _http_req(base, "POST", f"/workspaces/{ws}/vm/cells/sync", {
                "cells": [
                    {"id": "cell1", "kind": "python", "title": "demo", "text": "x = 1\n", "frameOrder": 1},
                    {"id": "cell2", "kind": "html", "title": "view", "text": "<h1>Hi</h1>\n", "frameOrder": 2},
                ],
                "force": True,
            })
            assert code == 200 and sync_res["count"] == 2
            cell1_path = sync_res["manifest"]["cell1"]["path"]
            assert cell1_path.startswith("cells/") and cell1_path.endswith(".py")

            # Edit cell on disk via IDE + add untracked cell file -> VM cells read picks both up
            code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/ide/repos/workspace/files/{cell1_path}", {"content": "x = 99\n"})
            assert code == 200
            code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/ide/repos/workspace/files/cells/agent_added.py", {"content": "y = 2\n"})
            assert code == 200
            code, _, read_res = _http_req(base, "GET", f"/workspaces/{ws}/vm/cells")
            assert code == 200
            c1_back = next(c for c in read_res["cells"] if c["id"] == "cell1")
            assert c1_back["text"] == "x = 99\n" and c1_back["changed"] is True
            assert any(u["path"] == "cells/agent_added.py" for u in read_res["untracked"])

            # mkdir, write, run, rename, delete + path safety
            code, _, _ = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos/workspace/mkdir", {"path": "src/sub"})
            assert code == 200
            code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/ide/repos/workspace/files/src/sub/demo.py", {"content": "print(2 + 3)\n"})
            assert code == 200
            code, _, run_res = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos/workspace/run", {"path": "src/sub/demo.py"})
            assert code == 200 and run_res["exit_code"] == 0 and run_res["stdout"].strip() == "5"
            code, _, _ = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos/workspace/rename", {"from": "src/sub/demo.py", "to": "src/sub/renamed.py"})
            assert code == 200
            code, _, _ = _http_req(base, "DELETE", f"/workspaces/{ws}/ide/repos/workspace/files/src/sub/renamed.py")
            assert code == 200

            for bad_path in ("../secret.txt", ".git/config"):
                code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/ide/repos/workspace/files/{bad_path}", {"content": "bad"})
                assert code == 400

            # Agent tuning settings
            code, _, sres = _http_req(base, "PUT", f"/workspaces/{ws}/agent-settings", {
                "cli": "claude",
                "model": "sonnet",
                "effort": "high",
                "instructions": "Tuned instruction line",
                "default_repo": "workspace",
                "ux_auto_canvas": True,
            })
            assert code == 200 and sres["instructions"] == "Tuned instruction line" and sres["ux_auto_canvas"] is True

            # Spec SQL endpoint
            code, _, sq = _http_req(base, "POST", f"/workspaces/{ws}/specs/query", {"sql": "SELECT req_id FROM srs_requirements WHERE domain='GRAPH_ENGINE'"})
            assert code == 200 and sq["rows"] == [{"req_id": "REQ-001"}]

            # Collab state + CLI launch
            code, _, st_res = _http_req(base, "GET", f"/workspaces/{ws}/collab/state")
            assert code == 200 and st_res["vm_enabled"] is True and st_res["spec_summary"]["accept"]["srs"] == 3
            code, _, ln_res = _http_req(base, "POST", f"/workspaces/{ws}/collab/cli-launch", {})
            assert code == 200 and ln_res["cli"] == "claude" and ln_res["db_path"].startswith("/dev/shm/")

            # Collab AGDD run mode
            code, _, agdd_res = _http_req(base, "POST", f"/workspaces/{ws}/collab/send", {
                "mode": "agdd",
                "message": "Implement auth gateway",
            })
            assert code == 200 and agdd_res["mode"] == "agdd" and agdd_res["blocked"] is False
            run_id = agdd_res["run_id"]
            assert run_id in state.runs and len(state.jobs) >= 1

            # Collab pure CLI chat turn running inside FakeRuntime
            code, _, cli_res = _http_req(base, "POST", f"/workspaces/{ws}/collab/send", {
                "mode": "cli_chat",
                "message": "Create app.py and ask UX canvas",
            })
            assert code == 200
            assert cli_res["mode"] == "cli_chat" and cli_res["ok"] is True
            assert cli_res["session_id"] == "sess-stub-1"
            assert "app.py" in cli_res["changed_files"]
            assert cli_res["ux_request"] is not None and cli_res["ux_request"]["view_key"] == "home"

            # UX pending-request + 2D submit-to-agent resuming WAITING_HITL run
            code, _, pend_res = _http_req(base, "GET", f"/workspaces/{ws}/ux/pending-request")
            assert code == 200 and len(pend_res["pending"]) >= 1

            state.runs[run_id]["status"] = "WAITING_HITL"
            state.runs[run_id]["current_node"] = "dev"
            state.hitl[1] = {
                "decision_id": 1,
                "run_id": run_id,
                "status": "PENDING",
                "reason_code": "UX_VISUAL_REQUEST",
                "decision_card_json": {"ux_request": cli_res["ux_request"]},
            }
            code, _, sub_res = _http_req(base, "POST", f"/workspaces/{ws}/ux/home/submit-to-agent", {
                "viewport": {"width": 1200, "height": 720},
                "note": "Keep hero locked above card",
                "elements": {
                    "#hero": {
                        "locked": True,
                        "config": {
                            "layout": {"x": 40, "y": 40, "z": 1},
                            "size": {"w": 600, "h": 160},
                            "variant": "split",
                            "tokens": {"accent": "#2563eb", "radius": 14, "bg": ""},
                            "spacing": {"padding": 16, "gap": 12},
                            "visibility": "visible",
                            "props": {"kind": "hero", "label": "Hero", "connects_to": ["#card"]},
                        },
                    },
                    "#card": {
                        "locked": False,
                        "config": {
                            "layout": {"x": 40, "y": 240, "z": 2},
                            "size": {"w": 600, "h": 220},
                            "variant": "metric",
                            "tokens": {"accent": "#2f5d50", "radius": 12, "bg": ""},
                            "props": {"kind": "card", "label": "Card"},
                        },
                    },
                },
            })
            assert code == 200 and sub_res["resumed_run_id"] == run_id
            assert state.runs[run_id]["status"] == "RUNNING"
            assert state.runs[run_id]["handoff_json"]["ux_layout_response"]["summary"]["viewport"]["width"] == 1200
            assert state.hitl[1]["status"] == "APPROVED"

            code, _, full_ux = _http_req(base, "GET", f"/workspaces/{ws}/ux/home?full=1")
            assert code == 200 and full_ux["summary"]["locked_selectors"] == ["#hero"]
            by_sel = {e["selector"]: e for e in full_ux["summary"]["elements"]}
            assert by_sel["#hero"]["variant"] == "split"
            assert by_sel["#hero"]["tokens"]["accent"] == "#2563eb"
            assert by_sel["#card"]["variant"] == "metric"

            # :canvas metadata + table props round-trip (and :canvas is never locked for audit_patch)
            tbl_data = {
                "columns": ["코드", "모듈", "상태"],
                "rows": [["A-1", "인증", "완료"], ["A-2", "테이블", "진행중"]],
                "badgeCol": 2,
                "showToolbar": True,
                "showHeader": True,
            }
            code, _, canvas_sub = _http_req(base, "POST", f"/workspaces/{ws}/ux/home/submit-to-agent", {
                "viewport": {"width": 1440, "height": 900},
                "launch_run": False,
                "elements": {
                    ":canvas": {
                        "locked": True,
                        "config": {
                            "size": {"w": 1440, "h": 900},
                            "layout": {"columns": 12},
                            "spacing": {"padding": 24, "gap": 16},
                            "tokens": {"bg": "#ffffff", "accent": "#2563eb", "radius": 12},
                            "props": {"kind": "canvas", "gridSize": 16, "showColumns": True, "theme": "dark"},
                        },
                    },
                    "#data-tbl": {
                        "locked": True,
                        "config": {
                            "layout": {"x": 48, "y": 64, "z": 1},
                            "size": {"w": 640, "h": 280},
                            "variant": "striped",
                            "props": {"kind": "table", "label": "작업 목록", "table": tbl_data},
                        },
                    },
                },
            })
            assert code == 200 and canvas_sub["ok"] is True
            code, _, full_after = _http_req(base, "GET", f"/workspaces/{ws}/ux/home?full=1")
            assert code == 200
            by_row = {r["component_selector"]: r for r in full_after["items"]}
            assert by_row[":canvas"]["locked_by_user"] is False
            assert ":canvas" not in full_after["summary"]["locked_selectors"]
            assert full_after["summary"]["canvas"]["props"]["gridSize"] == 16
            by_el2 = {e["selector"]: e for e in full_after["summary"]["elements"]}
            assert by_el2["#data-tbl"]["props"]["table"]["columns"] == ["코드", "모듈", "상태"]
            assert by_el2["#data-tbl"]["props"]["table"]["rows"] == [["A-1", "인증", "완료"], ["A-2", "테이블", "진행중"]]
        finally:
            srv.shutdown()
            srv.server_close()


def test_2d_spatial_summary_worker_dev_pause_verify_exit5_and_audit_lock(tmp_path, monkeypatch):
    # 1) Spatial relations in summarize_2d_layout
    summary = ux_workbench.summarize_2d_layout(
        {
            "#shell": {"layout": {"x": 20, "y": 20}, "size": {"w": 1000, "h": 640}},
            "#nav": {"layout": {"x": 40, "y": 40}, "size": {"w": 200, "h": 560}, "props": {"connects_to": ["#main"]}},
            "#main": {"layout": {"x": 280, "y": 40}, "size": {"w": 700, "h": 260}},
            "#footer": {"layout": {"x": 280, "y": 340}, "size": {"w": 700, "h": 120}},
        },
        locked_selectors={"#nav"},
        viewport={"width": 1200, "height": 720},
    )
    rels = {(r["type"], r["source"], r["target"]) for r in summary["spatial_relations"]}
    assert ("contained_in", "#nav", "#shell") in rels
    assert ("contained_in", "#main", "#shell") in rels
    assert ("left_of", "#nav", "#main") in rels
    assert ("above", "#main", "#footer") in rels
    assert summary["connections"] == [{"from": "#nav", "to": "#main"}]
    assert summary["locked_selectors"] == ["#nav"]

    # 1b) summarize_2d_layout handles :canvas metadata & preserves table/custom props
    summary_with_canvas = ux_workbench.summarize_2d_layout(
        {
            ":canvas": {
                "size": {"w": 1440, "h": 900},
                "layout": {"columns": 12},
                "spacing": {"padding": 24, "gap": 16},
                "tokens": {"bg": "#f8fafc", "accent": "#2563eb", "radius": 12},
                "props": {"kind": "canvas", "gridSize": 16, "showColumns": True, "theme": "light"},
            },
            "#tbl-1": {
                "layout": {"x": 32, "y": 32, "z": 1},
                "size": {"w": 600, "h": 280},
                "props": {
                    "kind": "table",
                    "label": "Users",
                    "table": {
                        "columns": ["ID", "Name", "Role"],
                        "rows": [["1", "Kim", "Admin"], ["2", "Lee", "Editor"]],
                        "badgeCol": 2,
                    },
                },
            },
        },
        locked_selectors={":canvas", "#tbl-1"},
        viewport=None,
    )
    assert summary_with_canvas["viewport"] == {"width": 1440, "height": 900}
    assert summary_with_canvas["locked_selectors"] == ["#tbl-1"]
    assert [e["selector"] for e in summary_with_canvas["elements"]] == ["#tbl-1"]
    assert summary_with_canvas["elements"][0]["props"]["table"]["columns"] == ["ID", "Name", "Role"]
    assert summary_with_canvas["canvas"]["props"]["gridSize"] == 16
    assert not any(r["source"] == ":canvas" or r["target"] == ":canvas" for r in summary_with_canvas["spatial_relations"])

    # 2) worker_vm.node_dev pause on UX_VISUAL_REQUEST + node_verify pytest exit 5 + node_audit locked UX diff
    ws = str(uuid.uuid4())
    state = FakeState(ws_id=ws, canvas_id=19)
    state.add_snapshot(_sample_snapshot(), passed=True)
    state.overrides[("home", "#nav")] = {
        "view_key": "home",
        "component_selector": "#nav",
        "config_json": {"layout": {"x": 40, "y": 40}},
        "locked_by_user": True,
        "updated_by": "human",
    }
    _patch_state(monkeypatch, state)
    vm_sync._VM_SCRIPTS_READY.clear()

    with _fake_rt(tmp_path, monkeypatch, 19) as (rt, vm_home):
        local_bin = vm_home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        claude_stub = local_bin / "claude"
        claude_stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, subprocess\n"
            "cli = pathlib.Path(os.environ['HOME']) / '.local' / 'bin' / 'agdd-spec'\n"
            "subprocess.run([str(cli), 'request-ux', '--view', 'home', '--prompt', 'Confirm nav & main',\n"
            "                '--elements', '[{\"selector\":\"#nav\",\"kind\":\"sidebar\",\"label\":\"Nav\",\"x\":40,\"y\":40,\"w\":200,\"h\":400}]'], check=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 'dev-s1',\n"
            "                  'result': 'Need 2D canvas input', 'usage': {'input_tokens': 5, 'output_tokens': 4}, 'total_cost_usd': 0.0}))\n",
            encoding="utf-8",
        )
        claude_stub.chmod(0o755)
        _patch_state(monkeypatch, state, runtime_srv=rt)

        ctx_dev = types.SimpleNamespace(
            ws=ws,
            run={"run_id": "run-1"},
            run_id="run-1",
            node={"name": "dev", "kind": "DEV"},
            handoff={"request": "Build home UI", "repo_key": "workspace"},
            hitl_requests=[],
            scope={"requirements": [{"req_id": "REQ-001", "title": "Auth login", "description": "..."}], "architecture": [], "schemas": []},
            ux_bindings={"home": {"#nav": {"layout": {"x": 40, "y": 40}}}},
            attempt=1,
        )
        v = worker_vm.VmCtx(ctx_dev)
        v.repo = f"{v.shell.home}/repos/workspace"
        try:
            v.shell.run(
                f"mkdir -p {v.repo} && git -C {v.repo} init -q && git -C {v.repo} config user.email t@example.com && git -C {v.repo} config user.name t",
                timeout=30,
            )
            v.shell.put_file(f"{v.repo}/README.md", "# empty repo\n")
            v.shell.run(f"cd {v.repo} && git add README.md && git commit -qm init", timeout=30)

            ok_dev, out_dev = worker_vm.node_dev(ctx_dev, v, lambda *a: None)
            assert ok_dev is True
            assert out_dev["pause_at_current"] is True
            assert out_dev["ux_request"]["view_key"] == "home"
            assert ctx_dev.hitl_requests[0]["reason_code"] == "UX_VISUAL_REQUEST"

            # node_verify on repo without tests: pytest exit code 5 treated as OK
            ctx_ver = types.SimpleNamespace(
                ws=ws,
                run_id="run-1",
                node={"name": "verify", "kind": "VERIFY", "command": [sys.executable, "-m", "pytest", "-q"]},
                handoff={},
            )
            ok_ver, out_ver = worker_vm.node_verify(ctx_ver, v)
            assert ok_ver is True
            assert "no test files collected" in out_ver["verify_summary"]

            # node_audit blocks patch touching locked selector (#nav) across store.locked_views(ws)
            ctx_aud = types.SimpleNamespace(
                ws=ws,
                run_id="run-1",
                node={"name": "audit", "kind": "AUDIT"},
                handoff={
                    "last_patch": "diff --git a/ui.css b/ui.css\n--- a/ui.css\n+++ b/ui.css\n-#nav { width: 200px; }\n+#nav { width: 999px; }\n",
                    "changed_files": [],
                },
            )
            ok_aud, out_aud = worker_vm.node_audit(ctx_aud, v)
            assert ok_aud is False
            assert "#nav" in out_aud["feedback"]
        finally:
            v.shell.close()


def test_vm_service_shell_and_canvas_for_no_deadlock(monkeypatch):
    ws = str(uuid.uuid4())
    vm_service._shells.clear()
    vm_service._canvas_cache.clear()

    class _VmPg:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def commit(self):
            return None

        def execute(self, sql, params=()):
            if "SELECT canvas_id FROM workspace_vms" in sql:
                return _Cursor([{"canvas_id": 4242}])
            return _Cursor()

    monkeypatch.setattr(infra, "pg", lambda readonly=False: _VmPg())

    result: dict = {}
    errors: list[BaseException] = []

    def _run():
        try:
            sh1 = vm_service.shell(ws, "agdd-ide")
            cid = vm_service.canvas_for(ws)
            sh2 = vm_service.shell(ws, "agdd-ide")
            result.update({"sh1": sh1, "sh2": sh2, "cid": cid})
        except BaseException as exc:
            errors.append(exc)

    try:
        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=3.0)
        assert not th.is_alive(), "vm_service.shell() deadlocked while resolving canvas_for()"
        assert not errors, f"unexpected error: {errors[0]!r}"
        assert result["cid"] == 4242
        assert result["sh1"].canvas_id == 4242
        assert result["sh1"] is result["sh2"]
    finally:
        vm_service._shells.clear()
        vm_service._canvas_cache.clear()


def test_vm_service_maybe_expand_disk_and_idempotence(monkeypatch):
    ws = str(uuid.uuid4())
    vm_service._shells.clear()
    vm_service._canvas_cache.clear()
    vm_service._expanded_cids.clear()

    monkeypatch.setattr(vm_service, "canvas_for", lambda w: 9901)
    set_calls = []

    class _FakeRC:
        def settings(self, cid):
            return {"revision": 3, "diskGiB": 6, "diskSizesGiB": [6, 12, 24]}

        def set_disk(self, cid, size_gib, revision):
            set_calls.append((cid, size_gib, revision))
            return {"ok": True, "revision": revision + 1}

    monkeypatch.setattr(vm_service, "RuntimeClient", _FakeRC)
    cached = vm.VmShell(vm.RuntimeClient(), 9901, "agdd-ide")
    cached.opened = True
    vm_service._shells[(ws, "agdd-ide")] = cached

    try:
        assert vm_service.maybe_expand_disk(ws) is True
        assert set_calls == [(9901, 12, 3)]
        assert (ws, "agdd-ide") not in vm_service._shells
        assert cached.opened is False
        # Idempotent once expanded
        assert vm_service.maybe_expand_disk(ws) is False
        assert len(set_calls) == 1
    finally:
        vm_service._shells.clear()
        vm_service._canvas_cache.clear()
        vm_service._expanded_cids.clear()


def test_audit_fixes_api_and_vm_sync(tmp_path, monkeypatch):
    ws = str(uuid.uuid4())
    state = FakeState(ws_id=ws, canvas_id=13)
    state.add_snapshot(_sample_snapshot(), passed=True)
    vm_sync._VM_SCRIPTS_READY.clear()
    api._IDE_READY.clear()
    api._CLI_CACHE.clear()

    monkeypatch.setenv("API_TOKEN", "test-api-token")
    invalidated: list[str] = []
    monkeypatch.setattr(infra, "cache_invalidate", lambda *keys: invalidated.extend(str(k) for k in keys))

    with _fake_rt(tmp_path, monkeypatch, 13) as (rt, vm_home):
        _patch_state(monkeypatch, state, runtime_srv=rt)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            # 1. Malformed JSON returns 400 JSON (not masked by broad Exception handler)
            bad_req = urllib.request.Request(
                f"{base}/workspaces/{ws}/collab/send",
                data=b"{bad-json",
                method="POST",
                headers={
                    "authorization": f"Bearer {os.environ['API_TOKEN']}",
                    "x-user-id": USER_ID,
                    "content-type": "application/json",
                },
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(bad_req, timeout=10)
            assert exc_info.value.code == 400
            bad_body = json.loads(exc_info.value.read().decode("utf-8"))
            assert "error" in bad_body

            # 2. Budget validation (rejects empty/negative/bool/NaN) + dash cache invalidation
            for bad_limit in ("", -5, True, "abc"):
                code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/budget", {"limit_usd": bad_limit})
                assert code == 422
            code, _, b_res = _http_req(base, "PUT", f"/workspaces/{ws}/budget", {"limit_usd": 42.5, "period_ym": "2026-11"})
            assert code == 200 and b_res["ok"] is True
            assert state.budgets[(ws, "2026-11")]["limit_usd"] == 42.5
            assert f"agdd:dash:{ws}" in invalidated

            # 3. CLI cache ?refresh=1 bypasses _CLI_CACHE
            stale = {"claude": {"installed": False, "version": "stale"}}
            api._CLI_CACHE[ws] = (time.monotonic(), stale)
            code, _, cached_res = _http_req(base, "GET", f"/workspaces/{ws}/vm/clis")
            assert code == 200 and cached_res == stale
            code, _, fresh_res = _http_req(base, "GET", f"/workspaces/{ws}/vm/clis?refresh=1")
            assert code == 200 and fresh_res != stale and "claude" in fresh_res
            assert api._CLI_CACHE[ws][1] == fresh_res

            # 4. Run cancel halts WAITING_HITL run, marks pending jobs/HITL closed, invalidates caches
            run_cancel_id = str(uuid.uuid4())
            state.runs[run_cancel_id] = {
                "run_id": run_cancel_id,
                "graph_id": str(uuid.uuid4()),
                "graph_version": 1,
                "status": "WAITING_HITL",
                "current_node": "dev",
                "handoff_json": {},
                "version": 1,
            }
            state.jobs.append({"run_id": run_cancel_id, "node_name": "dev", "status": "PENDING"})
            state.hitl[1] = {"decision_id": 1, "run_id": run_cancel_id, "status": "PENDING", "reversibility": "IRREVERSIBLE"}
            invalidated.clear()
            code, _, c_res = _http_req(base, "POST", f"/runs/{run_cancel_id}/cancel", {})
            assert code == 200 and c_res == {"ok": True, "halted": True}
            assert state.runs[run_cancel_id]["status"] == "HALTED"
            assert state.jobs[-1]["status"] == "FAILED"
            assert state.hitl[1]["status"] == "REJECTED"
            assert f"agdd:run:{run_cancel_id}" in invalidated and f"agdd:dash:{ws}" in invalidated

            # 5. HITL decide persists chosen_option and comment into resolution & handoff
            run_hitl_id = str(uuid.uuid4())
            state.runs[run_hitl_id] = {
                "run_id": run_hitl_id,
                "graph_id": str(uuid.uuid4()),
                "graph_version": 1,
                "status": "WAITING_HITL",
                "current_node": "dev",
                "handoff_json": {},
                "version": 1,
            }
            state.hitl[2] = {
                "decision_id": 2,
                "run_id": run_hitl_id,
                "status": "PENDING",
                "reversibility": "IRREVERSIBLE",
                "decision_card_json": {"summary": "Choose strategy"},
            }
            code, _, d_res = _http_req(base, "POST", "/hitl/2/decide", {
                "status": "APPROVED",
                "chosen_option": 1,
                "comment": "Use option B",
            })
            expected_res = {"status": "APPROVED", "chosen_option": 1, "comment": "Use option B"}
            assert code == 200 and d_res["resolution"] == expected_res
            assert state.hitl[2]["status"] == "APPROVED"
            assert state.hitl[2]["decision_card_json"]["resolution"] == expected_res
            assert state.runs[run_hitl_id]["status"] == "RUNNING"
            assert state.runs[run_hitl_id]["handoff_json"]["hitl_resolution"] == expected_res

            # 6. IDE git commit & revert to HEAD
            code, _, _ = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos", {"repo": "workspace"})
            assert code == 200
            code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/ide/repos/workspace/files/app.py", {"content": "v1\n"})
            assert code == 200
            code, _, com_res = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos/workspace/commit", {"message": "init v1"})
            assert code == 200 and com_res["ok"] is True and len(com_res["sha"]) >= 7
            code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/ide/repos/workspace/files/app.py", {"content": "v2\n"})
            assert code == 200
            code, _, rev_res = _http_req(base, "POST", f"/workspaces/{ws}/ide/repos/workspace/revert", {"path": "app.py"})
            assert code == 200 and rev_res["ok"] is True
            code, _, f_res = _http_req(base, "GET", f"/workspaces/{ws}/ide/repos/workspace/files/app.py")
            assert code == 200 and f_res["content"] == "v1\n"

            # 7. VM cell sync adopts explicit cells/... path directly without duplicates
            code, _, sync_res = _http_req(base, "POST", f"/workspaces/{ws}/vm/cells/sync", {
                "cells": [{
                    "id": "c_new",
                    "kind": "python",
                    "title": "Agent Cell",
                    "path": "cells/agent_added.py",
                    "text": "a = 1\n",
                    "frameOrder": 1,
                }],
                "delete_missing": True,
                "force": True,
            })
            assert code == 200 and sync_res["manifest"]["c_new"]["path"] == "cells/agent_added.py"
            cells_files = sorted(
                p.name
                for p in (vm_home / "repos" / "workspace" / "cells").iterdir()
                if not p.name.startswith(".")
            )
            assert cells_files == ["agent_added.py"]

            # 8a. Research saves history + maps URLError cleanly to 502 JSON
            monkeypatch.setenv("RESEARCH_URL", "http://research-mock")
            orig_urlopen = urllib.request.urlopen
            research_fail = {"down": False}

            class _FakeResp:
                def __init__(self, payload: dict):
                    self._raw = json.dumps(payload).encode("utf-8")

                def read(self):
                    return self._raw

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def _mock_urlopen(req, *a, **k):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if url.startswith("http://research-mock"):
                    if research_fail["down"]:
                        raise urllib.error.URLError("down")
                    return _FakeResp({
                        "research_id": 77,
                        "query": "test q",
                        "reused": False,
                        "insight_brief_md": "# Brief",
                        "errors": [],
                    })
                return orig_urlopen(req, *a, **k)

            monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)
            code, _, r_res = _http_req(base, "POST", f"/workspaces/{ws}/research", {"problem": "Check caching"})
            assert code == 200 and r_res["research_id"] == 77
            code, _, hist_res = _http_req(base, "GET", f"/workspaces/{ws}/research/history")
            assert code == 200 and len(hist_res["items"]) == 1
            assert hist_res["items"][0]["research_id"] == 77 and hist_res["items"][0]["problem"] == "Check caching"

            research_fail["down"] = True
            code, _, err_res = _http_req(base, "POST", f"/workspaces/{ws}/research", {"problem": "Should 502"})
            assert code == 502 and "error" in err_res

            # 8b. Workspace members CRUD + 409 last-OWNER guard
            code, _, m_list = _http_req(base, "GET", f"/workspaces/{ws}/members")
            assert code == 200 and len(m_list["members"]) == 1 and m_list["members"][0]["user_id"] == USER_ID
            code, _, _ = _http_req(base, "DELETE", f"/workspaces/{ws}/members/{USER_ID}")
            assert code == 409

            uid2 = str(uuid.uuid4())
            code, _, _ = _http_req(base, "PUT", f"/workspaces/{ws}/members", {"user_id": uid2, "role": "EDITOR"})
            assert code == 200
            code, _, m_list2 = _http_req(base, "GET", f"/workspaces/{ws}/members")
            assert code == 200 and {m["user_id"] for m in m_list2["members"]} == {USER_ID, uid2}
            code, _, del_res = _http_req(base, "DELETE", f"/workspaces/{ws}/members/{uid2}")
            assert code == 200 and del_res["deleted"] is True
            code, _, m_list3 = _http_req(base, "GET", f"/workspaces/{ws}/members")
            assert code == 200 and [m["user_id"] for m in m_list3["members"]] == [USER_ID]
        finally:
            srv.shutdown()
            srv.server_close()
            vm_sync._VM_SCRIPTS_READY.clear()
            api._IDE_READY.clear()
            api._CLI_CACHE.clear()



def test_vendor_urls_localized_only_for_bundled_packages(tmp_path, monkeypatch):
    from irade_agent.agdd import api as agdd_api
    monkeypatch.setenv("AGDD_VENDOR_DIR", str(tmp_path))
    html = (b'<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>'
            b'const MONACO_BASE = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min";')
    assert agdd_api.localize_vendor_urls(html) == html  # nothing fetched -> CDN stays

    pkg = tmp_path / "@xterm/xterm@5.5.0"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "lib/xterm.js").write_text("// xterm")
    (pkg / ".complete").write_text("ok")
    out = agdd_api.localize_vendor_urls(html)
    assert b'src="static/vendor/@xterm/xterm@5.5.0/lib/xterm.js"' in out
    assert b"https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min" in out

    assert agdd_api._vendor_file("@xterm/xterm@5.5.0/lib/xterm.js") == (pkg / "lib/xterm.js").resolve()
    for bad in ("../secret.js", "@xterm/xterm@5.5.0/../../x.js", "@xterm/xterm@5.5.0/.complete", "@xterm//x.js"):
        assert agdd_api._vendor_file(bad) is None
