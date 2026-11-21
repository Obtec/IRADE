"""VM + CLI agent layer against the fake Agora runtime (local PTYs) with stub CLIs."""
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_runtime import FakeRuntime
from irade_agent.agdd.cli_agents import PROFILES, AgentSettings, VmAgent, parse_codex_rate_limits, parse_sessions
from irade_agent.agdd.vm import RuntimeClient, VmShell, owner_for

STUB_CLAUDE = r'''#!/usr/bin/env python3
import json, sys, os, uuid
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.path.join(os.environ["HOME"], "claude-args.log")
open(log, "a").write(json.dumps(args) + "\n")
if "--json-schema" in args:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                      "structured_output": {"conflicts": [], "echo": "--effort" in args},
                      "session_id": "s-struct", "total_cost_usd": 0.001,
                      "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}}))
    sys.exit(0)
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
open("calc.py", "w").write("def add(a, b):\n    return a + b\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1890000000}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "fixed add; 1 passed",
                  "session_id": sid, "total_cost_usd": 0.02, "usage": {"input_tokens": 100, "output_tokens": 20}}))
'''


@pytest.fixture
def vm(tmp_path):
    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    home = tmp_path / "vms" / "1900000001"
    (home / ".local" / "bin").mkdir(parents=True)
    stub = home / ".local" / "bin" / "claude"
    stub.write_text(STUB_CLAUDE)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    sh = VmShell(RuntimeClient(str(tmp_path / "m.sock")), 1900000001, "agdd-test", home=str(home))
    yield sh, home, rt
    sh.close()
    rt.stop()


def test_structured_and_agent_with_session_resume(vm):
    sh, home, rt = vm
    (home / "repo").mkdir()
    agent = VmAgent(sh, AgentSettings(cli="claude", model="opus", effort="high", cheap_effort="low"))
    data, res = agent.structured("check", {"type": "object"})
    assert data == {"conflicts": [], "echo": True} and res.cost_usd == 0.001 and res.usage["cached_tokens"] == 90
    r1 = agent.agent("fix add", str(home / "repo"), None)
    assert r1.ok and r1.text == "fixed add; 1 passed" and r1.session_id and r1.rate_limits["status"] == "allowed"
    assert "return a + b" in (home / "repo" / "calc.py").read_text()
    r2 = agent.agent("again", str(home / "repo"), r1.session_id)
    assert r2.session_id == r1.session_id
    calls = [json.loads(l) for l in (home / "claude-args.log").read_text().splitlines()]
    assert ["--model", "opus"] == calls[1][calls[1].index("--model"):calls[1].index("--model") + 2]
    assert "--effort" in calls[1] and calls[1][calls[1].index("--effort") + 1] == "high"
    assert calls[0][calls[0].index("--effort") + 1] == "low"  # cheap structured steps use cheap_effort
    assert "--resume" in calls[2] and "bypassPermissions" in " ".join(calls[1])


def test_settings_validation():
    with pytest.raises(ValueError):
        AgentSettings.from_row({"cli": "nope"})
    with pytest.raises(ValueError):
        AgentSettings.from_row({"effort": "turbo"})
    with pytest.raises(ValueError):
        AgentSettings.from_row({"model": "x; rm -rf /"})
    with pytest.raises(ValueError):
        AgentSettings.from_row({"session": "$(id)"})
    assert AgentSettings.from_row({"cli": "codex", "session": "019a2b3c-1111"}).cli == "codex"


def test_command_shapes_are_quoted():
    c = PROFILES["codex"].agent_cmd("/w/p", "/workspace/repos/demo", "gpt-5.5", "max", "019a-ses", "sandbox")
    assert "codex exec resume 019a-ses --json --dangerously-bypass-approvals-and-sandbox" in c
    assert "model_reasoning_effort=xhigh" in c and "cd '/workspace/repos/demo'" in c
    a = PROFILES["agy"].agent_cmd("/w/p", "/r", "gemini-3-pro", "low", "conv1", "ask-edits")
    assert "--conversation conv1" in a and "--dangerously-skip-permissions" not in a and "--effort low" in a


def test_codex_parse_and_rate_limits():
    out = "\n".join(json.dumps(x) for x in [
        {"type": "thread.started", "thread_id": "019a-th"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 40, "output_tokens": 7,
                                             "reasoning_output_tokens": 3}}])
    r = PROFILES["codex"].parse(out)
    assert r.ok and r.session_id == "019a-th" and r.text == "done" and r.usage["thinking_tokens"] == 3
    roll = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 25.0, "window_minutes": 300, "resets_at": 1890000000}}}})
    assert parse_codex_rate_limits("Logged in\n---\n" + roll)["primary"]["used_percent"] == 25.0
    assert parse_sessions("019a\t1890000000\t/workspace/repos/demo\njunk") == [
        {"id": "019a", "mtime": 1890000000, "where": "/workspace/repos/demo"}]


def test_owner_mapping_is_stable_and_not_system():
    u = "7c467abc-85c0-4bf2-9bb6-05b6c755be05"
    assert owner_for(u) == owner_for(u) > 1


def test_agy_real_output_shape():
    err = '{"conversation_id":"","status":"ERROR","response":"","error":"authentication failed or timed out","usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}'
    r = PROFILES["agy"].parse("Error: authentication interrupted.\n" + err)
    assert not r.ok and r.session_id is None
    ok = '{"conversation_id":"c-1","status":"OK","response":"done","usage":{"input_tokens":10,"output_tokens":3,"thinking_tokens":2,"cache_read_tokens":90}}'
    r = PROFILES["agy"].parse(ok)
    assert r.ok and r.session_id == "c-1" and r.usage["cached_tokens"] == 90 and r.usage["prompt_tokens"] == 100


def test_claude_not_logged_in_is_failure():
    out = '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in · Please run /login","session_id":"x"}'
    assert not PROFILES["claude"].parse(out).ok
