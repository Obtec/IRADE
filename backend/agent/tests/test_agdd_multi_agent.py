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
        elif "agent_team_attachment" in s:
            rows = self._attachments(s, args)
        return _Cur(rows, self.rowcount)

    # minimal in-memory model of agent_team_attachments / agent_team_attachment_chunks
    def _attachments(self, s, args):
        att, chunks = self.db.setdefault("att", {}), self.db.setdefault("chunks", {})
        meta = lambda a: {k: a[k] for k in ("attachment_id", "name", "mime", "modality", "size_bytes", "sha256",  # noqa: E731
                                             "status", "created_at")}
        if s.startswith("SELECT COALESCE(sum(size_bytes)"):
            return [{"n": sum(a["size_bytes"] for a in att.values() if a["workspace_id"] == args[0])}]
        if s.startswith("INSERT INTO agent_team_attachments"):
            aid = f"{len(att) + 1:08d}-aaaa-4000-8000-000000000000"
            att[aid] = {"attachment_id": aid, "workspace_id": args[0], "name": args[1], "mime": args[2],
                        "modality": args[3], "size_bytes": args[4], "chunk_size": args[5], "sha256": None,
                        "status": "UPLOADING", "excerpt": None, "created_at": f"t{len(att)}"}
            return [meta(att[aid])]
        if s.startswith("SELECT attachment_id, size_bytes, chunk_size, status"):
            a = att.get(args[0])
            return [a] if a and a["workspace_id"] == args[1] else []
        if s.startswith("INSERT INTO agent_team_attachment_chunks"):
            chunks[(args[0], args[1])] = bytes(args[2])
            return []
        if s.startswith("SELECT count(*)"):
            mine = [v for (a, _), v in chunks.items() if a == args[0]]
            return [{"n": len(mine), "bytes": sum(len(v) for v in mine)}]
        if s.startswith("SELECT modality"):
            return [{"modality": att[args[0]]["modality"]}]
        if s.startswith("SELECT idx"):
            return [{"idx": i} for (a, i) in sorted(chunks) if a == args[0]]
        if s.startswith("SELECT data"):
            return [{"data": chunks[(args[0], args[1])]}] if (args[0], args[1]) in chunks else []
        if s.startswith("UPDATE agent_team_attachments SET status='READY'"):
            a = att[args[2]]
            a.update(status="READY", sha256=args[0], excerpt=args[1])
            return [meta(a)]
        if s.startswith("SELECT attachment_id") and "ANY" in s:
            return [{**meta(a), "excerpt": a["excerpt"]} for a in att.values()
                    if a["workspace_id"] == args[0] and a["status"] == "READY" and a["attachment_id"] in args[1]]
        if s.startswith("SELECT attachment_id") and "ORDER BY created_at" in s:
            return [meta(a) for a in att.values() if a["workspace_id"] == args[0] and a["status"] == "READY"]
        if s.startswith("SELECT attachment_id"):
            a = att.get(args[0])
            return [meta(a)] if a and a["workspace_id"] == args[1] and a["status"] == "READY" else []
        if s.startswith("DELETE FROM agent_team_attachments"):
            self.rowcount = 1 if att.pop(args[0], None) else 0
            return []
        return []

    def commit(self):
        pass


class _Cur:
    def __init__(self, rows, rowcount=0):
        self.rows = rows
        self.rowcount = rowcount

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


def test_mock_review_run_persists_results_without_live_invoker(fake_db, monkeypatch):
    db, svc = fake_db

    def unexpected_invoker(*args):
        raise AssertionError("a mock-only team must not initialize the live invoker")

    monkeypatch.setattr(svc, "invoker", unexpected_invoker)
    rid = svc.start_run("ws-1", None, ma.preset("mock_review"), "모의 실행", None)["team_run_id"]
    for _ in range(200):
        if db["runs"][rid]["status"] != "RUNNING":
            break
        time.sleep(0.02)
    run = svc.get_run("ws-1", rid)
    assert run["status"] == "SUCCEEDED"
    assert run["result_json"]["outputs"] == {"result": ["모의 검토 승인"]}
    assert [(e["node"], e["meta_json"]["mode"]) for e in run["events"] if e["type"] == "node_started"] == [
        ("generator", "mock"), ("evaluator", "mock"), ("generator", "mock"), ("evaluator", "mock"),
    ]


