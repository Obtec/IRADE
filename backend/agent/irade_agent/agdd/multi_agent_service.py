"""Multi-agent teams: persistence (agent_teams / agent_team_runs / agent_team_events) and live execution.

A team run executes in a background thread of agdd-api. Each agent node gets its own VM PTY
(``agdd-ma-<node>``) so independent agents really run in parallel inside the workspace VM; code-mode agents
share one repo working tree and are therefore serialized per workspace. Without a VM, think-mode agents
fall back to the LLM gateway and code-mode agents fail with a clear error.
"""
from __future__ import annotations

import json
import threading
import time

from psycopg.types.json import Jsonb

from . import infra, multi_agent, pgstore, ux_workbench, vm_service
from .multi_agent import TeamGraphError, TeamRun, validate_team_graph

MAX_ACTIVE_PER_WORKSPACE = 2
_ACTIVE: dict[str, threading.Event] = {}          # team_run_id -> cancel flag
_ACTIVE_WS: dict[str, set[str]] = {}
_CODE_LOCKS: dict[str, threading.Lock] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- teams CRUD
def list_teams(ws) -> list[dict]:
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT team_id, name, graph_json, updated_at FROM agent_teams WHERE workspace_id=%s "
                         "ORDER BY updated_at DESC", (ws,)).fetchall()


def save_team(ws, team_id, name: str, graph: dict, user) -> dict:
    name = str(name or "").strip()[:80] or "새 팀"
    clean = validate_team_graph(graph)
    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        if team_id:
            row = c.execute("UPDATE agent_teams SET name=%s, graph_json=%s, updated_by=%s, updated_at=now() "
                            "WHERE team_id=%s AND workspace_id=%s RETURNING team_id, name, graph_json, updated_at",
                            (name, Jsonb(clean), uid, team_id, ws)).fetchone()
            if row is None:
                raise LookupError("team not found")
        else:
            row = c.execute("INSERT INTO agent_teams (workspace_id, name, graph_json, updated_by) VALUES (%s,%s,%s,%s) "
                            "RETURNING team_id, name, graph_json, updated_at", (ws, name, Jsonb(clean), uid)).fetchone()
        c.commit()
    return row


def delete_team(ws, team_id) -> bool:
    with infra.pg() as c:
        n = c.execute("DELETE FROM agent_teams WHERE team_id=%s AND workspace_id=%s", (team_id, ws)).rowcount
        c.commit()
    return bool(n)


# ---------------------------------------------------------------- data sources
def resolver(ws, repo_key: str):
    def resolve(node: dict) -> str:
        src, p = node["source"], node.get("params") or {}
        if src == "text":
            return p.get("text") or "(빈 텍스트)"
        if src == "spec_scope":
            with infra.pg(readonly=True) as c:
                eng = pgstore.hydrate_engine(c, ws)
            try:
                scope = eng.executable_scope(p.get("domain") or None)
            finally:
                eng.close()
            return json.dumps(scope, ensure_ascii=False, indent=1, default=str)
        if src == "ux_layout":
            store = pgstore.PgOverrideStore()
            view = p.get("view_key") or "home"
            bindings = ux_workbench.Workbench(store).export_bindings(ws, view)
            locked = [r["component_selector"] for r in store.list(ws, view) if r.get("locked_by_user")]
            return json.dumps(ux_workbench.summarize_2d_layout(bindings, locked), ensure_ascii=False, indent=1)
        if src == "collab_history":
            with infra.pg(readonly=True) as c:
                rows = pgstore.list_collab_messages(c, ws, limit=p.get("limit", 20))
            return "\n".join(f"[{r['role']}/{r['mode']}] {str(r['content'])[:800]}" for r in rows) or "(대화 없음)"
        if not vm_service.enabled():
            raise RuntimeError(f"{src} 데이터 소스는 워크스페이스 VM이 필요합니다")
        if src == "cells":
            from . import vm_sync
            return json.dumps(vm_sync.read_cells(ws), ensure_ascii=False, indent=1)[:multi_agent.MAX_INPUT_CHARS]
        if src == "repo_files":
            from .vm import RuntimeClient, VmShell, sh_quote
            repo = p.get("repo_key") or repo_key
            vm_service.repo_path(repo)
            sh = VmShell(RuntimeClient(), vm_service.canvas_for(ws), "agdd-ma-data")
            try:
                parts = []
                for path in p.get("paths") or []:
                    r = sh.run(f"head -c 8000 {sh_quote(f'{sh.home}/repos/{repo}/{path}')} 2>&1", timeout=60)
                    parts.append(f"### {path}\n```\n{r.output}\n```")
                return "\n\n".join(parts) or "(경로 없음)"
            finally:
                sh.close()
        raise RuntimeError(f"unknown data source {src}")
    return resolve


