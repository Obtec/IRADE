"""Token-saving strategies applied to the API agents (DOC.md §6)."""
import ast
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_gateway import FakeGateway, reply, tool_call
from irade_agent.agdd import api_agents, budget, gateway, infra, semantic_cache
from irade_agent.agdd.api_agents import AgentSettings, ApiAgent


class FakeValkey(dict):
    def get(self, k): return super().get(k)
    def set(self, k, v, ex=None): self[k] = v


@pytest.fixture
def cache(monkeypatch):
    kv = FakeValkey()
    monkeypatch.setattr(infra, "valkey", lambda: kv)
    return kv


def test_structured_uses_cheap_model_without_tools_and_exact_cache(monkeypatch, cache):
    gw = FakeGateway(structured={"ok": True})
    monkeypatch.setattr(api_agents, "chat_completion", gw)
    agent = ApiAgent(None, AgentSettings(model="advisor"), "ws-1")
    d1, r1 = agent.structured("same question", {"type": "object"})
    d2, r2 = agent.structured("same question", {"type": "object"})
    assert d1 == d2 == {"ok": True} and r2.model == "cache"
    assert len(gw.calls) == 1, "identical structured question must not call the model twice"
    body = gw.calls[0][0]
    assert body["model"] == "cheap" and "tools" not in body
    agent.structured("different question", {"type": "object"})
    assert len(gw.calls) == 2


def test_agent_tool_results_are_hard_capped(tmp_path, monkeypatch, cache):
    class Shell:
        home = str(tmp_path)
        mirror = False

        def run(self, cmd, timeout=600, **k):
            from irade_agent.agdd.vm import CommandResult
            return CommandResult(1, "Traceback\nerror: boom\n" + "x " * 40000)

        def read_file(self, path, max_bytes=0):
            raise FileNotFoundError(path)

        def put_file(self, path, content):
            pass

    gw = FakeGateway([reply("", [tool_call("run_command", {"command": "pytest -q"})]),
                      reply("", [tool_call("finish", {"summary": "s"})])])
    monkeypatch.setattr(api_agents, "chat_completion", gw)
    ApiAgent(Shell(), AgentSettings()).agent("t", "/r", None)
    tool_msg = [m for m in gw.calls[1][0]["messages"] if m["role"] == "tool"][0]["content"]
    assert tool_msg.startswith("EXIT 1") and gateway.estimate_tokens(tool_msg) <= gateway.HARD_CAP_TOKENS


def test_budget_thresholds_and_halt():
    assert budget.check_budget(Decimal("10"), Decimal("1"), Decimal("0.1"), "mid", "cheap").action == "ALLOW"
    assert budget.check_budget(Decimal("10"), Decimal("8"), Decimal("0.1"), "mid", "cheap").model == "cheap"
    assert budget.check_budget(Decimal("10"), Decimal("10"), Decimal("0.1"), "mid", "cheap").action == "HALT"


def _const(name):
    src = (Path(__file__).resolve().parents[1] / "irade_agent/agdd/worker_vm.py").read_text()
    return next(ast.literal_eval(n.value) for n in ast.parse(src).body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name)


def test_resume_prompt_sends_only_the_delta():
    p = _const("RESUME_PROMPT").format(test_cmd="pytest -q", feedback="1 failed: test_mean_empty", extra="")
    assert "1 failed" in p and "작업 지시서:" not in p and "scope" not in p


def test_exact_cache_wrapper_for_gateway_calls(cache):
    seen = []
    llm = semantic_cache.exact_cache("ws", "preflight", "gateway:cheap", lambda p, s: seen.append(p) or {"ok": True})
    assert llm("q", {"type": "object"}) == {"ok": True} and llm("q", {"type": "object"}) == {"ok": True}
    assert llm("q", {"type": "object", "x": 1}) == {"ok": True}  # different schema_hash -> new call
    assert seen == ["q", "q"]