def test_mock_spawn_worker_and_last_response_reuse():
    graph = ma.preset("orchestrator_workers")
    responses = {
        "lead": [{"content": "plan", "tasks": [{"agent": "worker", "task": task} for task in ("A", "B")]},
                 {"content": "summary"}],
        "worker": [{"content": "fixture"}],
        "citer": [{"content": "final"}],
    }
    for n in graph["nodes"]:
        if n["kind"] == "agent":
            n.update(mode="mock", mock_responses=responses[n["id"]])
    events = []
    result = TeamRun(graph, None, lambda n: "", "test", events.append).run()
    assert result["status"] == "SUCCEEDED"
    assert result["result"]["outputs"] == {"result": ["final"]}
    assert sum(e["type"] == "node_finished" and e["meta"].get("subtask", False) and e["content"] == "fixture"
               for e in events) == 2


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


# ================================================================ production patterns + multimodal inputs
def _out(content, **kw):
    return {"content": content, "verdict": "none", "messages": [], **kw}


@pytest.mark.parametrize("graph,msg", [
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a"), {"id": "o", "kind": "output"}],
      "edges": [{"from": "i", "to": "a"}, {"from": "a", "to": "o", "when": "handoff"}]}, "must target an agent"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("lead"), agent("w"), {"id": "o", "kind": "output"}],
      "edges": [{"from": "i", "to": "lead"}, {"from": "lead", "to": "w", "when": "spawn"}, {"from": "w", "to": "o"}]},
     "cannot have outgoing"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("lead"), agent("w")],
      "edges": [{"from": "i", "to": "lead"}, {"from": "lead", "to": "w", "when": "spawn"}, {"from": "lead", "to": "w"}]},
     "only receives spawn"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a", modalities=["smell"])], "edges": [{"from": "i", "to": "a"}]},
     "modalities"),
    ({"nodes": [{"id": "i", "kind": "input"}, agent("a", output_key="bad key")], "edges": [{"from": "i", "to": "a"}]},
     "output_key"),
    ({"nodes": [{"id": "i", "kind": "input", "attachments": ["nope"]}, agent("a")], "edges": [{"from": "i", "to": "a"}]},
     "attachment id"),
])
def test_validation_of_pattern_edges_and_media_fields(graph, msg):
    with pytest.raises(TeamGraphError, match=msg):
        validate_team_graph(graph)


def test_new_presets_cover_vendor_patterns():
    info = ma.PRESET_INFO
    assert {"orchestrator_workers", "handoff_router", "guardrails", "sequential_state", "evaluator_optimizer",
            "voting", "multimodal_review"} <= set(info)
    joined = " ".join(v["vendor"] for v in info.values())
    assert "Anthropic" in joined and "OpenAI" in joined and "Google ADK" in joined
    a = next(n for n in validate_team_graph(ma.preset("multimodal_review"))["nodes"] if n["id"] == "vision")
    assert a["modalities"] == ["image", "video"]


def test_coerce_output_pattern_fields():
    out = ma.coerce_output("plan\n<<TASK:w>>dig A<</TASK>><<TASK:w>>dig B<</TASK>>\n<<HANDOFF:billing>>\n<<TRIPWIRE:pii leak>>")
    assert out.tasks == [{"agent": "w", "task": "dig A"}, {"agent": "w", "task": "dig B"}]
    assert out.handoff == "billing" and out.tripwire and out.tripwire_reason == "pii leak" and out.content == "plan"
    d = ma.coerce_output(_out("x", handoff="tech", tasks=[{"agent": "w", "task": "t"}, {"agent": "w", "task": " "}],
                              tripwire=True, tripwire_reason="r"))
    assert d.handoff == "tech" and d.tasks == [{"agent": "w", "task": "t"}] and d.tripwire and d.tripwire_reason == "r"
    assert ma.coerce_output(_out("x", tripwire="yes")).tripwire is False