# ---------------------------------------------------------------- agent invocation
def invoker(ws, repo_key: str, run_id: str):
    use_vm = vm_service.enabled()
    base = vm_service.load_settings(ws) if use_vm else None

    def invoke(node: dict, prompt: str, ctx: dict):
        if not use_vm:
            if node["mode"] == "code":
                raise RuntimeError("code 모드 에이전트는 워크스페이스 VM이 필요합니다 (AGORA_RUNTIME_SOCKET)")
            llm = infra.cheap_model("multi_agent", ws, None, node["id"])
            return llm(prompt, multi_agent.THINK_SCHEMA)
        from .cli_agents import VmAgent
        from .vm import RuntimeClient, VmShell
        prompt += f"\n\n(team run {run_id})"  # keep the semantic cache from replaying an earlier run's answer
        override = {k: node[k] for k in ("cli", "model", "effort") if node.get(k)}
        if node["mode"] == "think":  # structured() uses the cheap slot: point it at this node's model/effort
            override["cheap_model"] = node.get("model") or base.model
            override["cheap_effort"] = node.get("effort") or base.effort
        settings = vm_service.load_settings(ws, override)
        sh = VmShell(RuntimeClient(), vm_service.canvas_for(ws), f"agdd-ma-{node['id']}"[:64])
        agent = VmAgent(sh, settings, vm_service.budget_gate(ws))
        t0 = time.time()
        try:
            if node["mode"] == "think":
                data, res = agent.structured(prompt, multi_agent.THINK_SCHEMA, timeout=node["timeout"])
                vm_service.record_usage(ws, None, f"team:{node['id']}", settings, res,
                                        int((time.time() - t0) * 1000), cheap=True)
                return data
            with _code_lock(ws):
                repo_dir = f"{sh.home}/repos/{repo_key}"
                res = agent.agent(prompt, repo_dir, None, timeout=node["timeout"])
                status = sh.run(f"cd {repo_dir} && git status --porcelain 2>/dev/null || true", timeout=30).output
            vm_service.record_usage(ws, None, f"team:{node['id']}", settings, res, int((time.time() - t0) * 1000))
            if not res.ok:
                raise RuntimeError((res.text or res.raw_tail or "CLI agent failed")[:3000])
            out = multi_agent.coerce_output(res.text)
            out.meta = {"changed_files": sorted({l[3:].strip() for l in status.splitlines()
                                                 if l[3:].strip() and not l[3:].startswith(".agdd")}),
                        "cli": settings.cli, "model": res.model}
            return out
        finally:
            sh.close()
    return invoke


def _code_lock(ws) -> threading.Lock:
    with _lock:
        return _CODE_LOCKS.setdefault(str(ws), threading.Lock())


# ---------------------------------------------------------------- runs
def start_run(ws, team_id, graph: dict | None, request: str, user) -> dict:
    """Start a team run from a saved team (``team_id``) or an unsaved canvas graph."""
    name = "임시 팀"
    if team_id:
        with infra.pg(readonly=True) as c:
            row = c.execute("SELECT name, graph_json FROM agent_teams WHERE team_id=%s AND workspace_id=%s",
                            (team_id, ws)).fetchone()
        if row is None:
            raise LookupError("team not found")
        graph = graph or row["graph_json"]
        name = row["name"]
    clean = validate_team_graph(graph or {})
    with _lock:
        if len(_ACTIVE_WS.get(str(ws), set())) >= MAX_ACTIVE_PER_WORKSPACE:
            raise RuntimeError(f"이 워크스페이스에서 이미 {MAX_ACTIVE_PER_WORKSPACE}개의 팀 실행이 진행 중입니다")
    settings = vm_service.load_settings(ws) if vm_service.enabled() else None
    repo_key = clean["settings"]["repo_key"] or (settings.default_repo if settings else "workspace") or "workspace"
    if any(n["kind"] == "agent" and n["mode"] == "code" for n in clean["nodes"]):
        vm_service.repo_path(repo_key)
    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        run = c.execute("INSERT INTO agent_team_runs (team_id, workspace_id, graph_json, request, status, created_by) "
                        "VALUES (%s,%s,%s,%s,'RUNNING',%s) RETURNING team_run_id, created_at",
                        (team_id or None, ws, Jsonb(clean), request or "", uid)).fetchone()
        c.commit()
    rid = str(run["team_run_id"])
    cancel = threading.Event()
    with _lock:
        _ACTIVE[rid] = cancel
        _ACTIVE_WS.setdefault(str(ws), set()).add(rid)
    threading.Thread(target=_execute, args=(ws, rid, name, clean, request, repo_key, cancel, uid),
                     name=f"agdd-team-{rid[:8]}", daemon=True).start()
    return {"team_run_id": rid, "status": "RUNNING", "repo_key": repo_key}


