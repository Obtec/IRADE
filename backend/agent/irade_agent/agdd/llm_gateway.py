"""LLM Gateway Proxy (DOC.md §2 worker sandbox, §6.2).

OpenAI-compatible HTTP proxy in front of the upstream provider:
- model alias routing (cheap / mid / advisor) + 80% fallback / 100% halt budget gate
- reasoning_effort forced to none/low for simple task kinds
- prefix normalization for prompt caching (system first, stable tool order)
- tool_result batch clearing above the token threshold
- per-workspace rate limiting, usage -> node_executions + workspace_budgets
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import budget, gateway

ALIASES = {
    "cheap": os.environ.get("MODEL_CHEAP", "gpt-5-nano"),
    "mid": os.environ.get("MODEL_MID", "gpt-5-mini"),
    "advisor": os.environ.get("MODEL_ADVISOR", "gpt-5"),
}
# USD per 1M tokens: input, cached input, output (override via MODEL_PRICES JSON)
PRICES = json.loads(os.environ.get("MODEL_PRICES") or "{}") or {
    ALIASES["cheap"]: [0.05, 0.005, 0.4], ALIASES["mid"]: [0.25, 0.025, 2.0], ALIASES["advisor"]: [1.25, 0.125, 10.0]}
EST_OUTPUT_TOKENS = 800


def resolve_model(requested: str) -> str:
    return ALIASES.get(requested, requested)


def cost_of(model: str, usage: dict) -> float:
    p_in, p_cached, p_out = PRICES.get(model, [1.0, 0.1, 4.0])
    cached = usage.get("cached_tokens", 0)
    return ((usage.get("prompt_tokens", 0) - cached) * p_in + cached * p_cached +
            (usage.get("output_tokens", 0) + usage.get("thinking_tokens", 0)) * p_out) / 1e6


def normalize(body: dict, task: str) -> dict:
    """Stable prefix order: system -> tools(sorted) -> history -> dynamic last user turn."""
    body = dict(body)
    msgs = body.get("messages", [])
    systems = [m for m in msgs if m.get("role") in ("system", "developer")]
    rest = [m for m in msgs if m.get("role") not in ("system", "developer")]
    rest, _ = gateway.batch_clear(rest)
    body["messages"] = systems + rest
    if body.get("tools"):
        body["tools"] = sorted(body["tools"], key=lambda t: (t.get("function") or t).get("name", ""))
    effort = gateway.thinking_for(task)
    if effort == "none":
        body["reasoning_effort"] = os.environ.get("CHEAP_REASONING_EFFORT", "low")
    return body


STRUCTURED_MODE = os.environ.get("STRUCTURED_OUTPUT_MODE", "json_object")  # json_object | json_schema
MIN_INTERVAL = float(os.environ.get("UPSTREAM_MIN_INTERVAL", "1.0"))  # provider rate limit (EXAONE: 1 req/s)
MAX_RETRIES = int(os.environ.get("UPSTREAM_MAX_RETRIES", "6"))
_slot_lock = threading.Lock()
_next_slot = [0.0]


def structured(body: dict) -> dict:
    """Providers whose constrained decoding degenerates on complex schemas get json_object + schema-in-prompt."""
    rf = body.get("response_format") or {}
    if STRUCTURED_MODE != "json_object" or rf.get("type") != "json_schema":
        return body
    schema = (rf.get("json_schema") or {}).get("schema", {})
    msgs = [dict(m) for m in body.get("messages", [])]
    for m in reversed(msgs):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] += ("\n\n반드시 다음 JSON Schema를 따르는 JSON 객체 하나만 출력하세요(설명·코드펜스 금지):\n"
                             + json.dumps(schema, ensure_ascii=False))
            break
    return dict(body, messages=msgs, response_format={"type": "json_object"})


def wait_slot() -> None:
    with _slot_lock:
        now = time.monotonic()
        start = max(now, _next_slot[0])
        _next_slot[0] = start + MIN_INTERVAL
    if start > now:
        time.sleep(start - now)


def usage_of(resp: dict) -> dict:
    u = resp.get("usage") or {}
    return {"prompt_tokens": u.get("prompt_tokens", 0),
            "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0) - (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "thinking_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)}


class BudgetHalt(Exception):
    pass


def budget_gate(workspace_id: str | None, model: str, prompt_tokens: int) -> str:
    """Returns the model to actually use, or raises BudgetHalt (pipeline -> HITL)."""
    if not workspace_id:
        return model
    from . import infra
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    with infra.pg() as c:
        row = c.execute("SELECT limit_usd, spent_usd FROM workspace_budgets WHERE workspace_id=%s AND period_ym=%s",
                        (workspace_id, period)).fetchone()
    if row is None:
        return model
    est = cost_of(model, {"prompt_tokens": prompt_tokens, "output_tokens": EST_OUTPUT_TOKENS})
    d = budget.check_budget(row["limit_usd"], row["spent_usd"], est, model, ALIASES["cheap"])
    if d.action == "HALT":
        raise BudgetHalt(f"budget exhausted ({d.ratio:.2%})")
    return d.model


def charge(workspace_id, run_id, node, model, usage, ms):
    from . import infra, pgstore
    cost = cost_of(model, usage)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    with infra.pg() as c:
        if workspace_id:
            c.execute("UPDATE workspace_budgets SET spent_usd = spent_usd + %s WHERE workspace_id=%s AND period_ym=%s",
                      (cost, workspace_id, period))
        if run_id:
            pgstore.record_execution(c, run_id, node or "-", model, usage, cost, ms)
        c.commit()


class Handler(BaseHTTPRequestHandler):
    upstream = os.environ.get("UPSTREAM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    upstream_key = os.environ.get("UPSTREAM_API_KEY", "")
    token = os.environ.get("LLM_GATEWAY_TOKEN", "internal")

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "aliases": ALIASES})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("authorization", "") != f"Bearer {self.token}":
            return self._send(401, {"error": "unauthorized"})
        if self.path not in ("/v1/chat/completions", "/v1/embeddings"):
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        ws = self.headers.get("x-agdd-workspace") or None
        run_id = self.headers.get("x-agdd-run") or None
        node = self.headers.get("x-agdd-node") or None
        task = self.headers.get("x-agdd-task", "implement")
        if ws:
            from . import infra
            try:
                if not infra.rate_limit(f"ws:{ws}", int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))):
                    return self._send(429, {"error": "rate limited"})
            except Exception:
                pass  # Valkey down must not stop the pipeline
        model = resolve_model(body.get("model", "mid"))
        if self.path.endswith("completions"):
            body = normalize(body, task)
            est_prompt = gateway.estimate_tokens(json.dumps(body.get("messages", []), ensure_ascii=False))
            try:
                model = budget_gate(ws, model, est_prompt)
            except BudgetHalt as e:
                return self._send(402, {"error": {"type": "budget_halt", "message": str(e)}})
        body["model"] = model
        body = structured(body)
        req = urllib.request.Request(self.upstream + self.path[3:], data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {self.upstream_key}"})
        t0 = time.time()
        try:
            for attempt in range(MAX_RETRIES + 1):
                wait_slot()
                try:
                    with urllib.request.urlopen(req, timeout=float(os.environ.get("UPSTREAM_TIMEOUT", "300"))) as r:
                        resp = json.loads(r.read())
                    break
                except urllib.error.HTTPError as e:
                    if e.code not in (429, 503) or attempt == MAX_RETRIES:
                        raise
                    e.read()
                    time.sleep(min(30.0, MIN_INTERVAL * 2 ** attempt))
        except urllib.error.HTTPError as e:
            print(f"[gateway] task={task} model={model} status={e.code} ms={int((time.time() - t0) * 1000)}", flush=True)
            return self._send(e.code, json.loads(e.read() or b"{}") or {"error": str(e)})
        except Exception as e:
            print(f"[gateway] task={task} model={model} error={e!r:.200}", flush=True)
            return self._send(502, {"error": repr(e)[:300]})
        ms = int((time.time() - t0) * 1000)
        u = usage_of(resp)
        print(f"[gateway] task={task} model={model} status=200 ms={ms} prompt={u['prompt_tokens']} "
              f"cached={u['cached_tokens']} out={u['output_tokens']} thinking={u['thinking_tokens']}", flush=True)
        try:
            charge(ws, run_id, node, model, usage_of(resp), ms)
        except Exception as e:
            print(f"[gateway] accounting failed: {e!r}", flush=True)
        self._send(200, resp)


def main():
    port = int(os.environ.get("PORT", "8090"))
    print(f"[llm-gateway] listening :{port} aliases={ALIASES}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
