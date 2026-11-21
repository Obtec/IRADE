"""Gateway-level token saving (DOC.md §6.2): prefix ordering, output parser, hard cap, batch clearing."""
from __future__ import annotations

import hashlib
import json
from typing import Callable

HARD_CAP_TOKENS = 2500
ERROR_LINES = 30
CLEAR_THRESHOLD = 50_000
KEEP_RECENT_TURNS = 3
CLEAR_MIN_TOKENS = 1000


def estimate_tokens(text: str) -> int:
    """Cheap heuristic (~4 chars/token, Hangul ~1.5 chars/token)."""
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    return (len(text) - hangul) // 4 + int(hangul / 1.5) + 1


def hard_cap(text: str, max_tokens: int = HARD_CAP_TOKENS) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # longest prefix within cap
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens - 20:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + f"\n[TRUNCATED at {max_tokens} tokens; full output offloaded]"


_ERR_HINTS = ("error", "fail", "exception", "traceback", "assert", "panic", "fatal", "에러", "실패")


def parse_tool_output(command: str, exit_code: int, output: str,
                      offload: Callable[[str, str], str] | None = None) -> str:
    """Success -> 1 line. Failure -> exit code + key error lines (<=30). Always hard-capped.
    ``offload(command, output)`` stores the raw text (FTS5 / file) and returns a retrieval key."""
    key = offload(command, output) if offload else hashlib.sha256(output.encode()).hexdigest()[:12]
    lines = output.splitlines()
    if exit_code == 0:
        tail = lines[-1].strip() if lines else ""
        return hard_cap(f"OK `{command}` ({len(lines)} lines) {tail[:160]} [ref:{key}]")
    hits = [i for i, l in enumerate(lines) if any(h in l.lower() for h in _ERR_HINTS)]
    picked: list[str] = []
    for i in hits:
        for j in range(max(0, i - 1), min(len(lines), i + 3)):
            if lines[j] not in picked:
                picked.append(lines[j])
        if len(picked) >= ERROR_LINES:
            break
    body = "\n".join((picked or lines[-ERROR_LINES:])[:ERROR_LINES])
    return hard_cap(f"EXIT {exit_code} `{command}` [ref:{key}]\n{body}")


def build_messages(system: str, rules: str, tools: list[dict], history: list[dict], dynamic: str) -> dict:
    """Fixed prefix order for prompt caching: system+rules -> tool schema -> append-only history -> dynamic input."""
    return {
        "system": system.rstrip() + "\n\n" + rules.strip(),
        "tools": sorted(tools, key=lambda t: t.get("name", "")),  # stable order keeps the cache key
        "messages": list(history) + [{"role": "user", "content": dynamic}],
    }


def batch_clear(messages: list[dict], threshold: int = CLEAR_THRESHOLD, keep_turns: int = KEEP_RECENT_TURNS,
                min_tokens: int = CLEAR_MIN_TOKENS) -> tuple[list[dict], int]:
    """Replace old large tool_result bodies with ~30-token stubs only once context exceeds ``threshold``.
    tool_use blocks are kept. Returns (messages, cleared_count)."""
    total = sum(estimate_tokens(json.dumps(m.get("content"), ensure_ascii=False)) for m in messages)
    if total <= threshold:
        return messages, 0
    turn_idx = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    cutoff = turn_idx[-keep_turns] if len(turn_idx) >= keep_turns else 0
    stub = "[CLEARED] {} — 재조회: 동일 도구 재호출 또는 search_outputs"
    cleared = 0
    out = []
    for i, m in enumerate(messages):
        if i < cutoff and m.get("role") == "tool" and isinstance(m.get("content"), str) \
                and estimate_tokens(m["content"]) > min_tokens:  # OpenAI tool message
            m = dict(m, content=stub.format(m.get("tool_call_id", "")))
            cleared += 1
        elif i < cutoff and isinstance(m.get("content"), list):  # Anthropic-style tool_result blocks
            new = []
            for block in m["content"]:
                if block.get("type") == "tool_result" and \
                        estimate_tokens(json.dumps(block.get("content"), ensure_ascii=False)) > min_tokens:
                    block = dict(block, content=stub.format(block.get("tool_use_id", "")))
                    cleared += 1
                new.append(block)
            m = dict(m, content=new)
        out.append(m)
    return out, cleared


def thinking_for(task_kind: str) -> str:
    """Simple turns run with reasoning disabled on the cheap tier."""
    return "none" if task_kind in {"file_select", "conflict_check", "semantic_lift", "classify", "summarize",
                                   "route", "ambiguity"} else "medium"
