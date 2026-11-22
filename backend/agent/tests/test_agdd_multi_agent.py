"""Multi-agent team canvas: graph validation, scheduler (parallelism, review loops, direct messages), prompts,
service wiring (events persisted, collab notice) and the HTTP routes."""
from __future__ import annotations

import contextlib
import sys
import threading
import time
import types
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

from irade_agent.agdd import multi_agent as ma  # noqa: E402
from irade_agent.agdd.multi_agent import AgentOutput, TeamGraphError, TeamRun, validate_team_graph  # noqa: E402


def agent(id_, **kw):
    return {"id": id_, "kind": "agent", "label": id_, **kw}


def g(nodes, edges, **settings):
    return {"nodes": nodes, "edges": edges, "settings": settings}


# ---------------------------------------------------------------- validation
def test_presets_are_valid_and_normalized():
    for name in ma.PRESETS:
        clean = validate_team_graph(ma.preset(name))
        assert any(n["kind"] == "agent" for n in clean["nodes"])
        for n in clean["nodes"]:
            if n["kind"] == "agent":
                assert n["accepts"] and n["mode"] in ma.AGENT_MODES and 1 <= n["max_runs"] <= 10


@pytest.mark.parametrize("graph,msg", [
    ({"nodes": [], "edges": []}, "at least one agent"),
    ({"nodes": [agent("a")], "edges": []}, "no incoming edge"),
    ({"nodes": [{"id": "1x", "kind": "agent"}], "edges": []}, "node id"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a"), agent("a")], "edges": []}, "duplicate"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a")], "edges": [{"from": "a", "to": "i"}]}, "cannot receive"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a")], "edges": [{"from": "i", "to": "a", "when": "revise"}]},
     "only agent edges"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a", accepts=["bogus"])], "edges": [{"from": "i", "to": "a"}]},
     "accepts"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a", model="bad model!")], "edges": [{"from": "i", "to": "a"}]},
     "model"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a", can_message=["ghost"])], "edges": [{"from": "i", "to": "a"}]},
     "can_message"),
    ({"nodes": [{"id": "d", "kind": "data", "source": "repo_files", "params": {"paths": ["../etc/passwd"]}},
                agent("a")], "edges": [{"from": "d", "to": "a"}]}, "invalid path"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a")], "edges": [{"from": "a", "to": "a"}]}, "self loop"),
])
def test_validation_rejects_bad_graphs(graph, msg):
    with pytest.raises(TeamGraphError, match=msg):
        validate_team_graph(graph)


def test_validation_clamps_settings_and_strings():
    clean = validate_team_graph({
        "nodes": [{"id": "i", "kind": "input", "label": "x" * 500}, agent("a", max_runs=99, system_prompt="p" * 9000)],
        "edges": [{"from": "i", "to": "a"}], "settings": {"max_steps": 1000, "parallelism": 0}})
    assert clean["settings"] == {"max_steps": 100, "parallelism": 1, "repo_key": ""}
    a = clean["nodes"][1]
    assert a["max_runs"] == 10 and len(a["system_prompt"]) == 8000 and len(clean["nodes"][0]["label"]) == 80


# ---------------------------------------------------------------- output parsing
def test_coerce_output_markers_and_dicts():
    out = ma.coerce_output("done.\n<<TO:planner>>is X required?<</TO>>\n<<VERDICT:REVISE>>")
    assert out.verdict == "revise" and out.messages == [{"to": "planner", "content": "is X required?"}]
    assert out.content == "done."
    d = ma.coerce_output({"content": "ok", "verdict": "APPROVE", "messages": [{"to": "b", "content": "hi"}, "junk"]})
    assert d.verdict == "approve" and d.messages == [{"to": "b", "content": "hi"}]
    assert ma.coerce_output({"content": "x", "verdict": "maybe"}).verdict == "none"


# ---------------------------------------------------------------- scheduler
def _run(graph, behaviour, request="build it", **kw):
    calls, events = [], []
    lock = threading.Lock()

    def invoke(node, prompt, ctx):
        with lock:
            calls.append((node["id"], ctx["run_no"], prompt))
        return behaviour(node, prompt, ctx)

    tr = TeamRun(graph, invoke, lambda n: f"DATA<{n['id']}>", request, events.append, **kw)
    return tr.run(), calls, events


