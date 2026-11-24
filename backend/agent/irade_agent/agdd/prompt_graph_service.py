"""Persistence and background execution service for Prompt Dependency Graphs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from typing import Any

from psycopg.types.json import Jsonb

from . import api_agents, infra, prompt_graph, spec_store, vm_service
from .prompt_graph import (
    PromptGraphError,
    PromptGraphRun,
    auto_layout_graph,
    call_prompt_graph_admin_mcp_tool,
    detect_layout_overlaps,
    extract_group_template,
    instantiate_group_template,
    manage_prompt_graph_with_mcp,
    plan_execution,
    validate_prompt_graph,
)

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


def _make_artifact_reader(ws):
    def _reader(node: dict[str, Any], _graph: dict[str, Any]):
        art = node.get("artifact") or {}
        kind = art.get("kind") or "none"
        path = (art.get("path") or "").strip()
        if kind == "none" or not path:
            return None
        if kind == "spec_query":
            with infra.pg(readonly=True) as c:
                res = spec_store.query_readonly(c, ws, path, limit=100)
            text = json.dumps(res, ensure_ascii=False, default=str)
            return text, hashlib.sha256(text.encode("utf-8")).hexdigest()
        if kind == "repo_file":
            from . import api as agdd_api
            data = agdd_api._ide(ws, op="file", repo=art.get("repo") or "workspace", path=path)
            content = str(data.get("content") or "")
            return content, hashlib.sha256(content.encode("utf-8")).hexdigest()
        return None
    return _reader


def _make_artifact_writer(ws):
    def _writer(node: dict[str, Any], output_str: str, _graph: dict[str, Any]):
        art = node.get("artifact") or {}
        if art.get("kind") != "repo_file":
            return None
        path = (art.get("path") or "").strip()
        if not path:
            return None
        from . import api as agdd_api
        agdd_api._ide(ws, op="write", repo=art.get("repo") or "workspace", path=path, content=output_str)
        return hashlib.sha256(output_str.encode("utf-8")).hexdigest()
    return _writer


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

    def cache_lookup(input_hash: str) -> dict[str, Any] | None:
        try:
            with infra.pg(readonly=True) as c:
                row = c.execute(
                    "SELECT output, model, prompt_tokens, completion_tokens, total_tokens "
                    "FROM prompt_node_versions "
                    "WHERE workspace_id=%s AND input_hash=%s AND status='done' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (ws, input_hash),
                ).fetchone()
            if row is None:
                return None
            return {
                "output": row.get("output") or "",
                "model": row.get("model") or "",
                "usage": {
                    "prompt_tokens": int(row.get("prompt_tokens") or 0),
                    "completion_tokens": int(row.get("completion_tokens") or 0),
                    "total_tokens": int(row.get("total_tokens") or 0),
                },
            }
        except Exception:
            return None

    def on_node_version(ver: dict[str, Any]) -> None:
        try:
            usage = ver.get("usage") or {}
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            tt = int(usage.get("total_tokens") or (pt + ct))
            with infra.pg() as c:
                c.execute(
                    "INSERT INTO prompt_node_versions "
                    "(workspace_id, graph_id, run_id, node_id, node_label, iteration, input_hash, "
                    "prompt, temp_prompt, effective_pre_prompt, output, model, status, contract_passed, "
                    "contract_errors, prompt_tokens, completion_tokens, total_tokens, cost_usd, duration_ms, cache_hit) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        ws,
                        graph_id or None,
                        rid,
                        ver["node_id"],
                        ver.get("node_label") or ver["node_id"],
                        int(ver.get("iteration") or 1),
                        ver["input_hash"],
                        ver.get("prompt") or "",
                        ver.get("temp_prompt") or "",
                        ver.get("effective_pre_prompt") or "",
                        ver.get("output") or "",
                        ver.get("model") or "",
                        ver.get("status") or "done",
                        ver.get("contract_passed"),
                        Jsonb(ver.get("contract_errors") or []),
                        pt,
                        ct,
                        tt,
                        float(ver.get("cost_usd") or 0.0),
                        int(ver.get("ms") or 0),
                        bool(ver.get("cache_hit", False)),
                    ),
                )
                c.commit()
        except Exception as exc:
            print(f"[agdd-pg] node version write failed: {exc}", flush=True)

    def on_impact_review(rev: dict[str, Any]) -> None:
        try:
            dec = rev.get("decision") or ("PENDING" if rev.get("needs_rerun") else "DISMISSED")
            with infra.pg() as c:
                c.execute(
                    "INSERT INTO prompt_node_impact_reviews "
                    "(workspace_id, graph_id, run_id, source_node_id, target_id, needs_rerun, reason, decision, decided_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        ws,
                        graph_id or None,
                        rid,
                        rev["source_node_id"],
                        rev["target_id"],
                        bool(rev.get("needs_rerun", False)),
                        rev.get("reason") or "",
                        dec,
                        None if dec == "PENDING" else "now()",
                    ),
                )
                c.commit()
        except Exception as exc:
            print(f"[agdd-pg] impact review write failed: {exc}", flush=True)

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
            cache_lookup=cache_lookup,
            on_node_version=on_node_version,
            on_impact_review=on_impact_review,
            artifact_reader=_make_artifact_reader(ws),
            artifact_writer=_make_artifact_writer(ws),
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


# ------------------------------------------------------------------ Versions, Search, Impact Reviews, Templates, Analytics & Artifacts
def list_node_versions(ws, graph_id, node_id: str, limit: int = 30) -> list[dict[str, Any]]:
    with infra.pg(readonly=True) as c:
        return c.execute(
            "SELECT version_id, run_id, node_id, node_label, iteration, input_hash, prompt, temp_prompt, "
            "effective_pre_prompt, output, model, status, contract_passed, contract_errors, "
            "prompt_tokens, completion_tokens, total_tokens, cost_usd, duration_ms, cache_hit, created_at "
            "FROM prompt_node_versions "
            "WHERE workspace_id=%s AND graph_id=%s AND node_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (ws, graph_id, node_id, int(limit)),
        ).fetchall()


def checkout_node_version(
    ws,
    graph_id,
    node_id: str,
    version_id: int,
    restore_prompt: bool = False,
    user=None,
) -> dict[str, Any]:
    with infra.pg(readonly=True) as c:
        ver = c.execute(
            "SELECT version_id, node_id, prompt, temp_prompt, output, model, created_at "
            "FROM prompt_node_versions "
            "WHERE workspace_id=%s AND graph_id=%s AND node_id=%s AND version_id=%s",
            (ws, graph_id, node_id, int(version_id)),
        ).fetchone()
        grow = c.execute(
            "SELECT graph_id, name, graph_json FROM prompt_graphs WHERE workspace_id=%s AND graph_id=%s",
            (ws, graph_id),
        ).fetchone()
    if ver is None:
        raise LookupError("node version not found")
    if grow is None:
        raise LookupError("prompt graph not found")

    graph = json.loads(json.dumps(grow["graph_json"]))
    node = next((n for n in graph.get("nodes", []) if n.get("id") == node_id), None)
    if node is None:
        raise LookupError("node not found in graph")

    node["output"] = ver.get("output") or ""
    node["completed"] = True
    node["needs_rerun"] = False
    node["rerun_reason"] = ""
    if restore_prompt:
        node["prompt"] = ver.get("prompt") or ""
        node["last_run_prompt"] = node["prompt"]
    else:
        node["last_run_prompt"] = ver.get("prompt") or ""
    saved = save_graph(ws, graph_id, grow["name"], graph, user)
    return {"graph": saved["graph_json"], "version": ver}


def search_workspace_nodes(ws, query: str, limit: int = 30) -> list[dict[str, Any]]:
    q = str(query or "").strip().lower()
    rows = list_graphs(ws)
    hits: list[dict[str, Any]] = []
    for row in rows:
        g = row.get("graph_json") or {}
        for n in g.get("nodes") or []:
            hay = "\n".join(
                [
                    str(n.get("id") or ""),
                    str(n.get("label") or ""),
                    str(n.get("prompt") or ""),
                    str(n.get("output") or ""),
                    str(n.get("model") or ""),
                ]
            ).lower()
            if q and q not in hay:
                continue
            hits.append(
                {
                    "graph_id": row["graph_id"],
                    "graph_name": row["name"],
                    "node_id": n.get("id") or "",
                    "node_label": n.get("label") or n.get("id") or "",
                    "model": n.get("model") or "",
                    "completed": bool(n.get("completed", False)),
                    "prompt_preview": str(n.get("prompt") or "")[:140],
                    "output_preview": str(n.get("output") or "")[:140],
                }
            )
            if len(hits) >= int(limit):
                return hits
    return hits


def list_impact_reviews(ws, graph_id, limit: int = 50) -> list[dict[str, Any]]:
    with infra.pg(readonly=True) as c:
        return c.execute(
            "SELECT review_id, run_id, source_node_id, target_id, needs_rerun, reason, decision, decided_at, created_at "
            "FROM prompt_node_impact_reviews "
            "WHERE workspace_id=%s AND graph_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (ws, graph_id, int(limit)),
        ).fetchall()


def dismiss_impact_review(
    ws,
    graph_id,
    entity_id: str,
    review_id: int | None = None,
    user=None,
) -> dict[str, Any]:
    with infra.pg() as c:
        grow = c.execute(
            "SELECT graph_id, name, graph_json FROM prompt_graphs WHERE workspace_id=%s AND graph_id=%s",
            (ws, graph_id),
        ).fetchone()
        if grow is None:
            raise LookupError("prompt graph not found")
        if review_id is not None:
            c.execute(
                "UPDATE prompt_node_impact_reviews SET decision='DISMISSED', decided_at=now() "
                "WHERE workspace_id=%s AND graph_id=%s AND review_id=%s",
                (ws, graph_id, int(review_id)),
            )
        elif entity_id:
            c.execute(
                "UPDATE prompt_node_impact_reviews SET decision='DISMISSED', decided_at=now() "
                "WHERE workspace_id=%s AND graph_id=%s AND target_id=%s AND decision='PENDING'",
                (ws, graph_id, entity_id),
            )
        c.commit()

    graph = json.loads(json.dumps(grow["graph_json"]))
    for col_key in ("nodes", "groups"):
        for ent in graph.get(col_key) or []:
            if ent.get("id") == entity_id:
                ent["needs_rerun"] = False
                ent["rerun_reason"] = ""
                if col_key == "nodes":
                    for h in ent.get("impact_history") or []:
                        if h.get("decision") == "PENDING":
                            h["decision"] = "DISMISSED"
    saved = save_graph(ws, graph_id, grow["name"], graph, user)
    return {"ok": True, "graph": saved["graph_json"]}


def list_templates(ws) -> list[dict[str, Any]]:
    with infra.pg(readonly=True) as c:
        return c.execute(
            "SELECT template_id, name, description, tier_depth, node_count, group_count, subtree_json, created_at, updated_at "
            "FROM prompt_graph_templates WHERE workspace_id=%s ORDER BY updated_at DESC",
            (ws,),
        ).fetchall()


def save_template(
    ws,
    name: str,
    description: str,
    graph: dict[str, Any],
    group_id: str,
    user=None,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()[:100] or "재사용 N-그룹 컴포넌트"
    clean_desc = str(description or "").strip()[:400]
    extracted = extract_group_template(graph, group_id)
    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        row = c.execute(
            "INSERT INTO prompt_graph_templates "
            "(workspace_id, name, description, tier_depth, node_count, group_count, subtree_json, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING template_id, name, description, tier_depth, node_count, group_count, subtree_json, created_at, updated_at",
            (
                ws,
                clean_name,
                clean_desc,
                extracted["tier_depth"],
                extracted["node_count"],
                extracted["group_count"],
                Jsonb(extracted["subtree_json"]),
                uid,
            ),
        ).fetchone()
        c.commit()
    return row


def delete_template(ws, template_id) -> bool:
    with infra.pg() as c:
        n = c.execute(
            "DELETE FROM prompt_graph_templates WHERE template_id=%s AND workspace_id=%s",
            (template_id, ws),
        ).rowcount
        c.commit()
    return bool(n)


def instantiate_template(
    ws,
    template_id,
    graph: dict[str, Any],
    offset_x: int = 80,
    offset_y: int = 80,
) -> dict[str, Any]:
    with infra.pg(readonly=True) as c:
        row = c.execute(
            "SELECT template_id, name, subtree_json FROM prompt_graph_templates "
            "WHERE template_id=%s AND workspace_id=%s",
            (template_id, ws),
        ).fetchone()
    if row is None:
        raise LookupError("prompt graph template not found")
    return instantiate_group_template(
        graph,
        row["subtree_json"],
        offset_x=int(offset_x),
        offset_y=int(offset_y),
    )


def graph_analytics(ws, graph_id) -> dict[str, Any]:
    with infra.pg(readonly=True) as c:
        rows = c.execute(
            "SELECT node_id, MAX(node_label) AS node_label, COUNT(*) AS executions, "
            "COALESCE(AVG(duration_ms), 0)::int AS avg_ms, "
            "COALESCE(AVG(total_tokens), 0)::int AS avg_tokens, "
            "COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens, "
            "COALESCE(SUM(cost_usd), 0)::numeric(12,6) AS total_cost_usd, "
            "COALESCE(SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END), 0)::int AS cache_hits "
            "FROM prompt_node_versions "
            "WHERE workspace_id=%s AND graph_id=%s "
            "GROUP BY node_id ORDER BY total_cost_usd DESC, avg_ms DESC",
            (ws, graph_id),
        ).fetchall()
    tot_cost = sum(float(r.get("total_cost_usd") or 0) for r in rows)
    tot_tokens = sum(int(r.get("total_tokens") or 0) for r in rows)
    tot_cache = sum(int(r.get("cache_hits") or 0) for r in rows)
    tot_execs = sum(int(r.get("executions") or 0) for r in rows)
    return {
        "nodes": [
            {
                **r,
                "total_cost_usd": float(r.get("total_cost_usd") or 0),
            }
            for r in rows
        ],
        "summary": {
            "total_executions": tot_execs,
            "total_tokens": tot_tokens,
            "total_cost_usd": round(tot_cost, 6),
            "cache_hits": tot_cache,
        },
    }


def sync_graph_artifacts(
    ws,
    graph: dict[str, Any],
    graph_id=None,
    user=None,
    artifact_reader=None,
) -> dict[str, Any]:
    clean = validate_prompt_graph(graph)
    reader = artifact_reader or _make_artifact_reader(ws)
    changed_nodes: list[str] = []
    checked = 0
    errors: list[dict[str, str]] = []

    for n in clean["nodes"]:
        art = n.get("artifact") or {}
        if art.get("kind", "none") == "none" or not (art.get("path") or "").strip():
            continue
        checked += 1
        try:
            loaded = reader(n, clean)
        except Exception as exc:
            errors.append({"node_id": n["id"], "error": str(exc)[:300]})
            continue
        if not loaded:
            continue
        _content, new_hash = str(loaded[0] or ""), str(loaded[1] or "")
        prev_hash = art.get("last_content_hash") or ""
        if new_hash != prev_hash:
            art["last_content_hash"] = new_hash
            art["last_synced_at"] = str(int(time.time()))
            if n.get("completed", False):
                n["needs_rerun"] = True
                n["rerun_reason"] = f"연결 산출물 변경 감지 ({art.get('path')})"
                changed_nodes.append(n["id"])

    if graph_id and (changed_nodes or checked):
        with infra.pg() as c:
            grow = c.execute(
                "SELECT name FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
                (graph_id, ws),
            ).fetchone()
        if grow is not None:
            saved = save_graph(ws, graph_id, grow["name"], clean, user)
            clean = saved["graph_json"]

    return {"graph": clean, "changed_nodes": changed_nodes, "checked": checked, "errors": errors}


def _load_or_use_graph(ws, graph_id, graph: dict[str, Any] | None) -> tuple[str | None, dict[str, Any]]:
    if graph is not None:
        return None, graph
    if not graph_id:
        raise PromptGraphError("graph or graph_id is required")
    with infra.pg(readonly=True) as c:
        row = c.execute(
            "SELECT name, graph_json FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
            (graph_id, ws),
        ).fetchone()
    if row is None:
        raise LookupError("prompt graph not found")
    return str(row["name"] or "새 프롬프트 그래프"), row["graph_json"]


def auto_layout_saved_graph(
    ws,
    graph: dict[str, Any] | None = None,
    *,
    graph_id=None,
    direction: str = "auto",
    save: bool = False,
    user=None,
) -> dict[str, Any]:
    gname, src_graph = _load_or_use_graph(ws, graph_id, graph)
    laid_out = auto_layout_graph(src_graph, direction=direction)
    report = detect_layout_overlaps(laid_out)
    if save and graph_id:
        with infra.pg(readonly=True) as c:
            row = c.execute(
                "SELECT name FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
                (graph_id, ws),
            ).fetchone()
        resolved_name = (row["name"] if row else None) or gname or "새 프롬프트 그래프"
        saved = save_graph(ws, graph_id, resolved_name, laid_out, user)
        laid_out = saved["graph_json"]
    return {"ok": True, "graph": laid_out, "layout_report": report}


def mcp_invoke_tool(
    ws,
    graph: dict[str, Any] | None,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    graph_id=None,
    save: bool = False,
    user=None,
) -> dict[str, Any]:
    gname, src_graph = _load_or_use_graph(ws, graph_id, graph)
    res = asyncio.run(call_prompt_graph_admin_mcp_tool(src_graph, tool_name, arguments or {}))
    if res["ok"] and save and graph_id:
        with infra.pg(readonly=True) as c:
            row = c.execute(
                "SELECT name FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
                (graph_id, ws),
            ).fetchone()
        resolved_name = (row["name"] if row else None) or gname or "새 프롬프트 그래프"
        saved = save_graph(ws, graph_id, resolved_name, res["graph"], user)
        res["graph"] = saved["graph_json"]
    return res


def mcp_manage_graph(
    ws,
    graph: dict[str, Any] | None,
    instruction: str,
    *,
    graph_id=None,
    save: bool = False,
    user=None,
    max_steps: int = 16,
    history: Any = None,
) -> dict[str, Any]:
    gname, src_graph = _load_or_use_graph(ws, graph_id, graph)
    default_model = (src_graph.get("settings") or {}).get("default_model") or workspace_default_model(ws)
    chat_fn = make_gateway_chat_fn(ws, f"mcp-admin-{graph_id or 'unsaved'}", default_model=default_model)

    async def _run():
        try:
            return await manage_prompt_graph_with_mcp(src_graph, instruction, chat_fn, max_steps=max_steps, history=history)
        finally:
            await chat_fn.aclose()

    out = asyncio.run(_run())
    if out.get("changed") and save and graph_id:
        with infra.pg(readonly=True) as c:
            row = c.execute(
                "SELECT name FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
                (graph_id, ws),
            ).fetchone()
        resolved_name = (row["name"] if row else None) or gname or "새 프롬프트 그래프"
        saved = save_graph(ws, graph_id, resolved_name, out["graph"], user)
        out["graph"] = saved["graph_json"]
    return {"ok": True, **out}


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
    "list_node_versions",
    "checkout_node_version",
    "search_workspace_nodes",
    "list_impact_reviews",
    "dismiss_impact_review",
    "list_templates",
    "save_template",
    "delete_template",
    "instantiate_template",
    "graph_analytics",
    "sync_graph_artifacts",
    "auto_layout_saved_graph",
    "mcp_invoke_tool",
    "mcp_manage_graph",
]