def test_node_schema_only_exposes_usable_fields():
    g_ = validate_team_graph(ma.preset("handoff_router"))
    triage = next(n for n in g_["nodes"] if n["id"] == "triage")
    sch = ma.node_schema(g_, triage)
    assert sch["properties"]["handoff"]["enum"] == ["", "tech", "billing", "general"] and "handoff" in sch["required"]
    assert ma.node_schema(g_, next(n for n in g_["nodes"] if n["id"] == "tech")) is ma.THINK_SCHEMA
    o = validate_team_graph(ma.preset("orchestrator_workers"))
    lead = next(n for n in o["nodes"] if n["id"] == "lead")
    assert ma.node_schema(o, lead)["properties"]["tasks"]["items"]["properties"]["agent"]["enum"] == ["worker"]
    gr = validate_team_graph(ma.preset("guardrails"))
    guard = next(n for n in gr["nodes"] if n["id"] == "input_guard")
    assert {"tripwire", "tripwire_reason"} <= set(ma.node_schema(gr, guard)["required"])


class FakeMedia(ma.MediaHooks):
    def __init__(self, metas):
        self.by_id = {m["id"]: m for m in metas}
        self.prepared = []

    def lookup(self, ids):
        return [self.by_id[i] for i in ids]

    def prepare(self, metas):
        self.prepared.append([m["id"] for m in metas])

    def describe(self, metas):
        return "\n".join(f"FILE<{m['name']}>" for m in metas)


def _mid(n):
    return f"{n:08d}-0000-4000-8000-000000000000"


MEDIA = [{"id": _mid(1), "name": "shot.png", "mime": "image/png", "modality": "image", "size": 10},
         {"id": _mid(2), "name": "demo.mp4", "mime": "video/mp4", "modality": "video", "size": 10},
         {"id": _mid(3), "name": "call.m4a", "mime": "audio/mp4", "modality": "audio", "size": 10},
         {"id": _mid(4), "name": "spec.pdf", "mime": "application/pdf", "modality": "pdf", "size": 10}]


def test_handoff_transfers_control_and_carries_attachments():
    media = FakeMedia(MEDIA)

    def behave(node, prompt, ctx):
        if node["id"] == "triage":
            assert ctx["schema"]["properties"]["handoff"]["enum"][0] == ""
            return _out("결제 문제로 보임", handoff="billing")
        return _out(f"{node['id']} answered")

    res, calls, events = _run(ma.preset("handoff_router"), behave, attachments=[MEDIA[0]], media=media)
    assert [c[0] for c in calls] == ["triage", "billing"]
    assert res["result"]["outputs"]["result"] == ["billing answered"]   # triage's own edge to result did not fire
    assert any(e["type"] == "handoff" and e["target"] == "billing" for e in events)
    billing_prompt = calls[1][2]
    assert "FILE<shot.png>" in billing_prompt and "결제 문제로 보임" in billing_prompt
    assert res["node_state"]["tech"]["status"] == "skipped"


def test_handoff_to_unknown_target_is_rejected_and_normal_edges_fire():
    res, calls, events = _run(ma.preset("handoff_router"), lambda n, p, c: _out("hi there", handoff="ghost"))
    assert [c[0] for c in calls] == ["triage"] and res["result"]["outputs"]["result"] == ["hi there"]
    assert any(e["type"] == "handoff_rejected" for e in events)


