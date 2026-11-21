"""Offline tests for AGDD PHASE_2/3 modules (DOC.md §5.4-5.7, REQ-004/005/006)."""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from irade_agent.agdd import hitl, reporters, research, semantic_cache, vlm_screen, watchdog
from irade_agent.agdd.ux_workbench import LockedOverrideError, MemoryOverrideStore, Workbench, audit_patch


# ---------------- UX workbench ----------------
def test_ux_lock_blocks_agent_and_audit():
    wb = Workbench(MemoryOverrideStore())
    wb.user_set("w", "home", "#hero", {"layout": {"cols": 2}}, "u1")
    with pytest.raises(LockedOverrideError):
        wb.agent_set("w", "home", "#hero", {"layout": {"cols": 3}}, "agent")
    wb.agent_set("w", "home", "#footer", {"spacing": 8}, "agent")
    assert wb.export_bindings("w", "home") == {"#footer": {"spacing": 8}, "#hero": {"layout": {"cols": 2}}}
    diff = "--- a/x.tsx\n+++ b/x.tsx\n-<div id=\"#hero\" cols=2>\n+<div id=\"#hero\" cols=3>\n"
    assert audit_patch(wb, "w", "home", diff).blocked == ["#hero"]
    assert audit_patch(wb, "w", "home", "+++ b/y.tsx\n+<p/>\n").ok
    with pytest.raises(LockedOverrideError):
        wb.import_bindings("w", "home", {"#hero": {"layout": {"cols": 9}}}, "u2")
    wb.unlock("w", "home", "#hero", "u1")
    wb.agent_set("w", "home", "#hero", {"layout": {"cols": 3}}, "agent")
    with pytest.raises(ValueError):
        wb.user_set("w", "home", "#x", {"evil": 1}, "u1")


# ---------------- watchdog ----------------
def diff(path, minus, plus):
    return f"--- a/{path}\n+++ b/{path}\n-{minus}\n+{plus}\n"


def test_watchdog_ping_pong_cheat_deadlock_and_protection():
    wd = watchdog.Watchdog()
    assert wd.record(False, diff("a.py", "x = 1", "x = 2"), "E1").verdict == "CONTINUE"
    v = wd.record(False, diff("a.py", "x = 2", "x = 1"), "E2")
    assert v.verdict == "PING_PONG" and v.escalate_to == "ADVISOR"

    wd = watchdog.Watchdog()
    wd.record(False, diff("a.py", "a", "b"), "AssertionError")
    v = wd.record(True, "--- a/a.py\n+++ b/a.py\n+@pytest.mark.skip\n", "")
    assert v.verdict == "CHEAT"

    assert watchdog.Watchdog().record(False, diff("tests/test_a.py", "assert x", "assert True"), "").verdict == "CHEAT"

    wd = watchdog.Watchdog()
    for i in range(3):
        v = wd.record(False, diff(f"f{i}.py", f"o{i}", f"n{i}"), f"KeyError: 'k{i}' at line {i}")
    assert v.verdict == "DEADLOCK" and v.escalate_to == "RESEARCH"
    assert watchdog.readonly_mounts("/r", ["src/a.py", "tests/t.py"]) == ["/r/tests/t.py:/workspace/tests/t.py:ro"]


def test_watchdog_llm_gets_only_diff_and_two_errors():
    seen = []

    def llm(prompt, schema):
        seen.append(json.loads(prompt[prompt.index("{"):]))
        return {"verdict": "CONTINUE", "reason": "", "escalate_to": "NONE"}
    wd = watchdog.Watchdog(llm)
    for i in range(3):
        wd.record(False, diff(f"f{i}.py", f"o{i}", f"n{i}"), f"Err{i}: different {i}")
    assert set(seen[-1]) == {"git_diff", "errors"} and len(seen[-1]["errors"]) == 2


