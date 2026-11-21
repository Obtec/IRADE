"""Control-plane API: RBAC, spec tables, Pre-Flight Gate, graphs, runs, HITL, UX overrides, research,
SSE log stream (DOC.md §2 Application & Control Layer)."""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psycopg.types.json import Jsonb

from . import vm_service
from . import infra, orchestrator, pgstore, preflight, router
from .spec_engine import TABLES
from .ux_workbench import LockedOverrideError, Workbench

ROUTES: list[tuple[str, re.Pattern, str, object]] = []


def route(method, pattern, min_role=None):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), min_role, fn))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _ws_engine(conn, ws):
    return pgstore.hydrate_engine(conn, ws)


# ---------------- workspace VM & CLI agents ----------------
def _engine(body) -> str:
    e = (body or {}).get("engine") or ("vm-cli" if vm_service.enabled() else "gateway")
    if e not in ("vm-cli", "gateway"):
        raise HttpError(422, "engine must be vm-cli or gateway")
    if e == "vm-cli" and not vm_service.enabled():
        raise HttpError(503, "VM runtime is not connected (AGORA_RUNTIME_SOCKET)")
    return e


def _vm_llm(ws, name, override=None):
    """Cheap structured steps through the workspace's selected CLI inside its VM."""
    import time as _t
    from .cli_agents import VmAgent
    from .vm import RuntimeClient, VmShell
    settings = vm_service.load_settings(ws, override)
    agent = VmAgent(VmShell(RuntimeClient(), vm_service.canvas_for(ws), name), settings)
    t0 = [_t.time()]

    def on_result(res):
        vm_service.record_usage(ws, None, name, settings, res, int((_t.time() - t0[0]) * 1000), cheap=True)
        t0[0] = _t.time()
    return agent.cheap_llm(on_result)


def _require_vm():
    if not vm_service.enabled():
        raise HttpError(503, "VM runtime is not connected (AGORA_RUNTIME_SOCKET)")


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm", "VIEWER")
def vm_status(ctx, ws, body):
    _require_vm()
    from .vm import RuntimeClient
    cid = vm_service.canvas_for(ws)
    return {"canvas_id": cid, "runtime": RuntimeClient().settings(cid), "repos_dir": vm_service.REPOS_DIR}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/vm/resources", "OWNER")
def vm_resources(ctx, ws, body):
    _require_vm()
    from .vm import RuntimeClient
    rc, cid = RuntimeClient(), vm_service.canvas_for(ws)
    if "template" in body:
        r = rc.set_resources(cid, body["template"], int(body["revision"]))
    elif "sizeGiB" in body:
        r = rc.set_disk(cid, int(body["sizeGiB"]), int(body["revision"]))
    else:
        raise HttpError(422, "template or sizeGiB required")
    if r.get("error"):
        raise HttpError(409, r.get("message") or r["error"])
    return r


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/reset", "OWNER")
def vm_reset(ctx, ws, body):
    _require_vm()
    from .vm import RuntimeClient
    return RuntimeClient().reset(vm_service.canvas_for(ws))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/terminal/(?P<name>[A-Za-z0-9_-]{1,64})", "EDITOR")
def vm_terminal(ctx, ws, body, name):
    """Web terminal relay: open/poll/input/resize/close on the user's own PTY in the workspace VM.
    Automation PTYs (agdd-*) belong to owner 1 and can only be watched (poll) by members."""
    _require_vm()
    from .vm import SYSTEM_OWNER, RuntimeClient, owner_for
    op = body.get("operation")
    if op not in ("open", "poll", "input", "resize", "close"):
        raise HttpError(422, "invalid operation")
    watch = name.startswith("agdd-")
    if watch and op not in ("open", "poll"):
        raise HttpError(403, "automation terminals are read-only")
    owner = SYSTEM_OWNER if watch else owner_for(ctx["user"])
    r = RuntimeClient().terminal(vm_service.canvas_for(ws), owner, name, "poll" if watch and op == "open" else op,
                                 str(body.get("data", ""))[:8000], int(body.get("cols", 120)), int(body.get("rows", 32)),
                                 int(body.get("offset", 0)))
    return r