def test_orchestrator_spawns_parallel_workers_and_synthesizes():
    active, peak, lock = [0], [0], threading.Lock()

    def behave(node, prompt, ctx):
        if node["id"] == "lead":
            if ctx["run_no"] == 1:
                assert "tasks" in ctx["schema"]["properties"]
                return _out("plan", tasks=[{"agent": "worker", "task": f"topic {i}"} for i in range(3)] +
                            [{"agent": "citer", "task": "not a worker"}])
            assert "하위 작업 결과" in prompt and all(f"found topic {i}" in prompt for i in range(3))
            return _out("FINAL REPORT", tasks=[])
        if node["id"] == "worker":
            assert ctx["subtask"]["parent"] == "lead" and "배정된 하위 작업" in prompt and "팀 전체 요청" in prompt
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.15)
            with lock:
                active[0] -= 1
            task = prompt.split("## 배정된 하위 작업\n", 1)[1].split("\n", 1)[0]
            return _out(f"found {task}")
        assert "FINAL REPORT" in prompt
        return _out("REPORT + citations")

    res, calls, events = _run(ma.preset("orchestrator_workers"), behave)
    assert peak[0] == 3
    assert [c[0] for c in calls].count("worker") == 3 and [c[0] for c in calls].count("citer") == 1
    assert res["status"] == "SUCCEEDED" and res["result"]["outputs"]["result"] == ["REPORT + citations"]
    types_ = [e["type"] for e in events]
    assert types_.count("task_assigned") == 3 and "task_rejected" in types_ and "subtasks_returned" in types_
    assert "worker" not in res["result"]["outputs"]


def test_worker_failure_is_reported_back_to_orchestrator():
    def behave(node, prompt, ctx):
        if node["id"] == "lead":
            return _out("p", tasks=[{"agent": "worker", "task": "a"}, {"agent": "worker", "task": "b"}]) \
                if ctx["run_no"] == 1 else _out("final with gaps", tasks=[])
        if node["id"] == "worker" and "\na\n" in prompt + "\n":
            raise RuntimeError("CLI crashed")
        return _out("ok")

    res, calls, events = _run(ma.preset("orchestrator_workers"), behave)
    lead2 = [c for c in calls if c[0] == "lead"][1][2]
    assert "하위 작업 실패" in lead2 and res["status"] == "PARTIAL" and res["failures"] == ["worker"]


def test_input_guardrail_tripwire_blocks_the_run():
    def behave(node, prompt, ctx):
        if node["id"] == "input_guard":
            return _out("contains SSN", tripwire=True, tripwire_reason="개인정보 포함")
        return _out(node["id"])

    res, calls, events = _run(ma.preset("guardrails"), behave)
    assert res["status"] == "BLOCKED" and [c[0] for c in calls] == ["input_guard"]
    assert res["result"]["blocked"] == {"node": "input_guard", "reason": "개인정보 포함"}
    assert any(e["type"] == "guardrail_tripped" for e in events)

    ok, calls, _ = _run(ma.preset("guardrails"), lambda n, p, c: _out(f"OUT-{n['id']}", tripwire=False, tripwire_reason=""))
    assert ok["status"] == "SUCCEEDED" and [c[0] for c in calls] == ["input_guard", "assistant", "output_guard"]


def test_output_key_state_flows_into_templates_and_result():
    def behave(node, prompt, ctx):
        if node["id"] == "critic":
            assert "## 초안 (state:draft)\nDRAFT-1" in prompt
        if node["id"] == "refiner":
            assert "DRAFT-1" in prompt and "CRIT-1" in prompt
        return _out({"writer": "DRAFT-1", "critic": "CRIT-1", "refiner": "FINAL-1"}[node["id"]])

    res, _, events = _run(ma.preset("sequential_state"), behave)
    assert res["result"]["state"] == {"draft": "DRAFT-1", "critique": "CRIT-1", "final": "FINAL-1"}
    assert [e["meta"]["key"] for e in events if e["type"] == "state_set"] == ["draft", "critique", "final"]
    sys_graph = validate_team_graph(g([{"id": "i", "kind": "input"}, agent("a", system_prompt="use {{state:k}}")],
                                      [{"from": "i", "to": "a"}]))
    assert "use v1" in ma.build_prompt(sys_graph, sys_graph["nodes"][1], {}, "", [], 1, {"k": "v1"})