# ---------------- HITL ----------------
def test_hitl_classify_compress_dispatch():
    assert hitl.classify("X", "+++ b/m.sql\n+DROP TABLE users;\n")[0] == "IRREVERSIBLE"
    assert hitl.classify("X", "+++ b/a.py\n+def auth_check(): pass\n")[1] == "AUTH_CHANGE"
    assert hitl.classify("X", "+++ b/m.sql\n+CREATE INDEX CONCURRENTLY i ON t(a);\n")[0] == "REVERSIBLE"
    assert hitl.classify("CRITICAL_SPEC_CONFLICT")[0] == "IRREVERSIBLE"
    reqs = [{"id": 1, "reversibility": "REVERSIBLE", "files": ["svc/a.py"]},
            {"id": 2, "reversibility": "REVERSIBLE", "files": ["svc/b.py"]},
            {"id": 3, "reversibility": "IRREVERSIBLE", "files": ["db/m.sql"]}]
    calls = []
    cards = hitl.compress(reqs, lambda p, s: calls.append(p) or {"impact": ["a", "b", "c", "d"], "options": [
        {"label": "A"}, {"label": "B"}], "recommended": 5})
    assert len(cards) == 2 and len(calls) == 2
    assert all(len(c["impact"]) <= 3 and c["recommended"] == 0 for c in cards)
    d = hitl.dispatch(cards, datetime(2026, 11, 20, 11, 0))
    assert len(d.push_now) == 1 and d.digest_at == datetime(2026, 11, 20, 16, 0)
    assert hitl.next_digest(datetime(2026, 11, 20, 17, 0)) == datetime(2026, 11, 21, 10, 0)


# ---------------- research ----------------
class FakeProvider:
    name = "fake"

    def __init__(self):
        self.queries = []

    def search(self, query, limit):
        self.queries.append(query)
        return [research.Source("Lock-free queues", "https://arxiv.org/abs/1", "deadlock avoidance in lock ordering", "fake"),
                research.Source("PG docs", "https://postgresql.org/docs", "SKIP LOCKED avoids deadlock contention", "fake")]


class Boom:
    name = "boom"

    def search(self, q, l):
        raise RuntimeError("down")


def test_research_sanitizes_reuses_and_cites():
    p = FakeProvider()
    llm = lambda prompt, schema: {"hypotheses": ["lock order"], "tradeoffs": ["latency"],
                                  "principles": [{"claim": "use SKIP LOCKED", "source": 1},
                                                 {"claim": "made up", "source": 99}]}
    eng = research.ResearchEngine([p, Boom()], llm, research.hash_embed, internal_names=["frelog"])
    problem = ("psycopg 3.2.9 DeadlockDetectedError in /srv/frelog/app/queue.py api_key=sk-abcdefghijklmnop "
               "user kim@example.com host 10.0.0.3 frelog worker")
    out = eng.run(problem)
    q = p.queries[0]
    assert "frelog" not in q.lower() and "sk-" not in q and "@" not in q and "10.0.0.3" not in q
    assert "DeadlockDetectedError" in q
    brief = out["insight_brief_md"]
    assert "[2]" in brief and "made up" not in brief
    assert "https://arxiv.org/abs/1" in brief and "https://postgresql.org/docs" in brief
    assert research.estimate_tokens(brief) <= research.BRIEF_MAX_TOKENS
    assert out["errors"]
    again = eng.run(problem)
    assert again["reused"] and len(p.queries) == 1


# ---------------- reporters ----------------
JUNIT = """<testsuite><testcase classname="t" name="ok"/><testcase classname="t" name="bad">
<failure message="expected 1">trace</failure></testcase></testsuite>"""
SARIF = json.dumps({"runs": [{"results": [{"ruleId": "sqli", "level": "error", "message": {"text": "SQL injection"},
                                           "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"},
                                                                               "region": {"startLine": 3}}}]}]}]})
K6 = json.dumps({"metrics": {"http_req_duration": {"values": {"p(95)": 420.0}}}})
EXPLAIN = json.dumps([{"Plan": {"Node Type": "Seq Scan", "Relation Name": "orders", "Plan Rows": 500000,
                                "Total Cost": 9000.0}}])