def test_review_loop_revises_then_approves():
    graph = ma.preset("review_loop")
    verdicts = iter(["revise", "approve"])

    def behave(node, prompt, ctx):
        if node["id"] == "planner":
            return {"content": "PLAN-1", "verdict": "none", "messages": []}
        if node["id"] == "coder":
            return AgentOutput(f"CODE-v{ctx['run_no']}")
        return {"content": "needs tests" if ctx["run_no"] == 1 else "LGTM", "verdict": next(verdicts), "messages": []}

    res, calls, events = _run(graph, behave)
    order = [(c[0], c[1]) for c in calls]
    assert order == [("planner", 1), ("coder", 1), ("reviewer", 1), ("coder", 2), ("reviewer", 2)]
    assert res["status"] == "SUCCEEDED"
    assert res["result"]["outputs"] == {"result": ["LGTM"]}
    coder2 = calls[3][2]
    assert "리뷰 피드백" in coder2 and "needs tests" in coder2 and "PLAN-1" in coder2  # feedback + persistent plan
    assert "DATA<spec>" in calls[0][2] and "build it" in calls[0][2]
    assert res["node_state"]["reviewer"]["verdict"] == "approve"
    assert [e["type"] for e in events][0] == "run_started" and events[-1]["type"] == "run_finished"


def test_review_loop_bounded_by_max_runs():
    graph = ma.preset("review_loop")

    def behave(node, prompt, ctx):
        if node["id"] == "reviewer":
            return {"content": "still wrong", "verdict": "revise", "messages": []}
        return {"content": node["id"], "verdict": "none", "messages": []}

    res, calls, events = _run(graph, behave)
    assert sum(1 for c in calls if c[0] == "coder") == 3  # coder.max_runs
    assert res["status"] == "FAILED"                       # nothing ever reached the output
    assert any(e["type"] == "node_skipped" and e["node"] == "coder" for e in events)


def test_parallel_agents_run_concurrently_and_synth_waits_for_all():
    graph = ma.preset("parallel")
    active, peak, lock = [0], [0], threading.Lock()

    def behave(node, prompt, ctx):
        if node["id"].startswith("analyst"):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.15)
            with lock:
                active[0] -= 1
        return {"content": f"OUT-{node['id']}", "verdict": "none", "messages": []}

    res, calls, _ = _run(graph, behave)
    assert peak[0] == 3
    synth = [c for c in calls if c[0] == "synth"]
    assert len(synth) == 1
    assert all(f"OUT-analyst_{k}" in synth[0][2] for k in ("ux", "arch", "risk"))
    assert res["status"] == "SUCCEEDED" and res["result"]["outputs"]["result"] == ["OUT-synth"]


def test_direct_messages_reactivate_teammates_and_answers_flow_back():
    graph = {
        "nodes": [{"id": "req", "kind": "input"}, agent("planner"), agent("coder"),
                  {"id": "out", "kind": "output"}],
        "edges": [{"from": "req", "to": "planner"}, {"from": "planner", "to": "coder"}, {"from": "coder", "to": "out"}],
    }

    def behave(node, prompt, ctx):
        if node["id"] == "coder" and ctx["run_no"] == 1:
            return {"content": "blocked", "verdict": "none", "messages": [{"to": "planner", "content": "SQL or NoSQL?"}]}
        if node["id"] == "planner" and ctx["run_no"] == 2:
            assert "SQL or NoSQL?" in prompt
            return {"content": "use SQL", "verdict": "none", "messages": [{"to": "coder", "content": "SQL, Postgres"}]}
        if node["id"] == "coder" and ctx["run_no"] >= 2:
            return {"content": "implemented with Postgres", "verdict": "none", "messages": []}
        return {"content": "plan", "verdict": "none", "messages": []}

    res, calls, events = _run(graph, behave)
    msgs = [(e["node"], e["target"]) for e in events if e["type"] == "message"]
    assert ("coder", "planner") in msgs and ("planner", "coder") in msgs
    assert "implemented with Postgres" in res["result"]["outputs"]["out"]
    # a message is consumed once: coder's later prompt keeps the plan but not the stale question
    assert all("SQL or NoSQL?" not in c[2] for c in calls if c[0] == "coder")