def test_multimodal_attachments_routed_by_modality_and_prepared_once():
    media = FakeMedia(MEDIA)
    graph = ma.preset("multimodal_review")
    graph["nodes"][0]["attachments"] = [MEDIA[3]["id"]]          # default attachment saved on the input node
    seen = {}

    def behave(node, prompt, ctx):
        seen[node["id"]] = ([m["name"] for m in ctx["attachments"]], prompt)
        return _out(f"{node['id']} done")

    res, calls, events = _run(graph, behave, attachments=MEDIA[:3], media=media)
    assert res["status"] == "SUCCEEDED"
    assert media.prepared == [[m["id"] for m in MEDIA[:3]] + [MEDIA[3]["id"]]]
    assert seen["vision"][0] == ["shot.png", "demo.mp4"]
    assert seen["audio"][0] == ["demo.mp4", "call.m4a"]
    assert seen["docs"][0] == ["spec.pdf"]
    assert seen["synth"][0] == []                                  # modalities=[]: analyses only
    assert "## 첨부 파일 (멀티모달 입력)\nFILE<shot.png>\nFILE<demo.mp4>" in seen["vision"][1]
    assert "첨부 파일" not in seen["synth"][1]
    assert any(e["type"] == "media_ready" for e in events)


def test_files_data_node_and_media_failures_are_visible():
    media = FakeMedia(MEDIA)
    graph = {"nodes": [{"id": "f", "kind": "data", "source": "files",
                        "params": {"attachments": [MEDIA[0]["id"]], "note": "디자인 시안"}}, agent("a")],
             "edges": [{"from": "f", "to": "a"}]}
    res, calls, events = _run(graph, lambda n, p, c: _out(str([m["name"] for m in c["attachments"]])), media=media)
    assert res["result"]["outputs"]["a"] == ["['shot.png']"] and "디자인 시안" in calls[0][2]

    class Broken(FakeMedia):
        def prepare(self, metas):
            raise RuntimeError("VM disk full")

    res, calls, events = _run(graph, lambda n, p, c: _out("ok"), media=Broken(MEDIA))
    assert res["status"] == "SUCCEEDED" and any(e["type"] == "media_failed" and "disk full" in e["content"] for e in events)


# ---------------------------------------------------------------- attachments service + CLI delivery
def _upload(svc, ws, name, mime, raw, chunk=None):
    if chunk:
        svc.CHUNK_BYTES = chunk
    init = svc.create_attachment(ws, name, mime, len(raw), None)
    import base64 as b64
    for i in range(init["chunks"]):
        part = raw[i * init["chunk_size"]:(i + 1) * init["chunk_size"]]
        svc.put_chunk(ws, init["id"], i, b64.b64encode(part).decode())
    return svc.complete_attachment(ws, init["id"])


def test_chunked_attachment_upload_roundtrip_and_validation(fake_db, monkeypatch):
    import base64 as b64
    import hashlib
    db, svc = fake_db
    monkeypatch.setattr(svc, "CHUNK_BYTES", 5)
    raw = b"hello multimodal world"
    meta = _upload(svc, "ws-1", "../../etc/notes.md", "text/markdown", raw)
    assert meta["name"] == "notes.md" and meta["modality"] == "text" and meta["status"] == "READY"
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest() and b"".join(svc.iter_chunks(meta["id"])) == raw
    got = svc.get_attachments("ws-1", [meta["id"]])[0]
    assert got["excerpt"] == raw.decode()
    with pytest.raises(LookupError):
        svc.get_attachments("ws-2", [meta["id"]])                  # other workspace
    init = svc.create_attachment("ws-1", "clip.mp4", "video/mp4", 12, None)
    assert init["modality"] == "video" and init["chunks"] == 3
    with pytest.raises(ValueError, match="expected 5 bytes"):
        svc.put_chunk("ws-1", init["id"], 0, b64.b64encode(b"abc").decode())
    with pytest.raises(ValueError, match="incomplete"):
        svc.complete_attachment("ws-1", init["id"])
    with pytest.raises(ValueError, match="최대"):
        svc.create_attachment("ws-1", "huge.bin", "", svc.MAX_ATTACHMENT_BYTES + 1, None)
    assert svc.create_attachment("ws-1", "x", "Not A Mime", 1, None)["mime"] == "application/octet-stream"