@route("GET", r"/workspaces/(?P<ws>[^/]+)/agent-settings", "VIEWER")
def get_agent_settings(ctx, ws, body):
    from .cli_agents import EFFORTS, PROFILES
    s = vm_service.load_settings(ws)
    return {"settings": s.__dict__, "efforts": EFFORTS, "engine_default": "vm-cli" if vm_service.enabled() else "gateway",
            "clis": {k: {"models": list(p.models), "install": p.install, "login": p.login_hint}
                     for k, p in PROFILES.items()}}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/agent-settings", "EDITOR")
def put_agent_settings(ctx, ws, body):
    try:
        return vm_service.save_settings(ws, body, ctx["user"]).__dict__
    except ValueError as e:
        raise HttpError(422, str(e))


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/clis", "VIEWER")
def vm_clis(ctx, ws, body):
    _require_vm()
    from .cli_agents import PROFILES
    sh = vm_service.shell(ws, "agdd-api")
    out = sh.run("; ".join(f"echo {k}=$( {p.version_cmd()} 2>/dev/null | head -1 )" for k, p in PROFILES.items()),
                 timeout=120).output
    vers = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return {k: {"installed": bool(vers.get(k, "").strip()), "version": vers.get(k, "").strip(),
                "install": p.install, "login": p.login_hint} for k, p in PROFILES.items()}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/sessions/(?P<cli>claude|codex|agy)", "VIEWER")
def vm_sessions(ctx, ws, body, cli):
    _require_vm()
    from .cli_agents import PROFILES, parse_sessions
    out = vm_service.shell(ws, "agdd-api").run(PROFILES[cli].sessions_cmd(), timeout=120).output
    return {"sessions": parse_sessions(out), "raw": out[-4000:] if cli == "agy" else ""}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/usage/(?P<cli>claude|codex|agy)", "VIEWER")
def vm_usage(ctx, ws, body, cli):
    """Remaining plan usage as reported by the CLI itself + rate-limit snapshots seen during runs + AGDD's own tally."""
    _require_vm()
    from .cli_agents import PROFILES, parse_codex_rate_limits
    out = vm_service.shell(ws, "agdd-api").run(PROFILES[cli].usage_cmd(), timeout=300).output
    parsed = parse_codex_rate_limits(out) if cli == "codex" else None
    with infra.pg() as c:
        if parsed:
            c.execute("INSERT INTO cli_usage_snapshots (workspace_id, cli, rate_limits, raw_text) VALUES (%s,%s,%s,%s) "
                      "ON CONFLICT (workspace_id, cli) DO UPDATE SET rate_limits=EXCLUDED.rate_limits, "
                      "raw_text=EXCLUDED.raw_text, captured_at=now()", (ws, cli, Jsonb(parsed), out[-8000:]))
        snap = c.execute("SELECT rate_limits, captured_at FROM cli_usage_snapshots WHERE workspace_id=%s AND cli=%s",
                         (ws, cli)).fetchone()
        tally = c.execute(
            "SELECT model_name, count(*) calls, sum(prompt_tokens) prompt, sum(cached_tokens) cached, "
            "sum(output_tokens) output, sum(thinking_tokens) thinking, sum(cost_usd) cost FROM node_executions e "
            "JOIN workflow_runs r USING (run_id) JOIN graph_definitions g ON g.graph_id=r.graph_id "
            "AND g.version=r.graph_version WHERE g.workspace_id=%s AND e.model_name LIKE %s "
            "AND e.created_at > now() - interval '7 days' GROUP BY 1", (ws, cli + ":%")).fetchall()
        c.commit()
    return {"cli": cli, "report": out[-6000:], "rate_limits": parsed or (snap or {}).get("rate_limits"),
            "rate_limits_at": (snap or {}).get("captured_at"), "agdd_7d": tally}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/demo-repo", "EDITOR")
