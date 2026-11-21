"""Token-saving strategies applied to the CLI/API agents (DOC.md §6)."""
import json
import stat
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_runtime import FakeRuntime
from test_agdd_vm import STUB_CLAUDE
import ast
from irade_agent.agdd import budget, infra, semantic_cache
from irade_agent.agdd.cli_agents import PROFILES, AgentSettings, VmAgent
from irade_agent.agdd.vm import RuntimeClient, VmShell


class FakeValkey(dict):
    def get(self, k): return super().get(k)
    def set(self, k, v, ex=None): self[k] = v


@pytest.fixture
def cache(monkeypatch):
    kv = FakeValkey()
    monkeypatch.setattr(infra, "valkey", lambda: kv)
    return kv


@pytest.fixture
def vm(tmp_path):
    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    home = tmp_path / "vms" / "1900000001"
    (home / ".local" / "bin").mkdir(parents=True)
    stub = home / ".local" / "bin" / "claude"
    stub.write_text(STUB_CLAUDE)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    sh = VmShell(RuntimeClient(str(tmp_path / "m.sock")), 1900000001, "agdd-tok", home=str(home))
    yield sh, home
    sh.close()
    rt.stop()


def calls(home):
    return [json.loads(l) for l in (home / "claude-args.log").read_text().splitlines()]


def test_structured_uses_cheap_default_no_tools_no_mcp_and_exact_cache(vm, cache):
    sh, home = vm
    agent = VmAgent(sh, AgentSettings(cli="claude", model="opus"))
    d1, r1 = agent.structured("same question", {"type": "object"})
    d2, r2 = agent.structured("same question", {"type": "object"})
    assert d1 == d2 and r1.model == "haiku" and r2.model == "cache"
    args = calls(home)
    assert len(args) == 1, "identical structured question must not call the model twice"
    a = args[0]
    assert a[a.index("--model") + 1] == "haiku" and a[a.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in a and "--exclude-dynamic-system-prompt-sections" in a
    agent.structured("different question", {"type": "object"})
    assert len(calls(home)) == 2


def test_agent_mcp_allowlist_and_budget_fallback(vm, cache):
    sh, home = vm
    (home / "repo").mkdir()
    agent = VmAgent(sh, AgentSettings(cli="claude", model="opus"), gate=lambda kind: "FALLBACK")
    res = agent.agent("fix", str(home / "repo"), None, mcp=False)
    a = calls(home)[-1]
    assert res.model == "haiku" and a[a.index("--model") + 1] == "haiku" and "--strict-mcp-config" in a
    agent.gate = lambda kind: "ALLOW"
    res = agent.agent("fix", str(home / "repo"), None, mcp=True)
    a = calls(home)[-1]
    assert res.model == "opus" and "--strict-mcp-config" not in a


def test_budget_gate_thresholds_and_halt():
    assert budget.cli_gate(Decimal("10"), Decimal("7.9")) == "ALLOW"
    assert budget.cli_gate(Decimal("10"), Decimal("8")) == "FALLBACK"
    with pytest.raises(budget.BudgetHalt):
        budget.cli_gate(Decimal("10"), Decimal("10"))
    with pytest.raises(budget.BudgetHalt):
        budget.cli_gate(Decimal("0"), Decimal("0"))  # zero limit = no spending allowed


def test_codex_disables_mcp_for_structured_and_when_not_allowed():
    c = PROFILES["codex"]
    assert "mcp_servers={}" in c.structured_cmd("/p", "/s", "gpt-5-mini", "low")
    assert "mcp_servers={}" in c.agent_cmd("/p", "/r", "", "medium", None, "sandbox", mcp=False)
    assert "mcp_servers" not in c.agent_cmd("/p", "/r", "", "medium", None, "sandbox", mcp=True)


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
