"""VM layer + API agent (LLM Gateway) against the fake Agora runtime (local PTYs) and a scripted gateway."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_gateway import FakeGateway, reply, tool_call
from fake_runtime import FakeRuntime
from irade_agent.agdd import api_agents
from irade_agent.agdd.api_agents import AgentSettings, ApiAgent
from irade_agent.agdd.vm import RuntimeClient, VmShell, owner_for


@pytest.fixture
def vm(tmp_path):
    rt = FakeRuntime(str(tmp_path / "m.sock"), str(tmp_path / "vms"))
    home = tmp_path / "vms" / "1900000001"
    home.mkdir(parents=True)
    sh = VmShell(RuntimeClient(str(tmp_path / "m.sock")), 1900000001, "agdd-test", home=str(home))
    yield sh, home, rt
    sh.close()
    rt.stop()


@pytest.fixture
def nocache(monkeypatch):
    from irade_agent.agdd import semantic_cache
    monkeypatch.setattr(semantic_cache, "cache_lookup", lambda *a, **k: None)
    monkeypatch.setattr(semantic_cache, "cache_store", lambda *a, **k: None)


def test_agent_edits_runs_tests_and_resumes_session(vm, monkeypatch, nocache):
    sh, home, rt = vm
    repo = home / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    gw = FakeGateway([
        reply("calc.py 확인", [tool_call("read_file", {"path": "calc.py"})]),
        reply("", [tool_call("edit_file", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"})]),
        reply("", [tool_call("run_command", {"command": "python3 -c 'import calc; assert calc.add(1, 2) == 3; print(\"1 passed\")'"})]),
        reply("", [tool_call("finish", {"summary": "fixed add; 1 passed"})]),
    ])
    monkeypatch.setattr(api_agents, "chat_completion", gw)
    agent = ApiAgent(sh, AgentSettings(model="mid", effort="high"), "ws-1", "run-1", "dev")
    r1 = agent.agent("fix add", str(repo), None)
    assert r1.ok and r1.text == "fixed add; 1 passed" and r1.session_id and r1.steps == 4
    assert "return a + b" in (repo / "calc.py").read_text()
    body, headers = gw.calls[0]
    assert body["model"] == "mid" and body["reasoning_effort"] == "high"
    assert {t["function"]["name"] for t in body["tools"]} >= {"read_file", "edit_file", "run_command", "finish"}
    assert headers["x-agdd-workspace"] == "ws-1" and headers["x-agdd-run"] == "run-1" and headers["x-agdd-node"] == "dev"
    tool_msgs = [m for m in gw.calls[3][0]["messages"] if m["role"] == "tool"]
    assert "return a - b" in tool_msgs[0]["content"] and tool_msgs[1]["content"].startswith("OK edit_file")
    assert tool_msgs[2]["content"].startswith("OK ")  # parsed tool output: success = 1 line
    assert (home / ".agdd" / "sessions" / f"{r1.session_id}.json").exists()

    gw.script = [reply("이어서 확인했습니다")]
    r2 = agent.agent("again", str(repo), r1.session_id)
    assert r2.ok and r2.session_id == r1.session_id
    resumed = gw.calls[-1][0]["messages"]
    assert resumed[1]["content"] == "fix add" and resumed[-1]["content"] == "again"  # transcript was resumed


def test_ask_edits_has_no_shell_and_tests_are_read_only(vm, monkeypatch, nocache):
    sh, home, rt = vm
    repo = home / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("assert True\n")
    gw = FakeGateway([
        reply("", [tool_call("write_file", {"path": "tests/test_x.py", "content": "assert False\n"}, "c1"),
                   tool_call("run_command", {"command": "touch pwned"}, "c2")]),
        reply("", [tool_call("write_file", {"path": "../escape.py", "content": "x"})]),
        reply("", [tool_call("finish", {"summary": "nothing to do"})]),
    ])
    monkeypatch.setattr(api_agents, "chat_completion", gw)
    res = ApiAgent(sh, AgentSettings(permission="ask-edits")).agent("t", str(repo), None)
    assert res.ok
    assert "run_command" not in {t["function"]["name"] for t in gw.calls[0][0]["tools"]}
    tool_msgs = [m["content"] for m in gw.calls[1][0]["messages"] if m["role"] == "tool"]
    assert "읽기 전용" in tool_msgs[0] and "셸 명령" in tool_msgs[1]
    assert "도구 인자 오류" in [m["content"] for m in gw.calls[2][0]["messages"] if m["role"] == "tool"][-1]
    assert (repo / "tests" / "test_x.py").read_text() == "assert True\n" and not (repo / "pwned").exists()
    assert not (home / "escape.py").exists()


def test_structured_uses_cheap_slot_json_schema_and_reports_failure(monkeypatch, nocache):
    gw = FakeGateway(structured={"conflicts": []})
    monkeypatch.setattr(api_agents, "chat_completion", gw)
    agent = ApiAgent(None, AgentSettings(model="advisor", cheap_model="cheap", cheap_effort="xhigh"), "ws-1")
    data, res = agent.structured("check", {"type": "object"}, task="conflict_check")
    assert data == {"conflicts": []} and res.ok
    body, headers = gw.calls[0]
    assert body["model"] == "cheap" and body["reasoning_effort"] == "high"  # xhigh/max clamp to high
    assert body["response_format"]["type"] == "json_schema" and headers["x-agdd-task"] == "conflict_check"
    monkeypatch.setattr(api_agents, "chat_completion", lambda body, headers: reply("not json at all"))
    with pytest.raises(api_agents.AgentError):
        agent.structured("check2", {"type": "object"})


def test_media_parts_inline_images_and_text_name_the_rest():
    import base64

    class Shell:
        files = {"/a/shot.png": b"\x89PNGfake", "/a/notes.txt": "메모 내용".encode()}

        def read_file(self, path, max_bytes=0):
            if path not in self.files:
                raise FileNotFoundError(path)
            return self.files[path]

    files = [api_agents.MediaFile("/a/shot.png", "image"), api_agents.MediaFile("/a/notes.txt", "text"),
             api_agents.MediaFile("/a/demo.mp4", "video"), api_agents.MediaFile("/a/gone.png", "image")]
    parts = ApiAgent(Shell(), AgentSettings())._media_parts(files)
    assert parts[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"\x89PNGfake").decode()
    assert "메모 내용" in parts[1]["text"]
    assert "/a/demo.mp4" in parts[-1]["text"] and "/a/gone.png" in parts[-1]["text"]


def test_settings_validation_and_legacy_cli_key():
    with pytest.raises(ValueError):
        AgentSettings.from_row({"effort": "turbo"})
    with pytest.raises(ValueError):
        AgentSettings.from_row({"model": "x; rm -rf /"})
    with pytest.raises(ValueError):
        AgentSettings.from_row({"session": "$(id)"})
    with pytest.raises(ValueError):
        AgentSettings.from_row({"permission": "root"})
    s = AgentSettings.from_row({"cli": "codex", "session": "019a2b3c-1111", "model": ""})
    assert not hasattr(s, "cli") and s.session == "019a2b3c-1111" and s.model == "mid" and s.cheap_model == "cheap"


def test_mirror_echoes_output_to_pty_and_keeps_exit_code(vm):
    """mirror=True: the console's agent monitor sees captured output live in the PTY; result is unchanged."""
    import base64
    sh, home, rt = vm
    sh.mirror = True
    before = sh.offset
    r = sh.run("echo mirrored-line; echo " + "x" * 600 + "; exit 3")
    assert r.exit_code == 3 and "mirrored-line" in r.output and "x" * 600 in r.output  # capture file is unclipped
    sh.echo("▶ status line")
    poll = sh.client.terminal(sh.canvas_id, sh.owner, sh.name, "poll", offset=before)
    seen = base64.b64decode(poll.get("data") or b"").decode(errors="replace")
    assert "mirrored-line" in seen and "x" * 600 not in seen  # PTY view is clipped to MIRROR_COLS
    sh.mirror = False
    r2 = sh.run("echo hidden-line")
    assert r2.exit_code == 0 and r2.output.strip() == "hidden-line"


def test_owner_mapping_is_stable_and_not_system():
    u = "7c467abc-85c0-4bf2-9bb6-05b6c755be05"
    assert owner_for(u) == owner_for(u) > 1


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