def vm_demo_repo(ctx, ws, body):
    """Creates /workspace/repos/demo (buggy calc + tests) inside the VM for a first run."""
    _require_vm()
    sh = vm_service.shell(ws, "agdd-api")
    d = "/workspace/repos/demo"
    from .cli_agents import PREREQ
    sh.run(PREREQ + f"rm -rf {d} && mkdir -p {d}/tests", timeout=900, check=True)
    sh.put_file(f"{d}/calc.py", DEMO_CALC)
    sh.put_file(f"{d}/tests/test_calc.py", DEMO_TEST)
    sh.put_file(f"{d}/.gitignore", ".agdd/\n__pycache__/\n.pytest_cache/\n")
    r = sh.run(f"cd {d} && git init -q && git add . && git -c user.name=agdd -c user.email=agdd@localhost "
               f"commit -qm 'demo: buggy calc' && (python3 -c 'import pytest' 2>/dev/null || "
               f"python3 -m pip install -q pytest)", timeout=600)
    if r.exit_code != 0:
        raise HttpError(500, r.output[-1500:])
    return {"repo_key": "demo", "path": d}


DEMO_CALC = '''"""Tiny demo module for trying AGDD."""


def add(a, b):
    return a - b


def mean(values):
    return sum(values) / len(values)
'''
DEMO_TEST = '''import pytest

from calc import add, mean


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_mean_empty_raises_value_error():
    with pytest.raises(ValueError):
        mean([])
'''


# ---------------- bootstrap ----------------
def _ensure_user(c, user):
    c.execute("INSERT INTO users (user_id, email) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
              (user, f"{user}@agdd.local"))


@route("GET", r"/workspaces")
def my_workspaces(ctx, _, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT w.workspace_id, w.name, m.role FROM workspace_members m JOIN workspaces w "
                         "USING (workspace_id) WHERE m.user_id=%s ORDER BY w.created_at", (ctx["user"],)).fetchall()


@route("POST", r"/workspaces")
def create_workspace(ctx, _, body):
    name = str(body.get("name") or "").strip()[:100]
    if not name:
        raise HttpError(422, "name required")
    with infra.pg() as c:
        _ensure_user(c, ctx["user"])
        w = c.execute("INSERT INTO workspaces (name) VALUES (%s) RETURNING workspace_id", (name,)).fetchone()
        c.execute("INSERT INTO workspace_members VALUES (%s,%s,'OWNER')", (w["workspace_id"], ctx["user"]))
        c.commit()
    return {"workspace_id": w["workspace_id"], "name": name, "role": "OWNER"}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/members", "OWNER")
def add_member(ctx, ws, body):
    if body.get("role") not in pgstore.ROLE_RANK or not re.fullmatch(r"[0-9a-f-]{36}", str(body.get("user_id"))):
        raise HttpError(422, "user_id(uuid) and role required")
    with infra.pg() as c:
        _ensure_user(c, body["user_id"])
        c.execute("INSERT INTO workspace_members VALUES (%s,%s,%s) ON CONFLICT (workspace_id, user_id) "
                  "DO UPDATE SET role=EXCLUDED.role", (ws, body["user_id"], body["role"]))
        c.commit()
    return {"ok": True}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/seed-example", "EDITOR")
def seed_example(ctx, ws, body):
    """Loads the platform's own spec tables (DOC.md §3.3) so the gate and runs can be tried immediately."""
    from .seed import PLATFORM_SNAPSHOT
    with infra.pg() as c:
        sid = pgstore.save_snapshot(c, ws, {"srs": PLATFORM_SNAPSHOT["srs"], "arc": PLATFORM_SNAPSHOT["arc"],
                                            "sch": PLATFORM_SNAPSHOT["sch"], "trace": PLATFORM_SNAPSHOT["trace"]},
                                    False)
        c.commit()
    return {"snapshot_id": sid}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/graphs", "VIEWER")
def list_graphs(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT DISTINCT ON (graph_id) graph_id, version, definition_json, created_at "
                         "FROM graph_definitions WHERE workspace_id=%s AND is_published "
                         "ORDER BY graph_id, version DESC", (ws,)).fetchall()


@route("GET", r"/workspaces/(?P<ws>[^/]+)/runs", "VIEWER")
def list_runs(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT r.run_id, r.route_mode, r.current_node, r.status, r.created_at, r.updated_at "
                         "FROM workflow_runs r JOIN graph_definitions g ON g.graph_id=r.graph_id "
                         "AND g.version=r.graph_version WHERE g.workspace_id=%s ORDER BY r.created_at DESC LIMIT 50",
                         (ws,)).fetchall()