def _execute(ws, rid, name, graph, request, repo_key, cancel, uid):
    holder: dict = {}
    last_state = [0.0]

    def on_event(ev):
        tr = holder.get("run")
        try:
            with infra.pg() as c:
                c.execute("INSERT INTO agent_team_events (team_run_id, seq, type, node, target, content, meta_json) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                          (rid, ev["seq"], ev["type"], ev["node"], ev["target"], ev["content"],
                           Jsonb(json.loads(json.dumps(ev["meta"], default=str)))))
                if tr is not None and (ev["type"] in ("node_started", "node_finished", "node_failed")
                                       or time.time() - last_state[0] > 2):
                    c.execute("UPDATE agent_team_runs SET node_state=%s, updated_at=now() WHERE team_run_id=%s",
                              (Jsonb(tr.snapshot()), rid))
                    last_state[0] = time.time()
                c.commit()
        except Exception as exc:
            print(f"[agdd-team] event write failed: {exc}", flush=True)

    try:
        if vm_service.enabled() and any(n["kind"] == "agent" and n["mode"] == "code" for n in graph["nodes"]):
            try:
                from . import vm_sync
                vm_sync.sync_workspace_context(ws, repo_key)
            except Exception as exc:
                print(f"[agdd-team] context sync failed: {exc}", flush=True)
        tr = TeamRun(graph, invoker(ws, repo_key, rid), resolver(ws, repo_key), request, on_event, cancel)
        holder["run"] = tr
        out = tr.run()
        status, result, state = out["status"], out["result"], out["node_state"]
    except Exception as exc:
        status, result, state = "FAILED", {"error": str(exc)[:4000]}, {}
    finally:
        with _lock:
            _ACTIVE.pop(rid, None)
            _ACTIVE_WS.get(str(ws), set()).discard(rid)
    try:
        with infra.pg() as c:
            c.execute("UPDATE agent_team_runs SET status=%s, result_json=%s, node_state=%s, updated_at=now() "
                      "WHERE team_run_id=%s",
                      (status, Jsonb(json.loads(json.dumps(result, default=str))), Jsonb(state), rid))
            outputs = (result or {}).get("outputs") or {}
            first = next((v[-1] for v in outputs.values() if v), "") if isinstance(outputs, dict) else ""
            pgstore.add_collab_message(
                c, ws, "event", "agdd",
                f"🤝 멀티 에이전트 팀 '{name}' 실행 종료: {status}" + (f"\n\n{first[:1500]}" if first else ""),
                meta={"event": "TEAM_RUN_FINISHED", "team_run_id": rid, "status": status}, user_id=uid)
            c.commit()
    except Exception as exc:
        print(f"[agdd-team] finish write failed: {exc}", flush=True)


def cancel_run(ws, rid) -> bool:
    with _lock:
        ev = _ACTIVE.get(str(rid))
    if ev is None:
        return False
    ev.set()
    return True


def get_run(ws, rid, after: int = 0) -> dict | None:
    with infra.pg() as c:
        run = c.execute("SELECT team_run_id, team_id, graph_json, request, status, node_state, result_json, "
                        "created_at, updated_at, (updated_at < now() - interval '3 minutes') AS stale "
                        "FROM agent_team_runs WHERE team_run_id=%s AND workspace_id=%s", (rid, ws)).fetchone()
        if run is None:
            return None
        with _lock:
            alive = str(rid) in _ACTIVE
        if run["status"] == "RUNNING" and not alive and run["stale"]:
            # the API process restarted mid-run: the thread is gone, so the run can never finish
            c.execute("UPDATE agent_team_runs SET status='FAILED', result_json=%s, updated_at=now() "
                      "WHERE team_run_id=%s", (Jsonb({"error": "agdd-api restarted during the run"}), rid))
            c.commit()
            run["status"] = "FAILED"
        events = c.execute("SELECT seq, type, node, target, content, meta_json, created_at FROM agent_team_events "
                           "WHERE team_run_id=%s AND seq > %s ORDER BY seq LIMIT 500", (rid, int(after))).fetchall()
    run.pop("stale", None)
    run["events"] = events
    run["active"] = alive
    return run


def list_runs(ws, team_id=None, limit: int = 20) -> list[dict]:
    q = ("SELECT team_run_id, team_id, request, status, created_at, updated_at FROM agent_team_runs "
         "WHERE workspace_id=%s" + (" AND team_id=%s" if team_id else "") + " ORDER BY created_at DESC LIMIT %s")
    args = (ws, team_id, limit) if team_id else (ws, limit)
    with infra.pg(readonly=True) as c:
        return c.execute(q, args).fetchall()


__all__ = ["TeamGraphError", "list_teams", "save_team", "delete_team", "start_run", "get_run", "list_runs",
           "cancel_run"]
