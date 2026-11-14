"""Offline unit & Unix-socket tests for irade_agent (no network calls)."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from irade_agent import daemon, telemetry
from irade_agent.engine import run_turn


def completion(calls=None, answer=None):
    message = SimpleNamespace(content=answer, tool_calls=calls or [], reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def tool(name, arguments):
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(
        id=f"call-{name}",
        function=fn,
        model_dump=lambda exclude_none=True: {
            "id": f"call-{name}",
            "type": "function",
            "function": {"name": name, "arguments": fn.arguments},
        },
    )


@pytest.mark.asyncio
async def test_mcp_flow_reads_and_patches_without_forging_writes(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setattr(telemetry, "_server_states", {})
    bridge = AsyncMock(
        side_effect=[
            {"objects": [{"id": "cell-1", "revision": 4, "item": {"kind": "code", "code": "a=1", "language": "python", "renderMode": "irade-python"}}]},
            {"object_ids": ["cell-1"], "revision": 5},
        ]
    )
    create = AsyncMock(
        side_effect=[
            completion([tool("get_objects", {"ids": ["cell-1"]})]),
            completion(
                [
                    tool(
                        "patch_objects",
                        {"changes": [{"id": "cell-1", "revision": 4, "patch": {"code": "a=1\nb=2"}}]},
                    )
                ]
            ),
            completion(answer="cell-1 셀을 업데이트했습니다."),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = await run_turn({"message": "cell-1에 b=2를 추가해줘", "selected_ids": ["cell-1"]}, bridge, AsyncMock(), client)
    assert "업데이트했습니다" in result["answer"]
    assert bridge.await_count == 2
    assert bridge.await_args_list[0].args == ("get_objects", {"ids": ["cell-1"]})
    assert bridge.await_args_list[1].args[0] == "patch_objects"


@pytest.mark.asyncio
async def test_conflict_propagates_to_model_instead_of_dropping_edits(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setattr(telemetry, "_server_states", {})
    bridge = AsyncMock(side_effect=[{"error": "ITEM_REVISION_CONFLICT"}])
    create = AsyncMock(
        side_effect=[
            completion(
                [
                    tool(
                        "patch_objects",
                        {"changes": [{"id": "cell-1", "revision": 1, "patch": {"text": "new"}}]},
                    )
                ]
            ),
            completion(answer="충돌이 발생했습니다."),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = await run_turn({"message": "수정해줘"}, bridge, AsyncMock(), client)
    assert "충돌" in result["answer"]
    tool_msg = next(m["content"] for m in create.call_args.kwargs["messages"] if m["role"] == "tool")
    assert "ITEM_REVISION_CONFLICT" in tool_msg


@pytest.mark.asyncio
async def test_tool_list_disables_delete_by_default_and_enables_on_opt_in(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setattr(telemetry, "_server_states", {})
    create = AsyncMock(return_value=completion(answer="ok"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    await run_turn({"message": "hi"}, AsyncMock(), AsyncMock(), client)
    names_default = [t["function"]["name"] for t in create.call_args.kwargs["tools"]]
    assert "delete_objects" not in names_default
    await run_turn({"message": "hi", "allow_delete": True}, AsyncMock(), AsyncMock(), client)
    names_opt_in = [t["function"]["name"] for t in create.call_args.kwargs["tools"]]
    assert "delete_objects" in names_opt_in


@pytest.mark.asyncio
async def test_unconfigured_key_fails_fast_without_bridge(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_APIS_JSON", raising=False)
    bridge = AsyncMock()
    result = await run_turn({"message": "test"}, bridge, AsyncMock())
    assert result["error"] == "AGENT_NOT_CONFIGURED"
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_unix_socket_bridge_and_status_probe(tmp_path, monkeypatch):
    daemon.semaphore = asyncio.Semaphore(1)
    monkeypatch.setenv("AGENT_API_KEY", "server-status-key")
    monkeypatch.setenv("AGENT_SOCKET", str(tmp_path / "agent.sock"))
    monkeypatch.setattr(telemetry, "_server_states", {})
    sock = str(tmp_path / "agent.sock")
    async with await asyncio.start_unix_server(daemon.serve, sock, limit=daemon.MAX_FRAME):
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b'{"operation":"status"}\n')
        await writer.drain()
        status = json.loads(await reader.readline())
        assert status["ready"] is True
        assert status["configured"] is True
        assert status["default_configured"] is True
        assert status["credential_source"] == "server"
        assert status["model"] == os.getenv("AGENT_MODEL", "k-exaone_v2.5")
        assert len(status["apis"]) >= 1
        assert "server-status-key" not in json.dumps(status)
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_oversized_tool_result_blocks_truncation_attack(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setattr(telemetry, "_server_states", {})
    create = AsyncMock(
        side_effect=[
            completion([tool("get_objects", {"ids": ["cell-1"]})]),
            completion(answer="요청된 객체가 너무 큽니다."),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result_turn = await run_turn(
        {"message": "읽어줘"},
        AsyncMock(return_value={"objects": [{"id": "cell-1", "text": "secret-source-" + ("x" * 200000)}]}),
        AsyncMock(),
        client,
    )
    assert "너무" in result_turn["answer"]
    tool_msg = json.loads(next(m["content"] for m in create.call_args.kwargs["messages"] if m["role"] == "tool"))
    assert tool_msg["error"] == "TOOL_RESULT_TOO_LARGE"
    assert "secret-source-" not in json.dumps(tool_msg)


@pytest.mark.asyncio
async def test_provider_busy_returns_immediately_without_replaying_committed_tool(monkeypatch):
    import httpx
    from openai import RateLimitError
    from irade_agent import engine

    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setattr(telemetry, "_server_states", {})
    busy = RateLimitError(
        "do not disclose this",
        response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")),
        body={"error": "busy"},
    )
    create = AsyncMock(
        side_effect=[
            completion([tool("create_objects", {"items": [{"kind": "text", "text": "hi"}]})]),
            busy,
            completion(answer="완료"),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    bridge = AsyncMock(return_value={"object_ids": ["a"]})
    monkeypatch.setattr(engine.asyncio, "sleep", AsyncMock())
    with pytest.raises(ExceptionGroup):
        await run_turn({"message": "생성해줘"}, bridge, AsyncMock(), client)
    assert bridge.await_count == 1 and create.await_count == 2
    safe = daemon.public_error(ExceptionGroup("wrapped", [busy]))
    assert safe["error"] == "AGENT_PROVIDER_BUSY" and "do not disclose" not in json.dumps(safe)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_parse", [True, False])
async def test_raw_response_headers_and_custom_options(monkeypatch, tmp_path, async_parse):
    monkeypatch.setenv("AGENT_SOCKET", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("AGENT_API_KEY", "header-test-key")
    monkeypatch.setattr(telemetry, "_server_states", {})
    raw = SimpleNamespace(
        headers={
            "x-second-rate-limit": "1",
            "x-second-rate-limit-remaining": "0",
            "x-minute-rate-limit": "60",
            "x-minute-rate-limit-remaining": "59",
            "x-day-rate-limit": "100",
            "x-day-rate-limit-remaining": "91",
            "authorization": "hidden",
        },
        status_code=200,
        parse=AsyncMock(return_value=completion(answer="done")),
    )
    if not async_parse:
        raw.parse = lambda: completion(answer="done")
    create = AsyncMock(return_value=raw)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    )
    config = {"reasoning_effort": "max", "max_tokens": 16384, "max_steps": 3, "temperature": 0.7, "top_p": 0.8}
    result = await run_turn({"message": "test", "options": config}, AsyncMock(), AsyncMock(), client)
    assert result["telemetry"]["limits"]["day"] == {"limit": 100, "remaining": 91}
    assert result["telemetry"]["limits"]["second"] == {"limit": 1, "remaining": 0}
    assert result["telemetry"]["limits"]["minute"] == {"limit": 60, "remaining": 59}
    assert create.call_args.kwargs["extra_body"]["reasoning_effort"] == "max"
    assert create.call_args.kwargs["max_tokens"] == 16384
    assert result["options"] == config
    saved = (tmp_path / "quota.json").read_text()
    assert "hidden" not in saved and "authorization" not in saved
    monkeypatch.setattr(telemetry, "_server_states", {})
    assert telemetry.snapshot()["limits"]["minute"]["remaining"] == 59


def test_quota_errors_retry_after_and_key_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SOCKET", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("AGENT_API_KEY", "test-key-one")
    monkeypatch.setattr(telemetry, "_server_states", {})
    data = telemetry.record(
        {
            "x-day-rate-limit": "100",
            "x-day-rate-limit-remaining": "0",
            "retry-after-ms": "1250",
            "x-second-rate-limit": "NaN",
        },
        429,
    )
    assert data["status"] == 429 and data["retry_at"] - data["observed_at"] == 1.25
    assert "second" not in data["limits"]
    monkeypatch.setenv("AGENT_API_KEY", "test-key-two")
    monkeypatch.setattr(telemetry, "_server_states", {})
    assert telemetry.snapshot() == {}


@pytest.mark.asyncio
async def test_per_api_quota_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SOCKET", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("AGENT_API_KEY", "exaone-key")
    monkeypatch.setenv("AGENT_API_ID", "exaone-2.5")
    monkeypatch.setenv(
        "AGENT_APIS_JSON",
        json.dumps([
            {"id": "exaone-lab", "label": "EXAONE Lab", "base_url": "https://lab.exaone.ai/v1", "model": "exaone-lab-v2", "api_key": "lab-key"}
        ]),
    )
    monkeypatch.setattr(telemetry, "_server_states", {})
    from irade_agent.settings import resolve_api

    p1 = resolve_api("exaone-2.5")
    p2 = resolve_api("exaone-lab")
    t1 = telemetry.bind(p1["api_key"], p1)
    try:
        telemetry.record({"x-second-rate-limit": "5", "x-second-rate-limit-remaining": "4", "x-minute-rate-limit": "60", "x-minute-rate-limit-remaining": "55", "x-day-rate-limit": "500", "x-day-rate-limit-remaining": "490"}, 200)
    finally:
        telemetry.unbind(t1)

    t2 = telemetry.bind(p2["api_key"], p2)
    try:
        assert telemetry.snapshot() == {}
        telemetry.record({"x-second-rate-limit": "2", "x-second-rate-limit-remaining": "1", "x-minute-rate-limit": "20", "x-minute-rate-limit-remaining": "19", "x-day-rate-limit": "100", "x-day-rate-limit-remaining": "80"}, 200)
        assert telemetry.snapshot()["limits"]["day"]["remaining"] == 80
        assert telemetry.snapshot()["api"] == "exaone-lab"
    finally:
        telemetry.unbind(t2)

    t1_check = telemetry.bind(p1["api_key"], p1)
    try:
        snap1 = telemetry.snapshot()
        assert snap1["api"] == "exaone-2.5"
        assert snap1["limits"]["day"]["remaining"] == 490
        assert snap1["limits"]["second"]["remaining"] == 4
    finally:
        telemetry.unbind(t1_check)


@pytest.mark.parametrize(
    "config",
    [
        {"reasoning_effort": "medium"},
        {"max_tokens": 1000000},
        {"max_steps": 0},
        {"max_steps": 1.5},
        {"temperature": float("nan")},
        {"top_p": False},
        {"api_key": "forged"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_options_never_call_model(config):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    with pytest.raises(ValueError, match="AGENT_OPTIONS_INVALID"):
        await run_turn({"message": "test", "options": config}, AsyncMock(), AsyncMock(), client)
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_api_rejected_without_calling_model(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    with pytest.raises(ValueError, match="AGENT_API_UNKNOWN"):
        await run_turn({"message": "test", "api": "https://evil.internal"}, AsyncMock(), AsyncMock(), client)
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_truncated_reasoning_reports_token_budget_without_running_partial_tool(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    response = completion(answer=None)
    response.choices[0].finish_reason = "length"
    create = AsyncMock(return_value=response)
    bridge = AsyncMock()
    result = await run_turn(
        {"message": "test", "options": {"max_tokens": 512}},
        bridge,
        AsyncMock(),
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    assert result["limited"] is True and "토큰 상한" in result["answer"]
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_key_overrides_server_without_entering_prompt_or_result(monkeypatch):
    from irade_agent import engine

    monkeypatch.setenv("AGENT_API_KEY", "server-key-fixture")
    create = AsyncMock(return_value=completion(answer="done"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(engine, "AsyncOpenAI", factory)
    result = await engine.run_turn({"message": "test", "api_key": "personal-key-fixture"}, AsyncMock(), AsyncMock())
    assert factory.call_args.kwargs["api_key"] == "personal-key-fixture"
    assert "personal-key-fixture" not in json.dumps(create.call_args.kwargs)
    assert "personal-key-fixture" not in json.dumps(result)
    assert "server-key-fixture" not in json.dumps(result)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_personal_keys_and_apis_keep_quota_isolated_without_disk_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SOCKET", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("AGENT_API_KEY", "server-key-fixture")
    monkeypatch.setattr(telemetry, "_server_states", {})
    telemetry.record({"x-day-rate-limit": "100", "x-day-rate-limit-remaining": "90"}, 200)

    async def personal(key, api_id, remaining):
        token = telemetry.bind(key, {"id": api_id, "base_url": f"https://{api_id}.example/v1", "model": api_id})
        try:
            telemetry.record({"x-day-rate-limit": "100", "x-day-rate-limit-remaining": str(remaining)}, 200)
            await asyncio.sleep(0)
            return telemetry.snapshot()["limits"]["day"]["remaining"]
        finally:
            telemetry.unbind(token)

    assert await asyncio.gather(
        personal("personal-one", "exaone-2.5", 11),
        personal("personal-one", "exaone-alt", 33),
        personal("personal-two", "exaone-2.5", 22),
    ) == [11, 33, 22]
    assert telemetry.snapshot()["limits"]["day"]["remaining"] == 90
    disk = (tmp_path / "quota.json").read_text()
    assert "personal-one" not in disk and "personal-two" not in disk
    assert "11.0" not in disk and "22.0" not in disk and "33.0" not in disk


@pytest.mark.parametrize("key", [True, 42, "a\nb", "a b", "한글", "a" * 4097])
def test_personal_key_invalid_shapes_rejected(key):
    from irade_agent.credentials import personal_key

    with pytest.raises(ValueError, match="AGENT_API_KEY_INVALID"):
        personal_key({"api_key": key})


@pytest.mark.asyncio
async def test_status_personal_credentials_does_not_return_or_persist_key(tmp_path, monkeypatch):
    daemon.semaphore = asyncio.Semaphore(1)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_APIS_JSON", raising=False)
    monkeypatch.setenv("AGENT_SOCKET", str(tmp_path / "agent.sock"))
    monkeypatch.setattr(telemetry, "_server_states", {})
    sock = str(tmp_path / "agent.sock")
    async with await asyncio.start_unix_server(daemon.serve, sock, limit=daemon.MAX_FRAME):
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b'{"operation":"status","api_key":"personal-fixture-no-secret"}\n')
        await writer.drain()
        value = json.loads(await reader.readline())
        assert value["configured"] is True and value["default_configured"] is False
        assert value["credential_source"] == "personal"
        assert "personal-fixture-no-secret" not in json.dumps(value)
        writer.close()
        await writer.wait_closed()
        assert not (tmp_path / "quota.json").exists()