def test_service_run_with_attachments_without_vm(fake_db, monkeypatch):
    db, svc = fake_db
    from irade_agent.agdd import infra
    monkeypatch.setattr(svc, "CHUNK_BYTES", 1024)
    note = _upload(svc, "ws-1", "req.txt", "text/plain", "로그인 버튼이 안 눌림".encode())
    img = _upload(svc, "ws-1", "screen.png", "image/png", b"\x89PNG fake")
    prompts, gw = {}, []

    def fake_llm(task, ws=None, run=None, node=None):
        def call(prompt, schema):
            prompts[node] = prompt
            return _out(f"text-only {node}")
        return call

    def fake_gateway(ws, node, prompt, schema, metas):
        gw.append((node["id"], [m["name"] for m in metas]))
        return _out(f"saw image {node['id']}")

    monkeypatch.setattr(infra, "cheap_model", fake_llm)
    monkeypatch.setattr(svc, "_gateway_multimodal", fake_gateway)
    graph = {"nodes": [{"id": "req", "kind": "input"}, agent("vision", modalities=["image"]),
                       agent("reader", modalities=["text"]), {"id": "out", "kind": "output"}],
             "edges": [{"from": "req", "to": "vision"}, {"from": "req", "to": "reader"},
                       {"from": "vision", "to": "out"}, {"from": "reader", "to": "out"}]}
    rid = svc.start_run("ws-1", None, graph, "버그 분석", None, [note["id"], img["id"]])["team_run_id"]
    for _ in range(200):
        if db["runs"][rid]["status"] != "RUNNING":
            break
        time.sleep(0.02)
    assert db["runs"][rid]["status"] == "SUCCEEDED"
    assert gw == [("vision", ["screen.png"])]
    assert "로그인 버튼이 안 눌림" in prompts["reader"] and "screen.png" not in prompts["reader"]
    with pytest.raises(LookupError):
        svc.start_run("ws-1", None, graph, "x", None, ["99999999-0000-4000-8000-000000000000"])


def test_cli_profiles_receive_media_files():
    from irade_agent.agdd.cli_agents import PROFILES, MediaFile
    files = [MediaFile("/workspace/.agdd/attachments/ab/shot.png", "image"),
             MediaFile("/workspace/.agdd/attachments/cd/spec.pdf", "pdf"),
             MediaFile("/workspace/.agdd/attachments/cd/spec.pdf.agdd/page-1.png", "image", False),
             MediaFile("/workspace/.agdd/attachments/ef/demo.mp4", "video")]
    c = PROFILES["claude"].structured_cmd("/p", "/s", "haiku", "low", files)
    assert "--tools Read --allowedTools Read" in c and "--add-dir '/workspace/.agdd/attachments/ab'" in c
    assert '--tools ""' in PROFILES["claude"].structured_cmd("/p", "/s", "haiku", "low")
    x = PROFILES["codex"].structured_cmd("/p", "/s", "gpt-5-mini", "low", files)
    assert "--image='/workspace/.agdd/attachments/ab/shot.png'" in x and "page-1.png" in x and "demo.mp4" not in x
    assert x.index("--image=") < x.index(" - < ")
    sfx = PROFILES["agy"].prompt_suffix(files)
    assert "@/workspace/.agdd/attachments/ef/demo.mp4" in sfx and "page-1.png" not in sfx


