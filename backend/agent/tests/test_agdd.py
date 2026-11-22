"""Offline tests for the AGDD MVP package (DOC.md)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from irade_agent.agdd import budget, gateway, orchestrator, preflight, router, semantic_verify
from irade_agent.agdd.seed import PLATFORM_SNAPSHOT
from irade_agent.agdd.spec_engine import SpecEngine
from irade_agent.agdd.worker_db import WorkerDB


def no_conflicts(prompt, schema):
    return {"conflicts": []}


@pytest.fixture
def eng():
    e = SpecEngine.hydrate(PLATFORM_SNAPSHOT)
    yield e
    e.close()


def test_seed_passes_gate_and_scope_excludes_defer(eng):
    snaps = []
    res = preflight.run_gate(eng, no_conflicts, lambda s, ok: snaps.append(ok))
    assert res.passed and snaps == [True]
    ids = {r["req_id"] for r in res.scope["requirements"]}
    assert ids == {"REQ-001", "REQ-002", "REQ-003"}
    assert all(a["adoption_status"] == "ACCEPT_NOW" for a in res.scope["architecture"])
    assert "ARC-004" not in {a["arch_id"] for a in res.scope["architecture"]}


def test_adoption_mismatch_blocks_and_skips_llm(eng):
    eng.set_adoption("SRS", "REQ-004", "ACCEPT_NOW")
    calls = []
    res = preflight.run_gate(eng, lambda p, s: calls.append(p) or {"conflicts": []})
    assert not res.passed and calls == []
    types = {(c["conflict_type"], tuple(c["involved_ids"])) for c in res.critical}
    assert ("ADOPTION_MISMATCH", ("REQ-004", "ARC-004")) in types
    assert ("ADOPTION_MISMATCH", ("REQ-004", "SCH-004")) in types
    assert len(eng.open_conflicts("CRITICAL")) == len(res.critical)


def test_orphan_and_duplicate_schema(eng):
    eng.upsert("SRS", dict(PLATFORM_SNAPSHOT["srs"][0], req_id="REQ-099"))
    eng.upsert("SCH", dict(PLATFORM_SNAPSHOT["sch"][0], schema_id="SCH-099", columns_and_ddl_json={"x": 1}))
    kinds = {c["conflict_type"] for c in preflight.sql_checks(eng)}
    assert {"ORPHAN_SPEC", "DUPLICATE_SCHEMA"} <= kinds


def test_semantic_stage_filters_hallucinated_ids(eng):
    def llm(prompt, schema):
        return {"conflicts": [
            {"conflict_type": "REQ_VS_REQ", "severity": "CRITICAL", "involved_ids": ["REQ-001", "REQ-777"],
             "explanation": "x", "resolution_options": ["a", "b"]},
            {"conflict_type": "REQ_VS_REQ", "severity": "CRITICAL", "involved_ids": ["REQ-777"],
             "explanation": "ghost", "resolution_options": ["a"]}]}
    res = preflight.run_gate(eng, llm)
    assert not res.passed
    assert all("REQ-777" not in c["involved_ids"] for c in res.conflicts)


def test_batches_bounded(eng):
    for b in preflight.build_batches(eng):
        assert 1 <= len(b) <= 15 and all(t in ("SRS", "ARC", "SCH") for t, _ in b)


def test_write_through_and_version(eng):
    events = []
    eng.sink = lambda e, p: events.append((e, p))
    eng.set_adoption("SRS", "REQ-006", "HOLD")
    assert events[-1][1]["row"]["version"] == 2
    again = SpecEngine.hydrate(eng.snapshot())
    assert again.get("SRS", "REQ-006")["adoption_status"] == "HOLD"


def test_router():
    assert router.decide_route(20, ["a.py"]).route_mode == "FAST_TRACK"
    assert router.decide_route(31, ["a.py"]).route_mode == "FULL_AGDD"
    assert router.decide_route(5, ["a", "b", "c"]).route_mode == "FULL_AGDD"
    assert router.decide_route(5, ["auth/login.py"]).route_mode == "FULL_AGDD"
    assert router.decide_route(5, ["m.sql"], text="ALTER TABLE x").route_mode == "FULL_AGDD"


def test_ambiguity_gate_requires_choices():
    out = router.ambiguity_gate([{"req_id": "REQ-001"}], lambda p, s: {"questions": [
        {"spec_id": "REQ-001", "category": "CONCURRENCY", "question": "q", "options": ["a", "b"], "recommended": 9},
        {"spec_id": "REQ-001", "category": "PERMISSION", "question": "q", "options": ["only"], "recommended": 0}]})
    assert len(out) == 1 and out[0]["recommended"] == 0


def test_budget():
    assert budget.check_budget(100, 10, 1, "mid", "cheap").action == "ALLOW"
    d = budget.check_budget(100, 85, 1, "mid", "cheap")
    assert d.action == "FALLBACK" and d.model == "cheap"
    assert budget.check_budget(100, 99.5, 1, "mid", "cheap").action == "HALT"


def test_output_parser_and_cap():
    assert gateway.parse_tool_output("pytest", 0, "a\nb\n3 passed").startswith("OK `pytest`")
    log = "\n".join(f"line {i}" for i in range(500)) + "\nAssertionError: boom\n"
    out = gateway.parse_tool_output("pytest", 1, log)
    assert out.startswith("EXIT 1") and "AssertionError" in out and len(out.splitlines()) <= 32
    assert gateway.estimate_tokens(gateway.hard_cap("x" * 100000)) <= gateway.HARD_CAP_TOKENS


def test_batch_clear_keeps_recent_and_tool_use():
    big = "y" * 8000
    msgs = []
    for i in range(30):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}"}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": big}]})
    out, n = gateway.batch_clear(msgs)
    assert n == 27
    assert out[-1]["content"][0]["content"] == big
    assert out[1]["content"][0]["content"].startswith("[CLEARED]")
    assert all(m["content"][0]["type"] == "tool_use" for m in out[0::2])
    small, n2 = gateway.batch_clear(msgs[:4])
    assert n2 == 0


SRC_V1 = '''
class Svc:
    def rotate(self, token):
        """doc"""
        if not token:
            raise ValueError("empty")
        return token[::-1]

def helper(x):
    return Svc().rotate(x)
'''


def test_worker_symbols_watermark_and_semantic_verify(tmp_path):
    (tmp_path / "svc.py").write_text(SRC_V1)
    w = WorkerDB(root=tmp_path)
    w.index_file("svc.py")
    assert [s["qualified_name"] for s in w.find_symbol("rotate")] == ["Svc.rotate"]
    assert "def rotate" in w.read_symbol("Svc.rotate")
    assert w.read_file("svc.py").startswith("1: ")
    assert w.read_file("svc.py").startswith("[UNCHANGED")

    lifted = {"preconditions": ["token non-empty"], "state_transitions": [], "side_effects": [],
              "failure_contracts": ["ValueError on empty"], "invariants": []}
    calls = []

    def llm(prompt, schema):
        calls.append(prompt)
        return lifted

    contracts = {"Svc.rotate": lifted}
    r1 = semantic_verify.verify_changed(w, ["svc.py"], contracts, llm)
    assert r1.ok and r1.llm_calls == 2  # Svc.rotate + helper
    # whitespace/comment-only change -> same ast_hash -> 0 LLM calls
    (tmp_path / "svc.py").write_text(SRC_V1.replace("return token[::-1]", "return token[::-1]  # c"))
    r2 = semantic_verify.verify_changed(w, ["svc.py"], contracts, llm)
    assert r2.llm_calls == 0
    assert "+" in w.read_file("svc.py")  # diff only
    # real change + drift
    (tmp_path / "svc.py").write_text(SRC_V1.replace('raise ValueError("empty")', "return None"))
    contracts["Svc.rotate"] = dict(lifted, failure_contracts=["ValueError on empty", "must log"])
    r3 = semantic_verify.verify_changed(w, ["svc.py"], contracts, llm)
    assert r3.llm_calls == 1 and not r3.ok
    assert r3.violations[0].qualified_name == "Svc.rotate"
    assert "svc.py:" in r3.violations[0].location

    oid = w.offload("bash", "pytest", "alpha beta\nunique_marker failure here")
    assert w.search_outputs("unique_marker", output_id=oid)
    w.set_state("phase", {"node": "DEV"})
    assert w.get_state("phase") == {"node": "DEV"} and "phase" in w.state_prompt()


GRAPH = {"entry": "dev", "nodes": [
    {"name": "dev", "kind": "DEV", "domain": "CODE_VERIFY", "allowed_mcp": ["edit"], "max_retries": 1},
    {"name": "verify", "kind": "SEMANTIC_VERIFY"}],
    "edges": [{"from": "dev", "to": "verify"}, {"from": "verify", "to": "dev", "on": "failure"}]}


def test_graph_validation_and_run(eng):
    with pytest.raises(orchestrator.GraphError):
        orchestrator.validate_graph({"entry": "x", "nodes": [], "edges": []})
    seen = []

    def execute(inp):
        seen.append(inp)
        return True, {"from": inp.node}
    res = orchestrator.GraphRun(GRAPH, eng).run(execute)
    assert res["status"] == "SUCCEEDED" and [n for n, _ in res["trail"]] == ["dev", "verify"]
    assert [r["req_id"] for r in seen[0].scope["requirements"]] == ["REQ-003"]
    assert seen[1].handoff == {"from": "dev"}

    res2 = orchestrator.GraphRun(GRAPH, eng).run(lambda i: (i.node == "dev", {}))
    assert res2["status"] == "ESCALATED"


def test_batch_clear_openai_tool_messages():
    msgs = []
    for i in range(30):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "z" * 8000})
    out, n = gateway.batch_clear(msgs)
    assert n == 27 and out[1]["content"].startswith("[CLEARED] c0") and out[-1]["content"] == "z" * 8000
    assert all(m.get("tool_calls") for m in out[0::2])


def test_route_request_retries_then_falls_back():
    calls = []
    good = {"est_lines": 3, "files": ["a.py"], "touches_ddl": False, "touches_auth": False, "touches_payment": False}
    seq = [{}, good]
    assert router.route_request("x", lambda p, s: calls.append(1) or seq.pop(0)).route_mode == "FAST_TRACK"
    assert len(calls) == 2
    assert router.route_request("x", lambda p, s: {}).reasons == ["size estimate unavailable"]


def test_preflight_coalesces_batches_and_checks_bidirectional_adoption(eng):
    # All 3 small domains in PLATFORM_SNAPSHOT fit into a single semantic batch.
    assert len(preflight.build_batches(eng)) == 1

    # Reverse traceability direction (ARC -> SRS): ACCEPT_NOW SRS linked from DEFER ARC.
    eng.trace("ARC", "ARC-004", "SRS", "REQ-001", "IMPLEMENTS")
    types = {(c["conflict_type"], tuple(sorted(c["involved_ids"]))) for c in preflight.sql_checks(eng)}
    assert ("ADOPTION_MISMATCH", ("ARC-004", "REQ-001")) in types


def test_semantic_checks_handles_weird_recommended_and_sequential_workers(eng):
    def llm(prompt, schema):
        return {
            "conflicts": [
                {
                    "conflict_type": "REQ_VS_REQ",
                    "severity": "WARNING",
                    "involved_ids": ["REQ-001", "REQ-002"],
                    "explanation": "edge case",
                    "resolution_options": ["opt A", "opt B"],
                    "recommended": None,
                },
                {
                    "conflict_type": "REQ_VS_ARC",
                    "severity": "WARNING",
                    "involved_ids": ["REQ-001", "ARC-002"],
                    "explanation": "bool rec",
                    "resolution_options": ["first", "second"],
                    "recommended": True,
                },
            ]
        }

    out = preflight.semantic_checks(eng, llm, workers=1)
    assert len(out) == 2
    assert all(c["resolution_options"]["recommended"] == 0 for c in out)