def test_accept_filter_and_message_permissions():
    graph = {
        "nodes": [{"id": "req", "kind": "input"}, {"id": "d", "kind": "data", "source": "text", "params": {"text": "T"}},
                  agent("a", can_message=["b"]), agent("b", accepts=["agent"]), agent("c")],
        "edges": [{"from": "req", "to": "a"}, {"from": "d", "to": "b"}, {"from": "a", "to": "b"}, {"from": "req", "to": "c"}],
    }

    def behave(node, prompt, ctx):
        if node["id"] == "a":
            return {"content": "A", "verdict": "none", "messages": [{"to": "c", "content": "hey"}, {"to": "b", "content": "yo"}]}
        return {"content": node["id"], "verdict": "none", "messages": []}

    res, calls, events = _run(graph, behave)
    types_ = [(e["type"], e["node"], e["target"]) for e in events]
    assert ("message_rejected", "a", "c") in types_          # c is not in a.can_message
    assert ("dropped", "d", "b") in types_                   # b accepts only agent results
    assert ("dropped", "a", "b") in types_                   # ... so a's direct message is dropped too
    b_prompts = [c[2] for c in calls if c[0] == "b"]
    assert len(b_prompts) == 1 and "DATA<d>" not in b_prompts[0]


def test_node_failure_and_data_failure_are_reported():
    graph = {"nodes": [{"id": "d", "kind": "data", "source": "cells"}, agent("a"), agent("b"), {"id": "o", "kind": "output"}],
             "edges": [{"from": "d", "to": "a"}, {"from": "d", "to": "b"}, {"from": "a", "to": "o"}, {"from": "b", "to": "o"}]}

    def invoke(node, prompt, ctx):
        if node["id"] == "a":
            raise RuntimeError("CLI crashed")
        assert "조회 실패" in prompt
        return {"content": "B ok", "verdict": "none", "messages": []}

    def resolve(n):
        raise RuntimeError("no VM")

    events = []
    res = TeamRun(graph, invoke, resolve, "", events.append).run()
    assert res["status"] == "PARTIAL" and res["failures"] == ["a"]
    assert any(e["type"] == "data_failed" for e in events) and any(e["type"] == "node_failed" for e in events)


def test_step_limit_and_cancel():
    graph = ma.preset("debate")
    graph["settings"]["max_steps"] = 2
    ping = lambda node, prompt, ctx: {"content": "x", "verdict": "none",  # noqa: E731
                                      "messages": [{"to": "con" if node["id"] == "pro" else "pro", "content": "rebut"}]}
    res, calls, _ = _run(graph, ping)
    assert res["status"] == "STEP_LIMIT" and len(calls) == 2

    cancel = threading.Event()

    def slow(node, prompt, ctx):
        cancel.set()
        return {"content": "x", "verdict": "none", "messages": []}

    res, calls, _ = _run(ma.preset("debate"), slow, cancel=cancel)
    assert res["status"] == "CANCELLED" and all(c[0] in ("pro", "con") for c in calls)


def test_build_prompt_role_template_roster_and_protocol():
    graph = validate_team_graph(ma.preset("review_loop"))
    coder = next(n for n in graph["nodes"] if n["id"] == "coder")
    coder["prompt_template"] = "PLAN={{input:planner}}\nREQ={{request}}\nMISSING={{feedback}}"
    inbox = {"planner": {"node": "planner", "kind": "agent", "content": "step 1"}}
    p = ma.build_prompt(graph, coder, inbox, "add login", [], 1)
    assert "PLAN=step 1" in p and "REQ=add login" in p and "MISSING=(없음)" in p
    assert "역할: 계획대로 저장소에 코드를 작성" in p and "`reviewer`" in p and "<<TO:" in p
    reviewer = next(n for n in graph["nodes"] if n["id"] == "reviewer")
    rp = ma.build_prompt(graph, reviewer, {}, "x", [{"node": "coder", "content": "did it"}], 2)
    assert "approve 또는 revise" in rp and "2번째 활성화" in rp