@route("GET", r"/workspaces/(?P<ws>[^/]+)/conflicts", "VIEWER")
def last_gate(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT snapshot_id, version, conflict_passed, created_at FROM spec_snapshots "
                         "WHERE workspace_id=%s ORDER BY version DESC LIMIT 1", (ws,)).fetchone() or {}


# ---------------- specs ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/specs", "VIEWER")
def list_specs(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        eng = _ws_engine(c, ws)
    try:
        return {t.lower(): eng.rows(t, accept_only=False) for t in TABLES} | {"trace": eng.snapshot()["trace"]}
    finally:
        eng.close()


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/specs/(?P<typ>SRS|ARC|SCH)", "EDITOR")
def upsert_spec(ctx, ws, body, typ):
    """Write-through: every edit lands in the in-memory engine and is persisted as a new (unpassed) snapshot."""
    with infra.pg() as c:
        eng = _ws_engine(c, ws)
        try:
            rows = body if isinstance(body, list) else [body]
            for r in rows:
                eng.upsert(typ, r)
            for tr in (body.get("trace", []) if isinstance(body, dict) else []):
                eng.trace(*tr)
            sid = pgstore.save_snapshot(c, ws, eng.snapshot(), False)
            c.commit()
        finally:
            eng.close()
    infra_safe(infra.cache_invalidate, f"agdd:dash:{ws}")
    return {"snapshot_id": sid}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/trace", "EDITOR")
def put_trace(ctx, ws, body):
    with infra.pg() as c:
        eng = _ws_engine(c, ws)
        try:
            for t in body:
                eng.trace(t["source_type"], t["source_id"], t["target_type"], t["target_id"], t["relation_type"])
            sid = pgstore.save_snapshot(c, ws, eng.snapshot(), False)
            c.commit()
        finally:
            eng.close()
    return {"snapshot_id": sid}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/preflight", "EDITOR")
def run_preflight(ctx, ws, body):
    """3-step: (1) hydrate in a short read -> (2) SQL + LLM checks with no DB connection held ->
    (3) commit the snapshot in a short write (DOC.md §4.1: LLM calls are never inside a transaction)."""
    from concurrent.futures import ThreadPoolExecutor

    with infra.pg(readonly=True) as c:
        eng = _ws_engine(c, ws)
    try:
        use_vm = _engine(body) == "vm-cli"
        llm_for = (lambda task: _vm_llm(ws, "agdd-gate")) if use_vm else (lambda task: infra.cheap_model(task, ws))
        llm = llm_for("conflict_check") if body.get("semantic", True) else None
        snaps = []
        with ThreadPoolExecutor(max_workers=1) as ex:  # ambiguity linting overlaps the conflict gate
            amb = ex.submit(router.ambiguity_gate, eng.rows("SRS"),
                            _vm_llm(ws, "agdd-amb") if use_vm else llm_for("ambiguity")) \
                if body.get("ambiguity", True) else None
            res = preflight.run_gate(eng, llm, lambda snap, ok: snaps.append((snap, ok)))
            questions = amb.result() if amb and res.passed else []
    finally:
        eng.close()
    with infra.pg() as c:
        sid = pgstore.save_snapshot(c, ws, *snaps[0])
        c.commit()
    infra_safe(infra.cache_invalidate, f"agdd:dash:{ws}")
    return {"passed": res.passed, "snapshot_id": sid, "conflicts": res.conflicts,
            "ambiguity_questions": questions, "scope": res.scope}


# ---------------- graphs ----------------
@route("POST", r"/workspaces/(?P<ws>[^/]+)/graphs", "EDITOR")
def publish_graph(ctx, ws, body):
    try:
        orchestrator.validate_graph(body["definition"])
    except (orchestrator.GraphError, KeyError) as e:
        raise HttpError(422, str(e))
    with infra.pg() as c:
        gid = body.get("graph_id")
        r = c.execute(
            "INSERT INTO graph_definitions (graph_id, workspace_id, version, definition_json, is_published) "
            "SELECT coalesce(%s::uuid, gen_random_uuid()), %s, coalesce(max(version),0)+1, %s, true "
            "FROM graph_definitions WHERE graph_id = %s::uuid RETURNING graph_id, version",
            (gid, ws, Jsonb(body["definition"]), gid)).fetchone()
        c.commit()
    return r


# ---------------- runs ----------------
@route("POST", r"/workspaces/(?P<ws>[^/]+)/runs", "EDITOR")
def start_run(ctx, ws, body):
    with infra.pg() as c:
        snap = c.execute("SELECT snapshot_id FROM spec_snapshots WHERE workspace_id=%s AND conflict_passed "
                         "ORDER BY version DESC LIMIT 1", (ws,)).fetchone()
        latest = pgstore.latest_snapshot(c, ws)
        if not snap or latest["snapshot_id"] != snap["snapshot_id"]:
            raise HttpError(409, "latest spec snapshot has not passed the Pre-Flight Gate")
        g = c.execute("SELECT graph_id, version, definition_json FROM graph_definitions WHERE graph_id=%s "
                      "AND workspace_id=%s AND is_published ORDER BY version DESC LIMIT 1",
                      (body["graph_id"], ws)).fetchone()
        if not g:
            raise HttpError(404, "graph not found")
        engine = _engine(body)
        agent_override = body.get("agent") or {}
        if engine == "vm-cli":
            vm_service.load_settings(ws, agent_override)  # validates cli/model/effort/session before queueing
        route_llm = _vm_llm(ws, "agdd-route", agent_override) if engine == "vm-cli" else infra.cheap_model("route", ws)
        decision = router.route_request(body.get("request", ""), route_llm) \
            if body.get("request") else router.RouteDecision("FULL_AGDD", ["no request text"])
        entry = g["definition_json"].get("fast_track_entry") if decision.route_mode == "FAST_TRACK" else None
        entry = entry or g["definition_json"]["entry"]
        run = c.execute(
            "INSERT INTO workflow_runs (graph_id, graph_version, snapshot_id, route_mode, current_node, status, handoff_json) "
            "VALUES (%s,%s,%s,%s,%s,'PENDING',%s) RETURNING run_id",
            (g["graph_id"], g["version"], snap["snapshot_id"], decision.route_mode, entry,
             Jsonb({"request": body.get("request", ""), "repo_key": body.get("repo_key", ""), "engine": engine,
                    "agent": agent_override}))).fetchone()
        c.execute(orchestrator.ENQUEUE_SQL, {"run_id": run["run_id"], "node_name": entry, "priority": 5})
        c.commit()
    return {"run_id": run["run_id"], "route_mode": decision.route_mode, "reasons": decision.reasons}


@route("GET", r"/runs/(?P<run>[0-9a-f-]+)")
def get_run(ctx, run, body):
    def load():
        with infra.pg(readonly=True) as c:
            r = c.execute("SELECT * FROM workflow_runs WHERE run_id=%s", (run,)).fetchone()
            if not r:
                return None
            r["executions"] = c.execute(
                "SELECT node_name, model_name, prompt_tokens, cached_tokens, output_tokens, thinking_tokens, cost_usd, "
                "duration_ms, created_at FROM node_executions WHERE run_id=%s ORDER BY created_at", (run,)).fetchall()
            r["hitl"] = c.execute("SELECT * FROM hitl_decisions WHERE run_id=%s ORDER BY decision_id", (run,)).fetchall()
            return r
    r = infra_safe(infra.cache_get_or_set, f"agdd:run:{run}", 5, load, fallback=load)
    if not r:
        raise HttpError(404, "run not found")
    _check_run_role(ctx, run, "VIEWER")
    return r


def _check_run_role(ctx, run, minimum):
    with infra.pg(readonly=True) as c:
        ws = c.execute("SELECT g.workspace_id FROM workflow_runs r JOIN graph_definitions g "
                       "ON g.graph_id=r.graph_id AND g.version=r.graph_version WHERE r.run_id=%s", (run,)).fetchone()
        if not ws:
            raise HttpError(404, "run not found")
        try:
            pgstore.require_role(c, ws["workspace_id"], ctx["user"], minimum)
        except PermissionError as e:
            raise HttpError(403, str(e))
    return ws["workspace_id"]


# ---------------- HITL ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/hitl", "VIEWER")
def list_hitl(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute(
            "SELECT h.* FROM hitl_decisions h JOIN workflow_runs r USING (run_id) JOIN graph_definitions g "
            "ON g.graph_id=r.graph_id AND g.version=r.graph_version WHERE g.workspace_id=%s AND h.status='PENDING' "
            "ORDER BY h.reversibility DESC, h.decision_id", (ws,)).fetchall()


@route("POST", r"/hitl/(?P<did>\d+)/decide")
def decide(ctx, did, body):
    status = body.get("status")
    if status not in ("APPROVED", "REJECTED"):
        raise HttpError(422, "status must be APPROVED|REJECTED")
    with infra.pg() as c:
        d = c.execute("SELECT run_id FROM hitl_decisions WHERE decision_id=%s", (did,)).fetchone()
        if not d:
            raise HttpError(404, "decision not found")
        _check_run_role(ctx, d["run_id"], "APPROVER")
        r = c.execute("UPDATE hitl_decisions SET status=%s, decided_by=%s, decided_at=now() "
                      "WHERE decision_id=%s AND status='PENDING' RETURNING run_id", (status, ctx["user"], did)).fetchone()
        if not r:
            raise HttpError(409, "already decided")
        pending = c.execute("SELECT count(*) AS n FROM hitl_decisions WHERE run_id=%s AND status='PENDING' "
                            "AND reversibility='IRREVERSIBLE'", (r["run_id"],)).fetchone()["n"]
        if pending == 0:
            run = c.execute("SELECT current_node, status FROM workflow_runs WHERE run_id=%s FOR UPDATE",
                            (r["run_id"],)).fetchone()
            if run["status"] == "WAITING_HITL":
                new_status = "RUNNING" if status == "APPROVED" else "FAILED"
                c.execute("UPDATE workflow_runs SET status=%s, version=version+1, updated_at=now() WHERE run_id=%s",
                          (new_status, r["run_id"]))
                if status == "APPROVED":
                    c.execute(orchestrator.ENQUEUE_SQL, {"run_id": r["run_id"], "node_name": run["current_node"],
                                                         "priority": 1})
        c.commit()
    infra_safe(infra.cache_invalidate, f"agdd:run:{r['run_id']}")
    return {"ok": True}


# ---------------- UX workbench ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)", "VIEWER")
def ux_get(ctx, ws, body, view):
    return Workbench(pgstore.PgOverrideStore()).export_bindings(ws, view)


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)", "EDITOR")
def ux_put(ctx, ws, body, view):
    wb = Workbench(pgstore.PgOverrideStore())
    try:
        for sel, item in body.items():
            wb.user_set(ws, view, sel, item["config"], ctx["user"], lock=item.get("locked", True))
    except ValueError as e:
        raise HttpError(422, str(e))
    return wb.export_bindings(ws, view)


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)/unlock", "EDITOR")
def ux_unlock(ctx, ws, body, view):
    wb = Workbench(pgstore.PgOverrideStore())
    for sel in body.get("selectors", []):
        wb.unlock(ws, view, sel, ctx["user"])
    return {"ok": True}


