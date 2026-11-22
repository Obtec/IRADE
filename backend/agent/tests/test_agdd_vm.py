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



def test_mirror_echoes_output_to_pty_and_keeps_exit_code(vm):
    """mirror=True: the console's agent monitor sees captured output live in the PTY; result is unchanged."""
    sh, home, rt = vm
    sh.mirror = True
    before = sh.offset
    r = sh.run("echo mirrored-line; echo " + "x" * 600 + "; exit 3")
    assert r.exit_code == 3 and "mirrored-line" in r.output and "x" * 600 in r.output  # capture file is unclipped
    import base64
    poll = sh.client.terminal(sh.canvas_id, sh.owner, sh.name, "poll", offset=before)
    seen = base64.b64decode(poll.get("data") or b"").decode(errors="replace")
    assert "mirrored-line" in seen and "x" * 600 not in seen  # PTY view is clipped to MIRROR_COLS
    sh.mirror = False
    r2 = sh.run("echo hidden-line")
    assert r2.exit_code == 0 and r2.output.strip() == "hidden-line"

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


def test_vmshell_recovers_from_expired_pty(tmp_path):
    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    home = tmp_path / "vms" / "1900000001"
    home.mkdir(parents=True, exist_ok=True)
    sh = VmShell(RuntimeClient(str(tmp_path / "m.sock")), 1900000001, "agdd-exp", home=str(home))
    try:
        r1 = sh.run_inline("echo FIRST", timeout=5)
        assert r1.exit_code == 0 and "FIRST" in r1.output

        # Simulate guest idle-reaper removing all live PTY sessions while host holds VmShell.
        with rt.state.lock:
            for key, sess in list(rt.state.sessions.items()):
                sess.proc.kill()
                del rt.state.sessions[key]

        r2 = sh.run_inline("echo RECOVERED", timeout=5)
        assert r2.exit_code == 0 and "RECOVERED" in r2.output
    finally:
        sh.close()
        rt.stop()


def test_vmshell_read_file_timeout_raises_runtime_error_and_decodes_b64(monkeypatch, tmp_path):
    import base64
    from irade_agent.agdd.vm import CommandResult, RuntimeError_

    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    sh = VmShell(RuntimeClient(str(tmp_path / "m.sock")), 1900000001, "agdd-rf")
    try:
        monkeypatch.setattr(sh, "run", lambda *a, **k: CommandResult(124, "timeout after 30s", timed_out=True))
        with pytest.raises(RuntimeError_):
            sh.read_file("/tmp/missing.json")

        monkeypatch.setattr(sh, "run", lambda *a, **k: CommandResult(1, "timeout after 30s", timed_out=False))
        with pytest.raises(RuntimeError_):
            sh.read_file("/tmp/missing.json")

        payload = base64.b64encode(b'{"ok":true}').decode()
        monkeypatch.setattr(
            sh,
            "run",
            lambda cmd, *a, **k: CommandResult(0, "11") if "stat " in cmd else CommandResult(0, f"{payload[:8]}\r\n{payload[8:]}\n"),
        )
        assert sh.read_file("/tmp/ok.json") == b'{"ok":true}'
    finally:
        sh.close()
        rt.stop()


def test_agy_model_effort_and_structured_cmd():
    agy = PROFILES["agy"]
    assert agy.cheap_default == "gemini-3.8-flash"

    # Empty model must not emit --effort (rejected by agy CLI when model is default).
    cmd_empty = agy.agent_cmd("/w/p", "/r", "", "low", None, "sandbox")
    assert "--model" not in cmd_empty and "--effort" not in cmd_empty

    # gemini-3.8-flash does not accept max/xhigh; effort must be clamped.
    cmd_clamped = agy.agent_cmd("/w/p", "/r", "gemini-3.8-flash", "max", None, "sandbox")
    assert "--model gemini-3.8-flash" in cmd_clamped and "--effort max" not in cmd_clamped

    # structured_cmd passes schema path directly via --json-schema and disables slash commands.
    sc = agy.structured_cmd("/w/p", "/w/schema.json", "gemini-3.8-flash", "low")
    assert "--print=" in sc and "--json-schema" in sc and "/w/schema.json" in sc
    assert "--model gemini-3.8-flash" in sc and "--effort low" in sc
    assert "--disable-slash-commands" in sc and "cat '/w/schema.json'" not in sc


def test_vmagent_structured_fails_fast_on_auth_error(tmp_path):
    from irade_agent.agdd.cli_agents import CliError

    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    home = tmp_path / "vms" / "1900000001"
    (home / ".local" / "bin").mkdir(parents=True)
    agy_stub = home / ".local" / "bin" / "agy"
    agy_stub.write_text(
        '#!/usr/bin/env python3\n'
        'import json, os, sys\n'
        'open(os.path.join(os.environ["HOME"], "agy-calls.log"), "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'print(json.dumps({"conversation_id":"","status":"ERROR","response":"","error":"Not logged in. Please authenticate first.","usage":{}}))\n'
        'sys.exit(1)\n'
    )
    agy_stub.chmod(agy_stub.stat().st_mode | stat.S_IEXEC)
    sh = VmShell(RuntimeClient(str(tmp_path / "m.sock")), 1900000001, "agdd-auth", home=str(home))
    try:
        settings = AgentSettings(cli="agy", model="gemini-4-neon", cheap_model="gemini-4-neon", effort="low", permission="sandbox")
        with pytest.raises(CliError):
            VmAgent(sh, settings).structured("hello", {"type": "object"})
        calls = (home / "agy-calls.log").read_text().strip().splitlines()
        assert len(calls) == 1
    finally:
        sh.close()
        rt.stop()