# ---------------------------------------------------------------- service + routes
class _Conn:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0

    def execute(self, sql, args=()):
        self.db["sql"].append(sql)
        s = " ".join(sql.split())
        rows = []
        unwrap = lambda v: getattr(v, "obj", v)  # noqa: E731
        if s.startswith("INSERT INTO agent_team_runs"):
            rid = f"{len(self.db['runs']) + 1:08d}-0000-4000-8000-000000000000"
            self.db["runs"][rid] = {"team_run_id": rid, "graph_json": unwrap(args[2]), "request": args[3],
                                    "status": "RUNNING", "node_state": {}, "result_json": {}, "team_id": args[0]}
            rows = [{"team_run_id": rid, "created_at": "now"}]
        elif s.startswith("INSERT INTO agent_team_events"):
            self.db["events"].append({"team_run_id": args[0], "seq": args[1], "type": args[2], "node": args[3],
                                      "target": args[4], "content": args[5], "meta_json": unwrap(args[6])})
        elif s.startswith("UPDATE agent_team_runs SET status"):
            self.db["runs"][args[3]].update(status=args[0], result_json=unwrap(args[1]), node_state=unwrap(args[2]))
        elif s.startswith("INSERT INTO collab_messages"):
            self.db["collab"].append(args[3])
            rows = [{"msg_id": 1}]
        elif s.startswith("SELECT team_run_id, team_id, graph_json"):
            r = self.db["runs"].get(args[0])
            rows = [{**r, "created_at": "", "updated_at": "", "stale": False}] if r else []
        elif s.startswith("SELECT seq, type"):
            rows = [e for e in self.db["events"] if e["team_run_id"] == args[0] and e["seq"] > args[1]]
        return _Cur(rows)

    def commit(self):
        pass


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


@pytest.fixture
def fake_db(monkeypatch):
    from irade_agent.agdd import infra, multi_agent_service as svc, vm_service
    db = {"sql": [], "runs": {}, "events": [], "collab": []}

    @contextlib.contextmanager
    def pg(readonly=False):
        yield _Conn(db)

    monkeypatch.setattr(infra, "pg", pg)
    monkeypatch.setattr(vm_service, "enabled", lambda: False)
    return db, svc


def test_service_run_persists_events_and_posts_collab_notice(fake_db, monkeypatch):
    db, svc = fake_db
    from irade_agent.agdd import infra

    def fake_llm(task, ws=None, run=None, node=None):
        def call(prompt, schema):
            assert schema is ma.THINK_SCHEMA
            return {"content": f"answer from {node}", "verdict": "none", "messages": []}
        return call

    monkeypatch.setattr(infra, "cheap_model", fake_llm)
    graph = ma.preset("parallel")
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "spec"]
    graph["edges"] = [e for e in graph["edges"] if e["from"] != "spec"]
    out = svc.start_run("ws-1", None, graph, "plan a feature", "11111111-1111-1111-1111-111111111111")
    rid = out["team_run_id"]
    for _ in range(200):
        if db["runs"][rid]["status"] != "RUNNING":
            break
        time.sleep(0.02)
    run = svc.get_run("ws-1", rid)
    assert run["status"] == "SUCCEEDED"
    assert run["result_json"]["outputs"]["result"] == ["answer from synth"]
    assert [e["type"] for e in run["events"]][-1] == "run_finished"
    assert db["collab"] and "SUCCEEDED" in db["collab"][0]


def test_service_code_mode_without_vm_fails_cleanly(fake_db):
    db, svc = fake_db
    out = svc.start_run("ws-1", None, ma.preset("review_loop"), "x", None)
    rid = out["team_run_id"]
    for _ in range(200):
        if db["runs"][rid]["status"] != "RUNNING":
            break
        time.sleep(0.02)
    # spec_scope data needs PG engine (not faked) -> data_failed; planner think needs gateway (not faked) -> fails
    assert db["runs"][rid]["status"] in ("FAILED", "PARTIAL")
    assert any(e["type"] in ("node_failed", "data_failed") for e in db["events"])


def test_routes_validate_and_map_errors(fake_db, monkeypatch):
    from irade_agent.agdd import api
    _, svc = fake_db
    assert api.teams_validate({}, "ws", {"graph": ma.preset("debate")})["ok"] is True
    bad = api.teams_validate({}, "ws", {"graph": {"nodes": [], "edges": []}})
    assert bad["ok"] is False and "agent" in bad["error"]
    with pytest.raises(api.HttpError) as ei:
        api.team_run_start({"user": None}, "ws", {"graph": {"nodes": [], "edges": []}})
    assert ei.value.code == 422
    with pytest.raises(api.HttpError) as ei:
        api.team_run_start({"user": None}, "ws", {"team_id": "not-a-uuid"})
    assert ei.value.code == 422
    monkeypatch.setattr(svc, "_ACTIVE_WS", {"ws": {"a", "b"}})
    with pytest.raises(api.HttpError) as ei:
        api.team_run_start({"user": None}, "ws", {"graph": ma.preset("debate")})
    assert ei.value.code == 409