# ---------------- research ----------------
@route("POST", r"/workspaces/(?P<ws>[^/]+)/research", "EDITOR")
def research(ctx, ws, body):
    req = urllib.request.Request(os.environ.get("RESEARCH_URL", "http://research:8092") + "/research",
                                 data=json.dumps({"problem": body["problem"], "trigger_source": "HUMAN",
                                                  "run_id": body.get("run_id"), "workspace_id": ws}).encode(),
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {os.environ.get('INTERNAL_TOKEN', '')}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


# ---------------- dashboard ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/dashboard", "VIEWER")
def dashboard(ctx, ws, body):
    def load():
        with infra.pg(readonly=True) as c:
            return {
                "runs": c.execute(
                    "SELECT r.status, count(*) AS n FROM workflow_runs r JOIN graph_definitions g ON g.graph_id=r.graph_id "
                    "AND g.version=r.graph_version WHERE g.workspace_id=%s GROUP BY r.status", (ws,)).fetchall(),
                "budget": c.execute("SELECT * FROM workspace_budgets WHERE workspace_id=%s ORDER BY period_ym DESC LIMIT 1",
                                    (ws,)).fetchone(),
                "tokens": c.execute(
                    "SELECT e.model_name, sum(prompt_tokens) p, sum(cached_tokens) cached, sum(thinking_tokens) thinking, "
                    "sum(cost_usd) cost FROM node_executions e JOIN workflow_runs r USING (run_id) JOIN graph_definitions g "
                    "ON g.graph_id=r.graph_id AND g.version=r.graph_version WHERE g.workspace_id=%s "
                    "AND e.created_at > now() - interval '7 days' GROUP BY 1", (ws,)).fetchall(),
            }
    return infra_safe(infra.cache_get_or_set, f"agdd:dash:{ws}", 15, load, fallback=load)


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/budget", "OWNER")
def set_budget(ctx, ws, body):
    period = body.get("period_ym") or datetime.now(timezone.utc).strftime("%Y-%m")
    with infra.pg() as c:
        c.execute("INSERT INTO workspace_budgets (workspace_id, period_ym, limit_usd) VALUES (%s,%s,%s) "
                  "ON CONFLICT (workspace_id, period_ym) DO UPDATE SET limit_usd=EXCLUDED.limit_usd",
                  (ws, period, body["limit_usd"]))
        c.commit()
    return {"ok": True}


def infra_safe(fn, *args, fallback=None):
    try:
        return fn(*args)
    except Exception:
        return fallback() if callable(fallback) else fallback


# ---------------- HTTP plumbing ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = pgstore.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        # The upstream gateway (Spring/nginx) authenticates the user and forwards X-User-Id with a shared token.
        if self.headers.get("authorization", "") != f"Bearer {os.environ.get('API_TOKEN', '')}" or \
                not os.environ.get("API_TOKEN"):
            raise HttpError(401, "unauthorized")
        user = self.headers.get("x-user-id", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", user):
            raise HttpError(401, "x-user-id required")
        return {"user": user}

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if method == "GET" and path in ("/", "/console"):
            data = (Path(__file__).parent / "static" / "console.html").read_bytes()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("content-security-policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                             "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            if method == "GET" and path.startswith("/runs/") and path.endswith("/logs"):
                return self._sse(path.split("/")[2], self._auth())
            for m, rx, min_role, fn in ROUTES:
                mt = rx.match(path)
                if m == method and mt:
                    ctx = self._auth()
                    n = int(self.headers.get("content-length", 0) or 0)
                    body = json.loads(self.rfile.read(n)) if n else {}
                    kw = mt.groupdict()
                    if min_role:
                        with infra.pg(readonly=True) as c:
                            try:
                                pgstore.require_role(c, kw["ws"], ctx["user"], min_role)
                            except PermissionError as e:
                                raise HttpError(403, str(e))
                    ws_or_first = kw.pop("ws", None) or (kw.pop(next(iter(kw))) if kw else None)
                    return self._send(200, fn(ctx, ws_or_first, body, **kw))
            raise HttpError(404, "not found")
        except HttpError as e:
            self._send(e.code, {"error": str(e)})
        except LockedOverrideError as e:
            self._send(423, {"error": str(e)})
        except Exception as e:  # CLI / VM failures must surface, never pass silently
            from .cli_agents import CliError
            from .vm import RuntimeError_
            if isinstance(e, (CliError, RuntimeError_, TimeoutError)):
                self._send(502, {"error": str(e)[:600]})
            else:
                raise
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": repr(e)[:300]})

    def _sse(self, run, ctx):
        _check_run_role(ctx, run, "VIEWER")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        last = "0-0"
        try:
            while True:
                res = infra.valkey().xread({f"agdd:log:{run}": last}, block=15000, count=100) or []
                for _, entries in res:
                    for eid, fields in entries:
                        last = eid
                        self.wfile.write(f"id: {eid}\ndata: {json.dumps(fields, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")


def main():
    infra.wait_for_pg()
    try:
        infra.ensure_bucket()
    except Exception as e:
        print(f"[agdd] object storage not ready: {e!r}", flush=True)
    if os.environ.get("AGDD_MIGRATE", "1") == "1":
        infra.migrate()
    port = int(os.environ.get("PORT", "8091"))
    print(f"[agdd-api] listening :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