def test_vm_media_streams_once_and_describes_paths(tmp_path, monkeypatch):
    from fake_runtime import FakeRuntime
    from irade_agent.agdd import multi_agent_service as svc, vm_service
    import hashlib
    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    monkeypatch.setenv("AGORA_RUNTIME_SOCKET", str(tmp_path / "m.sock"))
    monkeypatch.setattr(vm_service, "canvas_for", lambda ws: 1900000003)
    home = tmp_path / "vms" / "1900000003"
    home.mkdir(parents=True)
    from irade_agent.agdd import vm as vm_mod
    real_init = vm_mod.VmShell.__init__
    monkeypatch.setattr(vm_mod.VmShell, "__init__", lambda self, c, cid, name="agdd", **kw:
                        real_init(self, c, cid, name, home=str(home)))
    data = {"i1": b"\x89PNG" + bytes(range(256)) * 20, "t1": "제목\n본문".encode()}
    pulls = []

    def chunks(aid):
        pulls.append(aid)
        yield data[aid]

    monkeypatch.setattr(svc, "iter_chunks", chunks)
    metas = [{"id": "i1", "name": "s c/r.png", "mime": "image/png", "modality": "image", "size": len(data["i1"]),
              "sha256": hashlib.sha256(data["i1"]).hexdigest()},
             {"id": "t1", "name": "n.md", "mime": "text/markdown", "modality": "text", "size": len(data["t1"]),
              "sha256": hashlib.sha256(data["t1"]).hexdigest(), "excerpt": "제목\n본문"}]
    try:
        m = svc.VmMedia("ws", use_vm=True)
        m.prepare(metas)
        img_path = m.info["i1"]["path"]
        assert img_path.endswith(f"/.agdd/attachments/{metas[0]['sha256']}/r.png")  # directory parts dropped
        assert (home / img_path[len(str(home)) + 1:]).read_bytes() == data["i1"]
        desc = m.describe(metas)
        assert img_path in desc and "제목" in desc
        kinds = [(f.kind, f.native) for f in m.files_for(metas)]
        assert ("image", True) in kinds and ("text", True) in kinds
        m2 = svc.VmMedia("ws", use_vm=True)
        m2.prepare(metas)                                         # cached by sha256: nothing re-uploaded
        assert pulls == ["i1", "t1"] and m2.info["i1"]["path"] == img_path
    finally:
        rt.stop()


def test_attachment_routes_and_raw_download(fake_db, monkeypatch):
    from irade_agent.agdd import api
    db, svc = fake_db
    monkeypatch.setattr(svc, "CHUNK_BYTES", 4)
    init = api.team_attachment_create({"user": None}, "ws", {"name": "a.svg", "mime": "image/svg+xml", "size": 6})
    import base64 as b64
    api.team_attachment_chunk({}, "ws", {"data": b64.b64encode(b"<svg").decode()}, init["id"], "0")
    with pytest.raises(api.HttpError) as ei:
        api.team_attachment_chunk({}, "ws", {"data": "!!notb64"}, init["id"], "1")
    assert ei.value.code == 422
    api.team_attachment_chunk({}, "ws", {"data": b64.b64encode(b"/>").decode()}, init["id"], "1")
    meta = api.team_attachment_complete({}, "ws", {}, init["id"])
    raw = api.team_attachment_raw({}, "ws", {}, meta["id"])
    assert isinstance(raw, api.RawResponse) and raw.content_type == "application/octet-stream" and not raw.inline
    assert b"".join(raw.chunks) == b"<svg/>"                     # SVG is never served inline (script risk)
    assert [a["id"] for a in api.team_attachments_list({}, "ws", {})["attachments"]] == [meta["id"]]
    assert api.team_attachment_delete({}, "ws", {}, meta["id"]) == {"ok": True}
    with pytest.raises(api.HttpError) as ei:
        api.team_attachment_raw({}, "ws", {}, meta["id"])
    assert ei.value.code == 404