def test_reporters_budgets():
    b = reporters.Budgets(p95_ms=300, explain_cost=1000, bundle_bytes=100)
    res = reporters.collect({"junit": JUNIT, "sarif": SARIF, "k6": K6, "explain": EXPLAIN,
                             "bundle": json.dumps({"total_bytes": 50})}, b)
    msgs = " ".join(f.message for f in res.findings)
    assert not res.passed and "expected 1" in msgs and "SQL injection" in msgs and "p95" in msgs
    assert "full table scan on orders" in msgs and "total cost" in msgs
    assert res.summary().startswith("VERIFY FAIL")
    with pytest.raises(ValueError):
        reporters.collect({"custom": "x"})


# ---------------- semantic cache ----------------
def test_semantic_cache_requires_same_schema_hash():
    calls = []
    cache = semantic_cache.SemanticCache(research.hash_embed, threshold=0.99)
    llm = cache.wrap("w", "classify", lambda p, s: calls.append(p) or {"n": len(calls)})
    assert llm("classify this request", {"a": 1}) == {"n": 1}
    assert llm("classify this request", {"a": 1}) == {"n": 1}
    assert llm("classify this request", {"a": 2}) == {"n": 2}
    assert len(calls) == 2


# ---------------- VLM screening ----------------
def test_vlm_screen_merges_and_filters():
    caps = [vlm_screen.Capture("mobile", b"png", None, [{"type": "BROKEN_IMAGE", "selector": "img.a"},
                                                       {"type": "UGLY", "selector": "x"}])]
    vlm = lambda prompt, imgs, schema: {"defects": [{"type": "OVERLAP", "selector": "#nav"},
                                                    {"type": "BROKEN_IMAGE", "selector": "img.a"},
                                                    {"type": "BAD_TASTE", "selector": "#x"}]}
    out = vlm_screen.screen(caps, vlm)
    assert sorted(d["type"] for d in out) == ["BROKEN_IMAGE", "OVERLAP"]


# ---------------- DEV agent loop ----------------
def test_dev_agent_tool_loop(tmp_path):
    import subprocess
    from irade_agent.agdd.dev_agent import DevTools, run_dev_agent
    from irade_agent.agdd.worker_db import WorkerDB

    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.email=a@b", "-c", "user.name=a",
                                                            "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    fix = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
    cheat = "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -4 +4 @@\n-    assert add(2, 3) == 5\n+    assert True\n"
    script = [("search_symbols", {"query": "add"}), ("read_symbol", {"qualified_name": "add"}),
              ("apply_patch", {"patch": cheat}), ("apply_patch", {"patch": fix}),
              ("run_tests", {}), ("finish", {"summary": "fixed add", "done": True})]
    outputs = []

    def chat(messages, tools):
        if messages[-1]["role"] == "tool":
            outputs.append(messages[-1]["content"])
        name, args = script.pop(0)
        return {"content": "", "tool_calls": [{"id": name, "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}}]}
    tools = DevTools(repo, WorkerDB(root=repo), [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    res = run_dev_agent(chat, tools, {"request": "fix add"}, log=lambda m: None)
    assert res.ok and res.changed_files == ["calc.py"] and res.steps == 6
    assert "calc.py:1-2" in outputs[0] and "return a - b" in outputs[1]
    assert outputs[2].startswith("rejected: read-only test files")
    assert outputs[4].startswith("OK ")
    assert "a + b" in (repo / "calc.py").read_text()


def test_dev_tools_resolution_and_fresh_watermarks(tmp_path):
    import subprocess
    from irade_agent.agdd.dev_agent import DevTools
    from irade_agent.agdd.worker_db import WorkerDB

    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    w = WorkerDB(root=repo)
    t = DevTools(repo, w, ["true"])
    for name in ("add", "calc.add", "pkg.calc.add", "pkg/calc.py:add"):
        assert "return a - b" in t.read_symbol(name), name
    assert "pkg/calc.py" in t.list_files("**/*.py") and "pkg/calc.py" in t.list_files("*.py")
    assert "calc.py:2" in t.grep("a - b", path="pkg")
    t.read_file("pkg/calc.py")
    assert t.read_file("pkg/calc.py").startswith("[UNCHANGED")
    t2 = DevTools(repo, w, ["true"])  # next DEV attempt = fresh model context
    assert t2.read_file("pkg/calc.py").startswith("1: def add")
