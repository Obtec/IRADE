"""Persistence and background execution service for Prompt Dependency Graphs."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from psycopg.types.json import Jsonb

from . import api_agents, infra, prompt_graph, vm_service
from .prompt_graph import PromptGraphError, PromptGraphRun, plan_execution, validate_prompt_graph

MAX_ACTIVE_PER_WORKSPACE = 2
_ACTIVE: dict[str, threading.Event] = {}  # run_id -> cancel flag
_ACTIVE_WS: dict[str, set[str]] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ CRUD
def list_graphs(ws) -> list[dict[str, Any]]:
    with infra.pg(readonly=True) as c:
        return c.execute(
            "SELECT graph_id, name, graph_json, updated_at FROM prompt_graphs "
            "WHERE workspace_id=%s ORDER BY updated_at DESC",
            (ws,),
        ).fetchall()


def save_graph(ws, graph_id, name: str, graph: dict[str, Any], user) -> dict[str, Any]:
    clean_name = str(name or "").strip()[:100] or "새 프롬프트 그래프"
    clean_graph = validate_prompt_graph(graph)
    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        if graph_id:
            row = c.execute(
                "UPDATE prompt_graphs SET name=%s, graph_json=%s, updated_by=%s, updated_at=now() "
                "WHERE graph_id=%s AND workspace_id=%s RETURNING graph_id, name, graph_json, updated_at",
                (clean_name, Jsonb(clean_graph), uid, graph_id, ws),
            ).fetchone()
            if row is None:
                raise LookupError("prompt graph not found")
        else:
            row = c.execute(
                "INSERT INTO prompt_graphs (workspace_id, name, graph_json, updated_by) "
                "VALUES (%s,%s,%s,%s) RETURNING graph_id, name, graph_json, updated_at",
                (ws, clean_name, Jsonb(clean_graph), uid),
            ).fetchone()
        c.commit()
    return row


def delete_graph(ws, graph_id) -> bool:
    with infra.pg() as c:
        n = c.execute(
            "DELETE FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
            (graph_id, ws),
        ).rowcount
        c.commit()
    return bool(n)


def workspace_default_model(ws) -> str:
    try:
        return vm_service.load_settings(ws).model or "mid"
    except Exception:
        return "mid"


# ------------------------------------------------------------------ Gateway adapter
def make_gateway_chat_fn(ws, run_id: str, default_model: str = "mid"):
    """Return an async chat completion callable wired to the LLM Gateway."""
    from openai import APIStatusError, AsyncOpenAI

    base_url = infra.env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1")
    api_key = infra.env("LLM_GATEWAY_TOKEN", "internal")
    timeout = float(infra.env("AGDD_MODEL_TIMEOUT", "300"))
    state: dict[str, Any] = {"client": None}

    async def chat_fn(*, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], node: dict[str, Any], extra: dict[str, Any]):
        if state["client"] is None:
            state["client"] = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=1,
            )
        client: AsyncOpenAI = state["client"]
        resolved_model = (str(model or "").strip()) or (str(default_model or "").strip()) or "mid"

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "extra_headers": {
                "x-agdd-task": "prompt_graph",
                "x-agdd-workspace": str(ws),
                "x-agdd-run": str(run_id),
                "x-agdd-node": str(node.get("id") or ""),
            },
        }
        if tools:
            kwargs["tools"] = tools

        effort = str((extra or {}).get("effort") or "").strip().lower()
        if effort in api_agents.EFFORTS:
            kwargs["reasoning_effort"] = api_agents.gateway_effort(effort)

        temp = (extra or {}).get("temperature")
        if isinstance(temp, (int, float)) and not isinstance(temp, bool) and 0 <= float(temp) <= 2:
            kwargs["temperature"] = float(temp)

        try:
            return await client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            raise RuntimeError(f"모델 API 오류 ({exc.status_code}): {str(exc.message)[:300]}") from exc
        except Exception as exc:
            raise RuntimeError(f"모델 API 호출 실패: {str(exc)[:300]}") from exc

    async def aclose() -> None:
        if state["client"] is not None:
            try:
                await state["client"].close()
            finally:
                state["client"] = None

    chat_fn.aclose = aclose  # type: ignore[attr-defined]
    return chat_fn


# ------------------------------------------------------------------ Runs
def start_run(
    ws,
    graph_id,
    graph: dict[str, Any] | None,
    target_type: str = "all",
    target_id: str | None = None,
    user=None,
    reuse_existing_outputs: bool = False,
    target_ids: list[str] | None = None,
    force_rerun: bool = False,
) -> dict[str, Any]:
    if graph_id and graph is None:
        with infra.pg(readonly=True) as c:
            row = c.execute(
                "SELECT graph_json FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
                (graph_id, ws),
            ).fetchone()
        if row is None:
            raise LookupError("prompt graph not found")
        graph = row["graph_json"]

    planned = plan_execution(
        graph or {},
        target_type=target_type or "all",
        target_id=target_id,
        reuse_existing_outputs=bool(reuse_existing_outputs),
        target_ids=target_ids,
        force_rerun=bool(force_rerun),
    )
    clean_graph = planned["graph"]
    default_model = clean_graph.get("settings", {}).get("default_model") or workspace_default_model(ws)
    norm_target_ids = planned.get("target_ids") or []
    db_target_id = ",".join(norm_target_ids)[:200] if (target_type or "all") == "subset" else target_id

    with _lock:
        if len(_ACTIVE_WS.get(str(ws), set())) >= MAX_ACTIVE_PER_WORKSPACE:
            raise RuntimeError(
                f"이 워크스페이스에서 이미 {MAX_ACTIVE_PER_WORKSPACE}개의 프롬프트 그래프 실행이 진행 중입니다"
            )

    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        row = c.execute(
            "INSERT INTO prompt_graph_runs "
            "(graph_id, workspace_id, target_type, target_id, graph_json, status, created_by) "
            "VALUES (%s,%s,%s,%s,%s,'RUNNING',%s) RETURNING run_id, created_at",
            (graph_id or None, ws, target_type or "all", db_target_id, Jsonb(clean_graph), uid),
        ).fetchone()
        c.commit()

    rid = str(row["run_id"])
    cancel = threading.Event()
    with _lock:
        _ACTIVE[rid] = cancel
        _ACTIVE_WS.setdefault(str(ws), set()).add(rid)

    threading.Thread(
        target=_execute,
        args=(
            ws,
            rid,
            graph_id,
            clean_graph,
            target_type or "all",
            target_id,
            bool(reuse_existing_outputs),
            norm_target_ids,
            bool(force_rerun),
            default_model,
            cancel,
        ),
        name=f"agdd-pg-{rid[:8]}",
        daemon=True,
    ).start()
    return {
        "run_id": rid,
        "status": "RUNNING",
        "target_type": target_type or "all",
        "target_id": db_target_id,
        "target_ids": norm_target_ids,
        "run_nodes": planned["run_nodes"],
        "skipped_clean_nodes": planned["skipped_clean_nodes"],
    }


def _execute(
    ws,
    rid: str,
    graph_id,
    graph: dict[str, Any],
    target_type: str,
    target_id: str | None,
    reuse_existing_outputs: bool,
    target_ids: list[str] | None,
    force_rerun: bool,
    default_model: str,
    cancel: threading.Event,
) -> None:
    holder: dict[str, PromptGraphRun] = {}
    last_state = [0.0]

    def on_event(ev: dict[str, Any]) -> None:
        tr = holder.get("run")
        try:
            with infra.pg() as c:
                c.execute(
                    "INSERT INTO prompt_graph_events (run_id, seq, type, node, target, content, meta_json) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        rid,
                        ev["seq"],
                        ev["type"],
                        ev["node"],
                        ev["target"],
                        ev["content"],
                        Jsonb(json.loads(json.dumps(ev["meta"], default=str))),
                    ),
                )
                if tr is not None and (
                    ev["type"] in ("node_started", "node_finished", "node_failed", "node_skipped")
                    or time.time() - last_state[0] > 2.0
                ):
                    c.execute(
                        "UPDATE prompt_graph_runs SET node_state=%s, graph_json=%s, updated_at=now() "
                        "WHERE run_id=%s",
                        (Jsonb(tr.snapshot()), Jsonb(tr.graph_snapshot()), rid),
                    )
                    last_state[0] = time.time()
                c.commit()
        except Exception as exc:
            print(f"[agdd-pg] event write failed: {exc}", flush=True)

    chat_fn = make_gateway_chat_fn(ws, rid, default_model=default_model)

    async def _run_async() -> dict[str, Any]:
        tr = PromptGraphRun(
            graph,
            chat_fn,
            target_type=target_type,
            target_id=target_id,
            reuse_existing_outputs=reuse_existing_outputs,
            target_ids=target_ids,
            force_rerun=force_rerun,
            on_event=on_event,
            cancel=cancel,
        )
        holder["run"] = tr
        try:
            return await tr.run()
        finally:
            await chat_fn.aclose()

    try:
        out = asyncio.run(_run_async())
        status = out["status"]
        result = out["result"]
        state = out["node_state"]
        final_graph = out["graph"]
    except Exception as exc:
        status = "FAILED"
        result = {"error": str(exc)[:4000]}
        state = holder["run"].snapshot() if "run" in holder else {}
        final_graph = holder["run"].graph_snapshot() if "run" in holder else graph
    finally:
        with _lock:
            _ACTIVE.pop(rid, None)
            _ACTIVE_WS.get(str(ws), set()).discard(rid)

    try:
        with infra.pg() as c:
            c.execute(
                "UPDATE prompt_graph_runs SET status=%s, result_json=%s, node_state=%s, graph_json=%s, "
                "updated_at=now() WHERE run_id=%s",
                (
                    status,
                    Jsonb(json.loads(json.dumps(result, default=str))),
                    Jsonb(state),
                    Jsonb(final_graph),
                    rid,
                ),
            )
            if graph_id:
                c.execute(
                    "UPDATE prompt_graphs SET graph_json=%s, updated_at=now() "
                    "WHERE graph_id=%s AND workspace_id=%s",
                    (Jsonb(final_graph), graph_id, ws),
                )
            c.commit()
    except Exception as exc:
        print(f"[agdd-pg] finish write failed: {exc}", flush=True)


def cancel_run(ws, rid) -> bool:
    with _lock:
        ev = _ACTIVE.get(str(rid))
    if ev is None:
        return False
    ev.set()
    return True


def get_run(ws, rid, after: int = 0) -> dict[str, Any] | None:
    with infra.pg() as c:
        run = c.execute(
            "SELECT run_id, graph_id, target_type, target_id, graph_json, status, node_state, "
            "result_json, created_at, updated_at, (updated_at < now() - interval '5 minutes') AS stale "
            "FROM prompt_graph_runs WHERE run_id=%s AND workspace_id=%s",
            (rid, ws),
        ).fetchone()
        if run is None:
            return None
        with _lock:
            alive = str(rid) in _ACTIVE
        if run["status"] == "RUNNING" and not alive and run["stale"]:
            c.execute(
                "UPDATE prompt_graph_runs SET status='FAILED', result_json=%s, updated_at=now() "
                "WHERE run_id=%s",
                (Jsonb({"error": "agdd-api restarted during the run"}), rid),
            )
            c.commit()
            run["status"] = "FAILED"
        events = c.execute(
            "SELECT seq, type, node, target, content, meta_json, created_at "
            "FROM prompt_graph_events WHERE run_id=%s AND seq > %s ORDER BY seq LIMIT 500",
            (rid, int(after)),
        ).fetchall()
    run.pop("stale", None)
    run["events"] = events
    run["active"] = alive
    return run


def list_runs(ws, graph_id=None, limit: int = 20) -> list[dict[str, Any]]:
    q = (
        "SELECT run_id, graph_id, target_type, target_id, status, created_at, updated_at "
        "FROM prompt_graph_runs WHERE workspace_id=%s"
        + (" AND graph_id=%s" if graph_id else "")
        + " ORDER BY created_at DESC LIMIT %s"
    )
    args = (ws, graph_id, int(limit)) if graph_id else (ws, int(limit))
    with infra.pg(readonly=True) as c:
        return c.execute(q, args).fetchall()


__all__ = [
    "PromptGraphError",
    "list_graphs",
    "save_graph",
    "delete_graph",
    "start_run",
    "get_run",
    "list_runs",
    "cancel_run",
    "make_gateway_chat_fn",
]
