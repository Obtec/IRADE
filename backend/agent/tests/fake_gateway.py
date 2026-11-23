"""Scripted stand-in for the LLM Gateway (api_agents.chat_completion) used by the AGDD agent tests."""
from __future__ import annotations

import json


def tool_call(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"id": call_id or f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", tool_calls: list | None = None, prompt_tokens: int = 100, completion_tokens: int = 20,
          model: str = "fake-mid") -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": model, "choices": [{"message": msg}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "prompt_tokens_details": {"cached_tokens": 0}}}


class FakeGateway:
    """Install with ``monkeypatch.setattr(api_agents, "chat_completion", gw)``.

    ``script`` is a list of replies (dicts) or callables ``(body) -> reply``; structured calls (bodies with a
    ``response_format``) are answered by ``structured`` when given, so pipelines can mix both kinds."""

    def __init__(self, script=None, structured=None):
        self.script = list(script or [])
        self.structured = structured
        self.calls: list[tuple[dict, dict]] = []

    def __call__(self, body: dict, headers: dict) -> dict:
        self.calls.append((json.loads(json.dumps(body)), dict(headers)))
        if body.get("response_format") and self.structured is not None:
            out = self.structured(body) if callable(self.structured) else self.structured
            return reply(json.dumps(out, ensure_ascii=False), model=body.get("model", ""))
        if not self.script:
            return reply("done")
        nxt = self.script.pop(0)
        return nxt(body) if callable(nxt) else nxt

    def bodies(self, structured: bool | None = None) -> list[dict]:
        out = [b for b, _ in self.calls]
        if structured is None:
            return out
        return [b for b in out if bool(b.get("response_format")) == structured]
