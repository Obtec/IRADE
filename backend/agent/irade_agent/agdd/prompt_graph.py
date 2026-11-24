"""Prompt dependency graph & nested pre-prompt group engine with real MCP server inspection.

Data model
----------
A prompt graph is::

    {
        "nodes": [
            {
                "id": "n1",
                "label": "Node 1",
                "prompt": "...",
                "output": "...",
                "model": "",
                "dependencies": ["n0"],
                "extra": {...},
                "parent_group_id": "g1" | None,
                "x": 100, "y": 100, "width": 250, "height": 140,
            },
            ...
        ],
        "groups": [
            {
                "id": "g1",
                "label": "Group 1",
                "pre_prompt": "...",
                "node_ids": ["n1"],
                "child_group_ids": ["g2"],
                "parent_group_id": None | "g0",
                "dependencies": ["g0_dep"],
                "x": 60, "y": 60, "width": 360, "height": 240,
            },
            ...
        ],
        "edges": [
            {"id": "e1", "from": "A", "to": "B"},  # "A depends on B" -> B executes first; A reads B via MCP
            ...
        ],
        "settings": {
            "parallelism": 4,
            "max_tool_steps": 8,
            "default_model": "",
        },
    }
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import PurePosixPath
import re
import threading
import time
from typing import Any, Awaitable, Callable

import anyio
from mcp import ClientSession
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import TextContent, Tool

MAX_NODES = 200
MAX_GROUPS = 200
MAX_EDGES = 600
MAX_PROMPT_CHARS = 32000
MAX_OUTPUT_CHARS = 100000
MAX_EXTRA_CHARS = 32000
MAX_TOOL_RESULT_CHARS = 120000

_ID_RX = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_MODEL_RX = re.compile(r"[A-Za-z0-9._:/\[\]-]{1,80}")

CONDITION_TYPES = (
    "always",
    "on_contract_pass",
    "on_contract_fail",
    "contains",
    "not_contains",
    "regex",
    "json_eq",
    "agent_decide",
)


class PromptGraphError(ValueError):
    """Validation or execution planning error for a prompt dependency graph."""


def _s(value: Any, limit: int, default: str = "") -> str:
    if value is None:
        return default
    return str(value)[:limit]


def _int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _id_list(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PromptGraphError(f"{where}: must be a list of IDs")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise PromptGraphError(f"{where}: IDs must be non-empty strings")
        val = item.strip()
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _str_list(raw: Any, limit: int = 200, max_items: int = 30) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [line.strip() for line in raw.splitlines()]
    elif isinstance(raw, list):
        items = [str(item).strip() for item in raw if item is not None]
    else:
        return []
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item[:limit])
        if len(out) >= max_items:
            break
    return out


def _normalize_contract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    on_fail = str(raw.get("on_fail") or "fail").strip().lower()
    if on_fail not in ("fail", "continue"):
        on_fail = "fail"
    return {
        "enabled": bool(raw.get("enabled", False)),
        "json_required": bool(raw.get("json_required", False)),
        "json_keys": _str_list(raw.get("json_keys"), 100, 30),
        "must_include": _str_list(raw.get("must_include"), 200, 30),
        "must_exclude": _str_list(raw.get("must_exclude"), 200, 30),
        "regex": _s(raw.get("regex"), 400).strip(),
        "min_chars": _int(raw.get("min_chars", 0), 0, 50000, 0),
        "max_retries": _int(raw.get("max_retries", 0), 0, 5, 0),
        "on_fail": on_fail,
    }


def _normalize_artifact(raw: Any, nid: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    kind = str(raw.get("kind") or "none").strip().lower()
    if kind not in ("none", "repo_file", "spec_query"):
        kind = "none"
    mode = str(raw.get("mode") or "read").strip().lower()
    if mode not in ("read", "write", "sync"):
        mode = "read"
    repo = _s(raw.get("repo"), 64).strip() or ("workspace" if kind == "repo_file" else "")
    path = _s(raw.get("path"), 240).strip()
    if kind == "repo_file" and path:
        if path.startswith("/") or "\\" in path or ".." in PurePosixPath(path).parts:
            raise PromptGraphError(f"node {nid}: invalid repo artifact path {path!r}")
    return {
        "kind": kind,
        "repo": repo,
        "path": path,
        "mode": mode,
        "last_content_hash": _s(raw.get("last_content_hash"), 64).strip(),
        "last_synced_at": _s(raw.get("last_synced_at"), 64).strip(),
    }


def _normalize_condition(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    ctype = str(raw.get("type") or "always").strip().lower()
    if ctype not in CONDITION_TYPES:
        ctype = "always"
    return {
        "type": ctype,
        "value": _s(raw.get("value"), 500),
        "case_sensitive": bool(raw.get("case_sensitive", False)),
    }


# ------------------------------------------------------------------ validation
def validate_prompt_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a prompt graph, syncing memberships, dependencies, and checking DAG acyclicity."""
    if not isinstance(graph, dict):
        raise PromptGraphError("prompt graph must be an object")
    raw_nodes = graph.get("nodes", [])
    raw_groups = graph.get("groups", [])
    raw_edges = graph.get("edges", [])
    raw_settings = graph.get("settings") or {}

    if not isinstance(raw_nodes, list):
        raise PromptGraphError("prompt graph nodes must be a list")
    if not isinstance(raw_groups, list):
        raise PromptGraphError("prompt graph groups must be a list")
    if not isinstance(raw_edges, list):
        raise PromptGraphError("prompt graph edges must be a list")
    if not isinstance(raw_settings, dict):
        raise PromptGraphError("prompt graph settings must be an object")

    if len(raw_nodes) > MAX_NODES:
        raise PromptGraphError(f"at most {MAX_NODES} prompt nodes allowed")
    if len(raw_groups) > MAX_GROUPS:
        raise PromptGraphError(f"at most {MAX_GROUPS} pre-prompt groups allowed")
    if len(raw_edges) > MAX_EDGES:
        raise PromptGraphError(f"at most {MAX_EDGES} dependency edges allowed")

    used_ids: set[str] = set()
    nodes: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []

    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise PromptGraphError("node must be an object")
        nid = str(raw.get("id") or "").strip()
        if not _ID_RX.fullmatch(nid):
            raise PromptGraphError(
                f"node id {nid!r}: must start with a letter and use letters/digits/_/- (max 40)"
            )
        if nid in used_ids:
            raise PromptGraphError(f"duplicate entity id {nid!r}")
        used_ids.add(nid)

        model = _s(raw.get("model"), 80).strip()
        if model and not _MODEL_RX.fullmatch(model):
            raise PromptGraphError(f"node {nid}: invalid model name {model!r}")

        extra = raw.get("extra")
        if extra is None:
            extra = {}
        if not isinstance(extra, dict):
            raise PromptGraphError(f"node {nid}: extra must be an object")
        extra_copy = deepcopy(extra)
        if len(json.dumps(extra_copy, ensure_ascii=False, default=str)) > MAX_EXTRA_CHARS:
            raise PromptGraphError(f"node {nid}: extra metadata exceeds {MAX_EXTRA_CHARS} chars")

        pgid = raw.get("parent_group_id")
        if pgid is not None:
            pgid = str(pgid).strip() or None

        prompt_val = _s(raw.get("prompt"), MAX_PROMPT_CHARS)
        completed_val = bool(raw.get("completed", False))
        if "last_run_prompt" in raw and raw["last_run_prompt"] is not None:
            last_run_prompt = _s(raw["last_run_prompt"], MAX_PROMPT_CHARS)
        else:
            last_run_prompt = prompt_val if completed_val else None

        if "last_run_temp_prompt" in raw and raw["last_run_temp_prompt"] is not None:
            last_run_temp_prompt = _s(raw["last_run_temp_prompt"], MAX_PROMPT_CHARS)
        else:
            last_run_temp_prompt = "" if completed_val else None

        raw_lrpp = raw.get("last_run_pre_prompts")
        if "last_run_pre_prompts" in raw and isinstance(raw_lrpp, dict):
            last_run_pre_prompts: dict[str, dict[str, str]] | None = {
                str(gid): {
                    "pre": _s(v.get("pre"), MAX_PROMPT_CHARS),
                    "temp": _s(v.get("temp"), MAX_PROMPT_CHARS),
                }
                for gid, v in raw_lrpp.items()
                if isinstance(v, dict)
            }
        else:
            last_run_pre_prompts = None

        needs_rerun_val = bool(raw.get("needs_rerun", raw.get("stale_by_dep", False))) if completed_val else False
        raw_imp = raw.get("impact_history")
        impact_history = (
            [
                {
                    "source_node_id": _s(item.get("source_node_id"), 40),
                    "needs_rerun": bool(item.get("needs_rerun", False)),
                    "reason": _s(item.get("reason"), 400),
                    "decision": _s(item.get("decision"), 20) or "PENDING",
                    "created_at": _s(item.get("created_at"), 64),
                }
                for item in raw_imp[:20]
                if isinstance(item, dict)
            ]
            if isinstance(raw_imp, list)
            else []
        )

        nodes.append(
            {
                "id": nid,
                "label": _s(raw.get("label"), 100, nid).strip() or nid,
                "prompt": prompt_val,
                "temp_prompt": _s(raw.get("temp_prompt"), MAX_PROMPT_CHARS),
                "output": _s(raw.get("output"), MAX_OUTPUT_CHARS),
                "model": model,
                "dependencies": _id_list(raw.get("dependencies"), f"node {nid}.dependencies"),
                "extra": extra_copy,
                "contract": _normalize_contract(raw.get("contract")),
                "artifact": _normalize_artifact(raw.get("artifact"), nid),
                "impact_history": impact_history,
                "completed": completed_val,
                "last_run_prompt": last_run_prompt,
                "last_run_temp_prompt": last_run_temp_prompt,
                "last_run_pre_prompts": last_run_pre_prompts,
                "needs_rerun": needs_rerun_val,
                "rerun_reason": _s(raw.get("rerun_reason"), 1000) if needs_rerun_val else "",
                "parent_group_id": pgid,
                "x": _int(raw.get("x", 80), -20000, 50000, 80),
                "y": _int(raw.get("y", 80), -20000, 50000, 80),
                "width": _int(raw.get("width", 250), 140, 1200, 250),
                "height": _int(raw.get("height", 140), 80, 900, 140),
            }
        )

    claimed_node_lists: dict[str, list[str]] = {}
    claimed_group_lists: dict[str, list[str]] = {}

    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise PromptGraphError("group must be an object")
        gid = str(raw.get("id") or "").strip()
        if not _ID_RX.fullmatch(gid):
            raise PromptGraphError(
                f"group id {gid!r}: must start with a letter and use letters/digits/_/- (max 40)"
            )
        if gid in used_ids:
            raise PromptGraphError(f"duplicate entity id {gid!r}")
        used_ids.add(gid)

        pgid = raw.get("parent_group_id")
        if pgid is not None:
            pgid = str(pgid).strip() or None
        if pgid == gid:
            raise PromptGraphError(f"group {gid}: cannot be its own parent")

        claimed_node_lists[gid] = _id_list(raw.get("node_ids"), f"group {gid}.node_ids")
        claimed_group_lists[gid] = _id_list(raw.get("child_group_ids"), f"group {gid}.child_group_ids")

        pre_prompt_val = _s(raw.get("pre_prompt"), MAX_PROMPT_CHARS)
        g_completed = bool(raw.get("completed", False))
        if "last_run_pre_prompt" in raw and raw["last_run_pre_prompt"] is not None:
            last_run_pre_prompt = _s(raw["last_run_pre_prompt"], MAX_PROMPT_CHARS)
        else:
            last_run_pre_prompt = pre_prompt_val if g_completed else None

        if "last_run_temp_pre_prompt" in raw and raw["last_run_temp_pre_prompt"] is not None:
            last_run_temp_pre_prompt = _s(raw["last_run_temp_pre_prompt"], MAX_PROMPT_CHARS)
        else:
            last_run_temp_pre_prompt = "" if g_completed else None

        g_needs_rerun = bool(raw.get("needs_rerun", raw.get("stale_by_dep", False))) if g_completed else False

        groups.append(
            {
                "id": gid,
                "label": _s(raw.get("label"), 100, gid).strip() or gid,
                "pre_prompt": pre_prompt_val,
                "temp_pre_prompt": _s(raw.get("temp_pre_prompt"), MAX_PROMPT_CHARS),
                "node_ids": [],
                "child_group_ids": [],
                "parent_group_id": pgid,
                "dependencies": _id_list(raw.get("dependencies"), f"group {gid}.dependencies"),
                "completed": g_completed,
                "last_run_pre_prompt": last_run_pre_prompt,
                "last_run_temp_pre_prompt": last_run_temp_pre_prompt,
                "needs_rerun": g_needs_rerun,
                "rerun_reason": _s(raw.get("rerun_reason"), 1000) if g_needs_rerun else "",
                "x": _int(raw.get("x", 40), -20000, 50000, 40),
                "y": _int(raw.get("y", 40), -20000, 50000, 40),
                "width": _int(raw.get("width", 360), 180, 20000, 360),
                "height": _int(raw.get("height", 240), 120, 20000, 240),
            }
        )

    node_by_id = {n["id"]: n for n in nodes}
    group_by_id = {g["id"]: g for g in groups}

    # Validate explicit parent_group_id references
    for n in nodes:
        if n["parent_group_id"] is not None and n["parent_group_id"] not in group_by_id:
            raise PromptGraphError(f"node {n['id']}: unknown parent_group_id {n['parent_group_id']!r}")
    for g in groups:
        if g["parent_group_id"] is not None and g["parent_group_id"] not in group_by_id:
            raise PromptGraphError(f"group {g['id']}: unknown parent_group_id {g['parent_group_id']!r}")

    # Reconcile group.node_ids / group.child_group_ids if caller specified them without setting child.parent_group_id
    for gid, nids in claimed_node_lists.items():
        for nid in nids:
            if nid not in node_by_id:
                raise PromptGraphError(f"group {gid}: unknown node_id {nid!r}")
            cur_parent = node_by_id[nid]["parent_group_id"]
            if cur_parent is None:
                node_by_id[nid]["parent_group_id"] = gid
            elif cur_parent != gid:
                raise PromptGraphError(f"node {nid}: conflicting parent groups ({cur_parent!r} vs {gid!r})")

    for gid, cgids in claimed_group_lists.items():
        for cgid in cgids:
            if cgid not in group_by_id:
                raise PromptGraphError(f"group {gid}: unknown child_group_id {cgid!r}")
            if cgid == gid:
                raise PromptGraphError(f"group {gid}: cannot contain itself")
            cur_parent = group_by_id[cgid]["parent_group_id"]
            if cur_parent is None:
                group_by_id[cgid]["parent_group_id"] = gid
            elif cur_parent != gid:
                raise PromptGraphError(f"group {cgid}: conflicting parent groups ({cur_parent!r} vs {gid!r})")

    # Detect group containment cycles
    for g in groups:
        seen_chain: set[str] = set()
        cur: str | None = g["id"]
        while cur is not None:
            if cur in seen_chain:
                raise PromptGraphError(f"group containment cycle involving {cur!r}")
            seen_chain.add(cur)
            cur = group_by_id[cur]["parent_group_id"]

    # Recompute authoritative node_ids and child_group_ids from parent_group_id
    for n in nodes:
        if n["parent_group_id"] is not None:
            group_by_id[n["parent_group_id"]]["node_ids"].append(n["id"])
    for g in groups:
        if g["parent_group_id"] is not None:
            group_by_id[g["parent_group_id"]]["child_group_ids"].append(g["id"])

    # Populate missing ancestor pre-prompt baselines for already-completed nodes
    tmp_graph = {"nodes": nodes, "groups": groups}
    for n in nodes:
        if n["completed"] and n["last_run_pre_prompts"] is None:
            n["last_run_pre_prompts"] = {
                g["id"]: {"pre": g["pre_prompt"], "temp": ""}
                for g in ancestor_groups(tmp_graph, n["id"])
            }

    # Compute subtree entity IDs for every entity (node or group)
    subtree_cache: dict[str, set[str]] = {}

    def subtree(entity_id: str) -> set[str]:
        if entity_id in subtree_cache:
            return subtree_cache[entity_id]
        if entity_id in node_by_id:
            res = {entity_id}
        else:
            g = group_by_id[entity_id]
            res = {entity_id}
            res.update(g["node_ids"])
            for cgid in g["child_group_ids"]:
                res.update(subtree(cgid))
        subtree_cache[entity_id] = res
        return res

    # Normalize edges and deduplicate (from, to, is_loop)
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, bool]] = set()
    used_edge_ids: set[str] = set()

    for idx, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise PromptGraphError("edge must be an object")
        frm = str(raw.get("from") or "").strip()
        to = str(raw.get("to") or "").strip()
        is_loop = bool(raw.get("is_loop", False))
        if frm not in used_ids:
            raise PromptGraphError(f"edge from unknown entity {frm!r}")
        if to not in used_ids:
            raise PromptGraphError(f"edge to unknown entity {to!r}")
        if frm == to and not is_loop:
            raise PromptGraphError(f"self dependency is not allowed on {frm!r}")
        if frm != to and (subtree(frm) & subtree(to)):
            raise PromptGraphError(
                f"invalid dependency {frm!r} -> {to!r}: ancestor and descendant cannot depend on each other"
            )
        if (frm, to, is_loop) in seen_pairs:
            continue
        seen_pairs.add((frm, to, is_loop))
        eid = str(raw.get("id") or f"e{idx + 1}").strip() or f"e{idx + 1}"
        while eid in used_edge_ids:
            eid = f"{eid}_{len(used_edge_ids) + 1}"
        used_edge_ids.add(eid)
        max_iter = _int(raw.get("max_iterations", 3 if is_loop else 0), 1 if is_loop else 0, 20, 3 if is_loop else 0)
        condition = _normalize_condition(raw.get("condition"))
        label = _s(raw.get("label"), 80).strip()
        edge_temp = _s(raw.get("temp_prompt"), MAX_PROMPT_CHARS)
        edges.append(
            {
                "id": eid,
                "from": frm,
                "to": to,
                "is_loop": is_loop,
                "max_iterations": max_iter,
                "condition": condition,
                "label": label,
                "temp_prompt": edge_temp,
            }
        )

    # Immediate synchronization of node/group dependencies field from non-loop edges
    deps_map: dict[str, list[str]] = {eid: [] for eid in used_ids}
    for e in edges:
        if not e["is_loop"]:
            deps_map[e["from"]].append(e["to"])
    for n in nodes:
        n["dependencies"] = deps_map[n["id"]]
    for g in groups:
        g["dependencies"] = deps_map[g["id"]]

    # Full hierarchical cycle detection on non-loop edges (loop edges explicitly permit controlled cycles):
    # Expand every non-loop edge u -> v to x -> y for all x in subtree(u), y in subtree(v).
    expanded_adj: dict[str, set[str]] = {eid: set() for eid in used_ids}
    for e in edges:
        if e["is_loop"]:
            continue
        src_members = subtree(e["from"])
        dst_members = subtree(e["to"])
        for x in src_members:
            expanded_adj[x].update(dst_members)

    color: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = 1
        stack.append(u)
        for v in sorted(expanded_adj[u]):
            st = color.get(v, 0)
            if st == 1:
                cycle_start = stack.index(v)
                cycle_path = " -> ".join([*stack[cycle_start:], v])
                raise PromptGraphError(f"dependency cycle detected: {cycle_path}")
            if st == 0:
                dfs(v)
        stack.pop()
        color[u] = 2

    for eid in sorted(used_ids):
        if color.get(eid, 0) == 0:
            dfs(eid)

    # Edge connection invariant: completed entities cannot depend on unexecuted entities on non-loop edges
    def _is_completed(eid: str) -> bool:
        if eid in node_by_id:
            return bool(node_by_id[eid]["completed"])
        return bool(group_by_id[eid]["completed"])

    for e in edges:
        if e["is_loop"]:
            continue
        comp_from = [x for x in sorted(subtree(e["from"])) if _is_completed(x)]
        uncomp_to = [y for y in sorted(subtree(e["to"])) if not _is_completed(y)]
        if comp_from and uncomp_to:
            raise PromptGraphError(
                f"완료된 대상({comp_from[0]})은 미실행 대상({uncomp_to[0]})에 의존하도록 연결할 수 없습니다."
            )

    default_model = _s(raw_settings.get("default_model"), 80).strip()
    if default_model and not _MODEL_RX.fullmatch(default_model):
        raise PromptGraphError(f"settings.default_model: invalid model name {default_model!r}")

    edge_dir = str(raw_settings.get("edge_direction") or "dependency").strip().lower()
    if edge_dir not in ("dependency", "execution"):
        edge_dir = "dependency"

    settings = {
        "parallelism": _int(raw_settings.get("parallelism", 4), 1, 8, 4),
        "max_tool_steps": _int(raw_settings.get("max_tool_steps", 8), 1, 16, 8),
        "default_model": default_model,
        "auto_rerun_needs_rerun": bool(raw_settings.get("auto_rerun_needs_rerun", False)),
        "edge_direction": edge_dir,
        "cross_graph_cache": bool(raw_settings.get("cross_graph_cache", False)),
    }

    return {
        "nodes": nodes,
        "groups": groups,
        "edges": edges,
        "settings": settings,
    }


# ------------------------------------------------------------------ hierarchy & dependency helpers
def _indexes(graph: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return (
        {n["id"]: n for n in graph.get("nodes", [])},
        {g["id"]: g for g in graph.get("groups", [])},
    )


def ancestor_groups(graph: dict[str, Any], entity_id: str) -> list[dict[str, Any]]:
    """Return enclosing Pre-Prompt Groups ordered from outermost to innermost."""
    node_by_id, group_by_id = _indexes(graph)
    if entity_id in node_by_id:
        cur = node_by_id[entity_id].get("parent_group_id")
    elif entity_id in group_by_id:
        cur = group_by_id[entity_id].get("parent_group_id")
    else:
        raise PromptGraphError(f"unknown entity {entity_id!r}")

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while cur is not None:
        if cur in seen or cur not in group_by_id:
            break
        seen.add(cur)
        grp = group_by_id[cur]
        chain.append(grp)
        cur = grp.get("parent_group_id")
    chain.reverse()
    return chain


def find_dependents(graph: dict[str, Any], target_id: str) -> list[dict[str, Any]]:
    """Return all prompt nodes and groups that directly depend on `target_id`."""
    node_by_id, group_by_id = _indexes(graph)
    if target_id not in node_by_id and target_id not in group_by_id:
        raise PromptGraphError(f"unknown entity {target_id!r}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in graph.get("edges", []):
        if e.get("is_loop"):
            continue
        if e.get("to") != target_id:
            continue
        fid = str(e.get("from") or "")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        if fid in node_by_id:
            n = node_by_id[fid]
            out.append(
                {
                    "id": fid,
                    "kind": "node",
                    "label": n.get("label") or fid,
                    "existing_temp_prompt": n.get("temp_prompt") or "",
                }
            )
        elif fid in group_by_id:
            g = group_by_id[fid]
            out.append(
                {
                    "id": fid,
                    "kind": "group",
                    "label": g.get("label") or fid,
                    "existing_temp_prompt": g.get("temp_pre_prompt") or "",
                }
            )
    return out


def delete_entity_with_temp_prompts(
    graph: dict[str, Any],
    target_id: str,
    temp_prompts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Destroy `target_id` (node or group) and assign optional one-shot temp prompts to remaining entities."""
    clean = validate_prompt_graph(graph)
    node_by_id, group_by_id = _indexes(clean)
    if target_id not in node_by_id and target_id not in group_by_id:
        raise PromptGraphError(f"unknown entity {target_id!r}")

    if target_id in node_by_id:
        clean["nodes"] = [n for n in clean["nodes"] if n["id"] != target_id]
    else:
        target_grp = group_by_id[target_id]
        fallback_parent = target_grp.get("parent_group_id")
        for n in clean["nodes"]:
            if n.get("parent_group_id") == target_id:
                n["parent_group_id"] = fallback_parent
        for g in clean["groups"]:
            if g.get("parent_group_id") == target_id:
                g["parent_group_id"] = fallback_parent
        clean["groups"] = [g for g in clean["groups"] if g["id"] != target_id]

    clean["edges"] = [
        e for e in clean["edges"] if e.get("from") != target_id and e.get("to") != target_id
    ]

    if temp_prompts is not None:
        if not isinstance(temp_prompts, dict):
            raise PromptGraphError("temp_prompts must be an object mapping entity IDs to text")
        rem_nodes = {n["id"]: n for n in clean["nodes"]}
        rem_groups = {g["id"]: g for g in clean["groups"]}
        for eid, raw_text in temp_prompts.items():
            if eid not in rem_nodes and eid not in rem_groups:
                raise PromptGraphError(f"unknown entity {eid!r} in temp_prompts")
            text = _s(raw_text, MAX_PROMPT_CHARS)
            if eid in rem_nodes:
                rem_nodes[eid]["temp_prompt"] = text
            else:
                rem_groups[eid]["temp_pre_prompt"] = text

    return validate_prompt_graph(clean)


def effective_pre_prompts(graph: dict[str, Any], node_id: str) -> list[dict[str, str]]:
    """Return non-empty pre-prompts (and temp pre-prompts) enclosing `node_id` from outermost to innermost."""
    out: list[dict[str, str]] = []
    for grp in ancestor_groups(graph, node_id):
        pre = str(grp.get("pre_prompt") or "")
        temp_pre = str(grp.get("temp_pre_prompt") or "")
        if pre.strip() or temp_pre.strip():
            out.append(
                {
                    "group_id": grp["id"],
                    "label": grp.get("label") or grp["id"],
                    "pre_prompt": pre,
                    "temp_pre_prompt": temp_pre,
                }
            )
    return out


def expand_nodes(graph: dict[str, Any], entity_id: str) -> list[str]:
    """Return all prompt node IDs contained in `entity_id` (recursively for nested groups)."""
    node_by_id, group_by_id = _indexes(graph)
    if entity_id in node_by_id:
        return [entity_id]
    if entity_id not in group_by_id:
        raise PromptGraphError(f"unknown entity {entity_id!r}")

    collected: list[str] = []
    seen: set[str] = set()

    def walk(gid: str) -> None:
        if gid in seen or gid not in group_by_id:
            return
        seen.add(gid)
        grp = group_by_id[gid]
        for nid in grp.get("node_ids", []):
            if nid in node_by_id and nid not in collected:
                collected.append(nid)
        for cgid in grp.get("child_group_ids", []):
            walk(cgid)

    walk(entity_id)
    return collected


def _expand_groups(graph: dict[str, Any], group_id: str) -> list[str]:
    """Return `group_id` and all descendant nested subgroup IDs."""
    _, group_by_id = _indexes(graph)
    if group_id not in group_by_id:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def walk(gid: str) -> None:
        if gid in seen or gid not in group_by_id:
            return
        seen.add(gid)
        out.append(gid)
        for cgid in group_by_id[gid].get("child_group_ids", []):
            walk(cgid)

    walk(group_id)
    return out


def group_tier(graph: dict[str, Any], group_id: str, _seen: set[str] | None = None) -> int:
    """Return bottom-up nesting tier of a group (1 = leaf 1-그룹, 2 = 2-그룹, 3 = 3-그룹, ..., N = N-그룹)."""
    _, group_by_id = _indexes(graph)
    if group_id not in group_by_id:
        return 1
    seen = _seen if _seen is not None else set()
    if group_id in seen:
        return 1
    seen.add(group_id)
    children = [cid for cid in group_by_id[group_id].get("child_group_ids", []) if cid in group_by_id and cid not in seen]
    tier = 1 if not children else 1 + max(group_tier(graph, cid, seen) for cid in children)
    seen.discard(group_id)
    return tier


def effective_node_dependencies(graph: dict[str, Any]) -> dict[str, set[str]]:
    """Map every prompt node ID -> set of prompt node IDs that must finish before it runs."""
    node_by_id, group_by_id = _indexes(graph)
    deps: dict[str, set[str]] = {nid: set() for nid in node_by_id}

    for nid in node_by_id:
        owners = [nid, *(g["id"] for g in ancestor_groups(graph, nid))]
        for owner_id in owners:
            entity = node_by_id.get(owner_id) or group_by_id.get(owner_id)
            if not entity:
                continue
            for target_id in entity.get("dependencies", []):
                deps[nid].update(expand_nodes(graph, target_id))
    return deps


def dependency_scope(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Compute MCP-accessible dependency scope for `node_id` (including inherited group dependencies)."""
    node_by_id, group_by_id = _indexes(graph)
    if node_id not in node_by_id:
        raise PromptGraphError(f"unknown node {node_id!r}")

    direct_dependencies: list[dict[str, str]] = []
    seen_direct: set[tuple[str, str]] = set()
    queue: list[str] = []

    node_obj = node_by_id[node_id]
    for target_id in node_obj.get("dependencies", []):
        kind = "group" if target_id in group_by_id else "node"
        if (target_id, "self") not in seen_direct:
            seen_direct.add((target_id, "self"))
            direct_dependencies.append({"id": target_id, "kind": kind, "via": "self"})
            queue.append(target_id)

    for grp in ancestor_groups(graph, node_id):
        for target_id in grp.get("dependencies", []):
            kind = "group" if target_id in group_by_id else "node"
            if (target_id, grp["id"]) not in seen_direct:
                seen_direct.add((target_id, grp["id"]))
                direct_dependencies.append({"id": target_id, "kind": kind, "via": grp["id"]})
                queue.append(target_id)

    allowed_node_ids: set[str] = set()
    allowed_group_ids: set[str] = set()
    visited_entities: set[str] = set()

    while queue:
        eid = queue.pop(0)
        if eid in visited_entities:
            continue
        visited_entities.add(eid)

        if eid in node_by_id:
            allowed_node_ids.add(eid)
            for dep_id in node_by_id[eid].get("dependencies", []):
                if dep_id not in visited_entities:
                    queue.append(dep_id)
            for agrp in ancestor_groups(graph, eid):
                for dep_id in agrp.get("dependencies", []):
                    if dep_id not in visited_entities:
                        queue.append(dep_id)
        elif eid in group_by_id:
            allowed_group_ids.add(eid)
            grp = group_by_id[eid]
            for child_nid in grp.get("node_ids", []):
                if child_nid not in visited_entities:
                    queue.append(child_nid)
            for child_gid in grp.get("child_group_ids", []):
                if child_gid not in visited_entities:
                    queue.append(child_gid)
            for dep_id in grp.get("dependencies", []):
                if dep_id not in visited_entities:
                    queue.append(dep_id)

    return {
        "direct_dependencies": direct_dependencies,
        "allowed_node_ids": allowed_node_ids,
        "allowed_group_ids": allowed_group_ids,
    }


def dependent_scope(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Compute entities that directly depend on `node_id` (or its ancestor groups) for MCP impact review."""
    node_by_id, group_by_id = _indexes(graph)
    if node_id not in node_by_id:
        raise PromptGraphError(f"unknown node {node_id!r}")

    source_ids = [node_id, *(g["id"] for g in ancestor_groups(graph, node_id))]
    source_set = set(source_ids)
    direct_dependents: list[dict[str, str]] = []
    seen_direct: set[tuple[str, str]] = set()
    allowed_node_ids: set[str] = set()
    allowed_group_ids: set[str] = set()

    for e in graph.get("edges") or []:
        if e.get("is_loop"):
            continue
        frm = str(e.get("from") or "")
        to = str(e.get("to") or "")
        if to not in source_set or (frm not in node_by_id and frm not in group_by_id):
            continue
        via = "self" if to == node_id else to
        kind = "group" if frm in group_by_id else "node"
        if (frm, via) not in seen_direct:
            seen_direct.add((frm, via))
            direct_dependents.append({"id": frm, "kind": kind, "via": via})
        if frm in node_by_id:
            allowed_node_ids.add(frm)
        else:
            allowed_group_ids.update(_expand_groups(graph, frm))
            allowed_node_ids.update(expand_nodes(graph, frm))

    return {
        "direct_dependents": direct_dependents,
        "allowed_node_ids": allowed_node_ids,
        "allowed_group_ids": allowed_group_ids,
    }


def compute_change_status(graph: dict[str, Any]) -> dict[str, Any]:
    """Compute Git-style change status ('clean' | 'modified' | 'needs_rerun' | 'unexecuted') for all nodes and groups."""
    node_by_id, group_by_id = _indexes(graph)

    state: dict[str, str] = {}
    reasons: dict[str, list[str]] = {}

    for nid, n in node_by_id.items():
        if not n.get("completed", False):
            state[nid] = "unexecuted"
            reasons[nid] = ["unexecuted"]
            continue

        direct_reasons: list[str] = []
        if n.get("last_run_prompt") is not None and (n.get("prompt") or "") != n["last_run_prompt"]:
            direct_reasons.append("prompt_changed")
        last_temp = "" if n.get("last_run_temp_prompt") is None else n["last_run_temp_prompt"]
        if (n.get("temp_prompt") or "") != last_temp:
            direct_reasons.append("temp_prompt_changed")

        cur_pre = {
            g["id"]: {"pre": g.get("pre_prompt") or "", "temp": g.get("temp_pre_prompt") or ""}
            for g in ancestor_groups(graph, nid)
        }
        prev_pre = n.get("last_run_pre_prompts")
        pre_changed_gids: list[str] = []
        if isinstance(prev_pre, dict):
            for gid, vals in cur_pre.items():
                if prev_pre.get(gid) != vals:
                    pre_changed_gids.append(gid)
            for gid, vals in prev_pre.items():
                if gid not in cur_pre and isinstance(vals, dict) and (vals.get("pre") or vals.get("temp")):
                    pre_changed_gids.append(gid)

        pre_reasons = [f"pre_prompt_changed:{gid}" for gid in pre_changed_gids]
        marked_rerun = bool(n.get("needs_rerun", False))
        mark_reasons = ["agent_marked_rerun"] if marked_rerun else []
        if direct_reasons:
            state[nid] = "modified"
            reasons[nid] = direct_reasons + pre_reasons + mark_reasons
        elif marked_rerun or pre_changed_gids:
            state[nid] = "needs_rerun"
            reasons[nid] = mark_reasons + pre_reasons
        else:
            state[nid] = "clean"
            reasons[nid] = []

    for gid, g in group_by_id.items():
        if not g.get("completed", False):
            state[gid] = "unexecuted"
            reasons[gid] = ["unexecuted"]
            continue

        direct_reasons = []
        if g.get("last_run_pre_prompt") is not None and (g.get("pre_prompt") or "") != g["last_run_pre_prompt"]:
            direct_reasons.append("pre_prompt_changed")
        last_tpre = "" if g.get("last_run_temp_pre_prompt") is None else g["last_run_temp_pre_prompt"]
        if (g.get("temp_pre_prompt") or "") != last_tpre:
            direct_reasons.append("temp_pre_prompt_changed")

        marked_rerun = bool(g.get("needs_rerun", False))
        mark_reasons = ["agent_marked_rerun"] if marked_rerun else []
        if direct_reasons:
            state[gid] = "modified"
            reasons[gid] = direct_reasons + mark_reasons
        elif marked_rerun:
            state[gid] = "needs_rerun"
            reasons[gid] = mark_reasons
        else:
            state[gid] = "clean"
            reasons[gid] = []

    # Propagate dirty/needs_rerun upwards through enclosing groups (containment only, NOT edges)
    groups = graph.get("groups") or []
    changed = True
    while changed:
        changed = False
        for g in groups:
            gid = g["id"]
            if gid not in state:
                continue
            children = list(g.get("node_ids") or []) + list(g.get("child_group_ids") or [])
            for cid in children:
                if cid in state and state[cid] != "clean":
                    if state[gid] == "clean":
                        state[gid] = "needs_rerun"
                        changed = True
                    if state[gid] == "needs_rerun":
                        tag = f"member_changed:{cid}"
                        if tag not in reasons[gid]:
                            reasons[gid].append(tag)

    return {
        "nodes": {
            nid: {
                "state": state[nid],
                "needs_rerun": state[nid] != "clean",
                "reasons": reasons[nid],
                "rerun_reason": node_by_id[nid].get("rerun_reason") or "",
            }
            for nid in node_by_id
        },
        "groups": {
            gid: {
                "state": state[gid],
                "needs_rerun": state[gid] != "clean",
                "reasons": reasons[gid],
                "rerun_reason": group_by_id[gid].get("rerun_reason") or "",
            }
            for gid in group_by_id
        },
    }


def plan_execution(
    graph: dict[str, Any],
    target_type: str = "all",
    target_id: str | None = None,
    reuse_existing_outputs: bool = False,
    target_ids: list[str] | None = None,
    force_rerun: bool = False,
) -> dict[str, Any]:
    """Compute target nodes, transitive dependencies, and the exact set of nodes to execute."""
    clean = validate_prompt_graph(graph)
    node_by_id, group_by_id = _indexes(clean)

    if target_type not in ("all", "node", "group", "subset"):
        raise PromptGraphError("target_type must be 'all', 'node', 'group', or 'subset'")

    normalized_target_ids: list[str] = []
    target_groups: list[str] = []
    if target_type == "all":
        target_nodes = [n["id"] for n in clean["nodes"]]
        target_groups = [g["id"] for g in clean["groups"]]
    elif target_type == "node":
        if not target_id or target_id not in node_by_id:
            raise PromptGraphError(f"unknown target node {target_id!r}")
        target_nodes = [target_id]
        normalized_target_ids = [target_id]
    elif target_type == "group":
        if not target_id or target_id not in group_by_id:
            raise PromptGraphError(f"unknown target group {target_id!r}")
        target_nodes = expand_nodes(clean, target_id)
        normalized_target_ids = [target_id]
        target_groups = _expand_groups(clean, target_id)
    else:
        if not isinstance(target_ids, list) or not target_ids:
            raise PromptGraphError("target_ids must be a non-empty list when target_type is 'subset'")
        seen_tids: set[str] = set()
        seen_tnodes: set[str] = set()
        seen_tgroups: set[str] = set()
        target_nodes = []
        for raw_tid in target_ids:
            tid = str(raw_tid or "").strip()
            if tid not in node_by_id and tid not in group_by_id:
                raise PromptGraphError(f"unknown target entity {tid!r} in target_ids")
            if tid in seen_tids:
                continue
            seen_tids.add(tid)
            normalized_target_ids.append(tid)
            if tid in group_by_id:
                for gid in _expand_groups(clean, tid):
                    if gid not in seen_tgroups:
                        seen_tgroups.add(gid)
                        target_groups.append(gid)
            for nid in expand_nodes(clean, tid):
                if nid not in seen_tnodes:
                    seen_tnodes.add(nid)
                    target_nodes.append(nid)

    eff_deps = effective_node_dependencies(clean)

    # Walk dependencies backwards from target_nodes
    required_set: set[str] = set()
    stack = list(target_nodes)
    while stack:
        nid = stack.pop()
        if nid in required_set:
            continue
        required_set.add(nid)
        for dep_nid in eff_deps.get(nid, set()):
            if dep_nid not in required_set:
                stack.append(dep_nid)

    def _has_pending_temp(nid: str) -> bool:
        if str(node_by_id[nid].get("temp_prompt") or "").strip():
            return True
        return any(str(g.get("temp_pre_prompt") or "").strip() for g in ancestor_groups(clean, nid))

    change_status = compute_change_status(clean)
    target_set = set(target_nodes)
    run_set: set[str] = set()
    skipped_clean_nodes: set[str] = set()
    for nid in required_set:
        if not force_rerun and change_status["nodes"][nid]["state"] == "clean":
            skipped_clean_nodes.add(nid)
            continue
        if reuse_existing_outputs and nid not in target_set and not _has_pending_temp(nid):
            existing_output = str(node_by_id[nid].get("output") or "").strip()
            if existing_output:
                continue
        run_set.add(nid)

    # Topological sort over run_set for deterministic ordering
    ordered_nodes: list[str] = []
    in_deg = {nid: len(eff_deps[nid] & run_set) for nid in run_set}
    ready = sorted(nid for nid, deg in in_deg.items() if deg == 0)
    while ready:
        cur = ready.pop(0)
        ordered_nodes.append(cur)
        for nid in sorted(run_set):
            if cur in eff_deps[nid]:
                in_deg[nid] -= 1
                if in_deg[nid] == 0:
                    ready.append(nid)
                    ready.sort()

    return {
        "graph": clean,
        "target_type": target_type,
        "target_id": target_id,
        "target_ids": normalized_target_ids,
        "target_groups": target_groups,
        "target_nodes": target_nodes,
        "required_nodes": sorted(required_set),
        "run_nodes": ordered_nodes,
        "skipped_clean_nodes": sorted(skipped_clean_nodes),
        "change_status": change_status,
        "effective_deps": {nid: sorted(eff_deps[nid] & run_set) for nid in ordered_nodes},
    }


# ------------------------------------------------------------------ Contracts, Conditions, Hash Cache, Profiling & Templates
def _extract_json_payload(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty")
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            i = raw.find(opener)
            j = raw.rfind(closer)
            if i != -1 and j > i:
                return json.loads(raw[i : j + 1])
        raise


def evaluate_output_contract(output: str, contract: dict[str, Any] | None) -> dict[str, Any]:
    """Validate node output against schema / assertion contract rules."""
    cfg = _normalize_contract(contract)
    if not cfg["enabled"]:
        return {"passed": True, "errors": [], "parsed_json": None}

    text = str(output or "")
    errors: list[str] = []
    parsed_json: Any = None

    if cfg["min_chars"] > 0 and len(text.strip()) < cfg["min_chars"]:
        errors.append(f"최소 길이 미달 ({len(text.strip())} < {cfg['min_chars']}자)")

    if cfg["json_required"] or cfg["json_keys"]:
        try:
            parsed_json = _extract_json_payload(text)
        except Exception:
            errors.append("유효한 JSON 형식이 아닙니다.")
        else:
            if cfg["json_keys"]:
                if not isinstance(parsed_json, dict):
                    errors.append("JSON 루트가 객체(object)여야 필수 키를 검증할 수 있습니다.")
                else:
                    missing = [k for k in cfg["json_keys"] if k not in parsed_json]
                    if missing:
                        errors.append(f"필수 JSON 키 누락: {', '.join(missing)}")

    for needle in cfg["must_include"]:
        if needle not in text:
            errors.append(f"필수 포함 문자열 누락: {needle!r}")

    for banned in cfg["must_exclude"]:
        if banned in text:
            errors.append(f"금지 문자열 포함됨: {banned!r}")

    if cfg["regex"]:
        try:
            if not re.search(cfg["regex"], text, re.MULTILINE):
                errors.append(f"정규식 패턴 불일치: /{cfg['regex']}/")
        except re.error as exc:
            errors.append(f"잘못된 정규식: {exc}")

    return {"passed": len(errors) == 0, "errors": errors, "parsed_json": parsed_json}


def _json_lookup(obj: Any, dotted_key: str) -> Any:
    cur = obj
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def evaluate_edge_condition(
    edge: dict[str, Any],
    upstream_output: str,
    contract_result: dict[str, Any] | None = None,
    agent_decisions: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Evaluate a conditional forward or loop edge after upstream node execution."""
    cond = _normalize_condition(edge.get("condition"))
    ctype = cond["type"]
    val = cond["value"]
    out = str(upstream_output or "")

    if ctype == "always":
        return True
    if ctype == "on_contract_pass":
        return bool((contract_result or {}).get("passed", True))
    if ctype == "on_contract_fail":
        return not bool((contract_result or {}).get("passed", True))
    if ctype == "contains":
        if not val:
            return True
        return (val in out) if cond["case_sensitive"] else (val.lower() in out.lower())
    if ctype == "not_contains":
        if not val:
            return True
        return (val not in out) if cond["case_sensitive"] else (val.lower() not in out.lower())
    if ctype == "regex":
        if not val:
            return True
        flags = re.MULTILINE | (0 if cond["case_sensitive"] else re.IGNORECASE)
        try:
            return bool(re.search(val, out, flags))
        except re.error:
            return False
    if ctype == "json_eq":
        if "=" not in val:
            return False
        k, expected = val.split("=", 1)
        try:
            parsed = (contract_result or {}).get("parsed_json")
            if parsed is None:
                parsed = _extract_json_payload(out)
        except Exception:
            return False
        actual = _json_lookup(parsed, k.strip())
        if actual is None:
            return False
        actual_s = json.dumps(actual, ensure_ascii=False) if isinstance(actual, (dict, list)) else str(actual)
        if isinstance(actual, bool):
            actual_s = "true" if actual else "false"
        exp_s = expected.strip()
        return (actual_s == exp_s) if cond["case_sensitive"] else (actual_s.lower() == exp_s.lower())
    if ctype == "agent_decide":
        decs = agent_decisions or {}
        rec = decs.get(str(edge.get("id") or "")) or decs.get("*")
        if isinstance(rec, dict):
            return bool(rec.get("continue_loop", False))
        return False
    return True


def compute_node_input_hash(graph: dict[str, Any], node_id: str, artifact_hash: str = "") -> str:
    """Compute a deterministic SHA-256 hash of all inputs that affect node execution."""
    node_by_id, _ = _indexes(graph)
    if node_id not in node_by_id:
        raise PromptGraphError(f"unknown node {node_id!r}")
    node = node_by_id[node_id]
    eff = effective_node_dependencies(graph).get(node_id, set())
    dep_outputs = {
        did: str((node_by_id.get(did) or {}).get("output") or "")
        for did in sorted(eff)
    }
    payload = {
        "prompt": node.get("prompt") or "",
        "temp_prompt": node.get("temp_prompt") or "",
        "pre_prompts": [
            {"id": p["group_id"], "pre": p.get("pre_prompt") or "", "temp": p.get("temp_pre_prompt") or ""}
            for p in effective_pre_prompts(graph, node_id)
        ],
        "model": node.get("model") or (graph.get("settings") or {}).get("default_model") or "",
        "extra": node.get("extra") or {},
        "contract": node.get("contract") or {},
        "artifact_hash": artifact_hash or (node.get("artifact") or {}).get("last_content_hash") or "",
        "dep_outputs": dep_outputs,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost by model alias/name."""
    m = str(model or "").lower()
    if any(k in m for k in ("cheap", "flash", "lite", "mini", "haiku")):
        in_rate, out_rate = 0.0002, 0.0008
    elif any(k in m for k in ("advisor", "pro", "opus", "o1", "o3", "5.4", "sonnet-4")):
        in_rate, out_rate = 0.0030, 0.0150
    else:
        in_rate, out_rate = 0.0010, 0.0040
    return round((max(0, prompt_tokens) * in_rate + max(0, completion_tokens) * out_rate) / 1000.0, 6)


def compute_run_profile(
    graph: dict[str, Any],
    node_state: dict[str, dict[str, Any]],
    saved_estimates: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute cost, tokens, critical path, and incremental cache/clean savings for a run."""
    node_by_id, _ = _indexes(graph)
    eff_deps = effective_node_dependencies(graph)
    total_prompt = 0
    total_comp = 0
    total_tokens = 0
    total_cost = 0.0
    sum_ms = 0
    reused_clean = 0
    cache_hits = 0
    saved_tokens = 0
    saved_cost = 0.0
    saved_ms = 0

    for nid, st in (node_state or {}).items():
        usage = st.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        cost = float(st.get("cost_usd") if st.get("cost_usd") is not None else estimate_cost_usd(st.get("model") or "", pt, ct))
        ms = int(st.get("ms") or 0)
        if st.get("cache_hit"):
            cache_hits += 1
            est = (saved_estimates or {}).get(nid) or {}
            saved_tokens += int(est.get("total_tokens") or tt or 600)
            saved_cost += float(est.get("cost_usd") or estimate_cost_usd(st.get("model") or "", 400, 200))
            saved_ms += int(est.get("ms") or 1200)
        elif st.get("status") == "reused":
            reused_clean += 1
            est = (saved_estimates or {}).get(nid) or {}
            est_tok = int(est.get("total_tokens") or max(150, len((node_by_id.get(nid) or {}).get("output") or "") // 3))
            saved_tokens += est_tok
            saved_cost += float(est.get("cost_usd") or estimate_cost_usd(st.get("model") or "", int(est_tok * 0.65), int(est_tok * 0.35)))
            saved_ms += int(est.get("ms") or 1200)
        else:
            total_prompt += pt
            total_comp += ct
            total_tokens += tt
            total_cost += cost
            sum_ms += ms

    # DAG critical path across nodes participating in execution timing (sum of max dependency path ms)
    active_ids = [
        nid for nid, st in (node_state or {}).items()
        if nid in node_by_id and st.get("status") in ("done", "failed")
    ]
    active_set = set(active_ids)
    dist: dict[str, int] = {}
    prev: dict[str, str | None] = {}

    def _longest(u: str) -> int:
        if u in dist:
            return dist[u]
        own = int((node_state.get(u) or {}).get("ms") or 0)
        best_dep = 0
        best_p: str | None = None
        for d in sorted(eff_deps.get(u, set()) & active_set):
            cand = _longest(d)
            if cand >= best_dep:
                best_dep = cand
                best_p = d
        dist[u] = own + best_dep
        prev[u] = best_p
        return dist[u]

    crit_end: str | None = None
    crit_ms = 0
    for nid in active_ids:
        val = _longest(nid)
        if val >= crit_ms:
            crit_ms = val
            crit_end = nid

    crit_nodes: list[str] = []
    cur = crit_end
    while cur is not None:
        crit_nodes.append(cur)
        cur = prev.get(cur)
    crit_nodes.reverse()

    return {
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_comp,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "total_node_ms": sum_ms,
        "critical_path_ms": crit_ms,
        "critical_path_nodes": crit_nodes,
        "parallelism_speedup": round(sum_ms / crit_ms, 2) if crit_ms > 0 else 1.0,
        "reused_clean_count": reused_clean,
        "cache_hit_count": cache_hits,
        "estimated_saved_tokens": saved_tokens,
        "estimated_saved_cost_usd": round(saved_cost, 6),
        "estimated_saved_ms": saved_ms,
    }


def extract_group_template(graph: dict[str, Any], group_id: str) -> dict[str, Any]:
    """Extract a group and its full nested hierarchy + internal edges as a reusable template."""
    clean = validate_prompt_graph(graph)
    node_by_id, group_by_id = _indexes(clean)
    if group_id not in group_by_id:
        raise PromptGraphError(f"unknown group {group_id!r}")

    gids = _expand_groups(clean, group_id)
    gid_set = set(gids)
    nids = expand_nodes(clean, group_id)
    nid_set = set(nids)
    all_set = gid_set | nid_set

    groups_copy = [deepcopy(group_by_id[gid]) for gid in gids]
    nodes_copy = [deepcopy(node_by_id[nid]) for nid in nids]
    root_grp = next(g for g in groups_copy if g["id"] == group_id)
    ox, oy = int(root_grp.get("x") or 0), int(root_grp.get("y") or 0)
    root_grp["parent_group_id"] = None
    root_grp["dependencies"] = [d for d in root_grp.get("dependencies", []) if d in all_set]

    for g in groups_copy:
        g["x"] = int(g.get("x") or 0) - ox
        g["y"] = int(g.get("y") or 0) - oy
        g["completed"] = False
        g["needs_rerun"] = False
        g["rerun_reason"] = ""
        g["temp_pre_prompt"] = ""
        g["last_run_pre_prompt"] = None
        g["last_run_temp_pre_prompt"] = None
        g["dependencies"] = [d for d in g.get("dependencies", []) if d in all_set]

    for n in nodes_copy:
        n["x"] = int(n.get("x") or 0) - ox
        n["y"] = int(n.get("y") or 0) - oy
        n["completed"] = False
        n["needs_rerun"] = False
        n["rerun_reason"] = ""
        n["output"] = ""
        n["temp_prompt"] = ""
        n["last_run_prompt"] = None
        n["last_run_temp_prompt"] = None
        n["last_run_pre_prompts"] = None
        n["impact_history"] = []
        n["dependencies"] = [d for d in n.get("dependencies", []) if d in all_set]

    edges_copy = [
        deepcopy(e) for e in clean.get("edges", [])
        if e["from"] in all_set and e["to"] in all_set
    ]
    return {
        "subtree_json": {
            "root_group_id": group_id,
            "nodes": nodes_copy,
            "groups": groups_copy,
            "edges": edges_copy,
        },
        "tier_depth": group_tier(clean, group_id),
        "node_count": len(nodes_copy),
        "group_count": len(groups_copy),
    }


def instantiate_group_template(
    graph: dict[str, Any],
    subtree_json: dict[str, Any],
    offset_x: int = 80,
    offset_y: int = 80,
) -> dict[str, Any]:
    """Instantiate a saved N-group template into `graph` with fresh unique entity/edge IDs."""
    base = validate_prompt_graph(deepcopy(graph or {"nodes": [], "groups": [], "edges": [], "settings": {}}))
    used_ids = {n["id"] for n in base["nodes"]} | {g["id"] for g in base["groups"]}
    used_edge_ids = {e["id"] for e in base["edges"]}

    def _fresh(prefix: str, existing: set[str]) -> str:
        idx = 1
        while f"{prefix}_{idx}" in existing:
            idx += 1
        cand = f"{prefix}_{idx}"
        existing.add(cand)
        return cand

    id_map: dict[str, str] = {}
    tpl_groups = deepcopy(subtree_json.get("groups") or [])
    tpl_nodes = deepcopy(subtree_json.get("nodes") or [])
    tpl_edges = deepcopy(subtree_json.get("edges") or [])
    if not tpl_groups:
        raise PromptGraphError("template contains no groups")

    for g in tpl_groups:
        new_gid = _fresh("grp", used_ids)
        id_map[g["id"]] = new_gid
    for n in tpl_nodes:
        new_nid = _fresh("node", used_ids)
        id_map[n["id"]] = new_nid

    root_old_id = str(subtree_json.get("root_group_id") or tpl_groups[0]["id"])
    inserted_root_id = id_map.get(root_old_id, tpl_groups[0]["id"])

    for g in tpl_groups:
        old_id = g["id"]
        g["id"] = id_map[old_id]
        g["parent_group_id"] = id_map.get(g.get("parent_group_id")) if g.get("parent_group_id") in id_map else None
        g["node_ids"] = [id_map[x] for x in g.get("node_ids", []) if x in id_map]
        g["child_group_ids"] = [id_map[x] for x in g.get("child_group_ids", []) if x in id_map]
        g["dependencies"] = [id_map[x] for x in g.get("dependencies", []) if x in id_map]
        g["x"] = int(g.get("x") or 0) + int(offset_x)
        g["y"] = int(g.get("y") or 0) + int(offset_y)
        base["groups"].append(g)

    for n in tpl_nodes:
        n["id"] = id_map[n["id"]]
        n["parent_group_id"] = id_map.get(n.get("parent_group_id")) if n.get("parent_group_id") in id_map else None
        n["dependencies"] = [id_map[x] for x in n.get("dependencies", []) if x in id_map]
        n["x"] = int(n.get("x") or 0) + int(offset_x)
        n["y"] = int(n.get("y") or 0) + int(offset_y)
        base["nodes"].append(n)

    for e in tpl_edges:
        if e.get("from") in id_map and e.get("to") in id_map:
            new_e = deepcopy(e)
            new_e["id"] = _fresh("edge", used_edge_ids)
            new_e["from"] = id_map[e["from"]]
            new_e["to"] = id_map[e["to"]]
            base["edges"].append(new_e)

    clean = validate_prompt_graph(base)
    return {"graph": clean, "inserted_group_id": inserted_root_id, "id_map": id_map}


# ------------------------------------------------------------------ Zero-overlap Bottom-Up Hierarchical Auto-Layout
LAYOUT_TITLE_GAP = 24


def detect_layout_overlaps(graph: dict[str, Any]) -> dict[str, Any]:
    """Detect bounding-box overlaps (with floating-title clearance), containment violations, and top clips."""
    nodes = list(graph.get("nodes") or [])
    groups = list(graph.get("groups") or [])
    by_id: dict[str, dict[str, Any]] = {}
    kind_by_id: dict[str, str] = {}
    for n in nodes:
        by_id[n["id"]] = n
        kind_by_id[n["id"]] = "node"
    for g in groups:
        by_id[g["id"]] = g
        kind_by_id[g["id"]] = "group"

    def _box(eid: str) -> tuple[int, int, int, int]:
        ent = by_id[eid]
        w_def = 250 if kind_by_id[eid] == "node" else 360
        h_def = 140 if kind_by_id[eid] == "node" else 240
        x = int(ent.get("x") or 0)
        y = int(ent.get("y") or 0)
        w = int(ent.get("width") or w_def)
        h = int(ent.get("height") or h_def)
        return x, y, w, h

    def _ancestors(eid: str) -> set[str]:
        anc: set[str] = set()
        cur = by_id[eid].get("parent_group_id")
        while cur and cur in by_id and cur not in anc:
            anc.add(cur)
            cur = by_id[cur].get("parent_group_id")
        return anc

    anc_map = {eid: _ancestors(eid) for eid in by_id}
    overlap_pairs: list[tuple[str, str]] = []
    containment_violations: list[tuple[str, str]] = []
    top_clips: list[str] = []

    all_ids = sorted(by_id.keys())
    for eid in all_ids:
        x, y, w, h = _box(eid)
        if y < 32:
            top_clips.append(eid)
        pgid = by_id[eid].get("parent_group_id")
        if pgid and pgid in by_id:
            px, py, pw, ph = _box(pgid)
            if x < px + 12 or y < py + 30 or (x + w) > (px + pw - 12) or (y + h) > (py + ph - 12):
                containment_violations.append((pgid, eid))

    for i, a_id in enumerate(all_ids):
        ax, ay, aw, ah = _box(a_id)
        for b_id in all_ids[i + 1 :]:
            if a_id in anc_map[b_id] or b_id in anc_map[a_id]:
                continue
            bx, by, bw, bh = _box(b_id)
            # Include floating title clearance above each entity so titles never collide
            a_top = ay - LAYOUT_TITLE_GAP
            b_top = by - LAYOUT_TITLE_GAP
            if ax < bx + bw and ax + aw > bx and a_top < by + bh and ay + ah > b_top:
                overlap_pairs.append((a_id, b_id))

    return {
        "ok": not (overlap_pairs or containment_violations or top_clips),
        "overlap_pairs": overlap_pairs,
        "containment_violations": containment_violations,
        "top_clips": top_clips,
    }


def auto_layout_graph(
    graph: dict[str, Any],
    *,
    direction: str = "auto",
    preserve_sizes: bool = True,
) -> dict[str, Any]:
    """Deterministically arrange nodes and arbitrarily nested N-groups with zero overlaps."""
    clean = validate_prompt_graph(deepcopy(graph or {"nodes": [], "groups": [], "edges": [], "settings": {}}))
    node_by_id, group_by_id = _indexes(clean)
    if not node_by_id and not group_by_id:
        return clean

    PAD_X = 40
    PAD_TOP = 56
    PAD_BOT = 36
    GAP_X = 90
    GAP_Y = 56
    MARGIN_X = 60
    MARGIN_Y = 64
    EMPTY_GRP_W = 280
    EMPTY_GRP_H = 170

    def _snap(v: float) -> int:
        return int(round(v / 10.0) * 10)

    def _translate_subtree(eid: str, dx: int, dy: int) -> None:
        if not dx and not dy:
            return
        if eid in node_by_id:
            node_by_id[eid]["x"] = int(node_by_id[eid]["x"]) + dx
            node_by_id[eid]["y"] = int(node_by_id[eid]["y"]) + dy
            return
        grp = group_by_id[eid]
        grp["x"] = int(grp["x"]) + dx
        grp["y"] = int(grp["y"]) + dy
        for cid in grp.get("node_ids", []):
            if cid in node_by_id:
                _translate_subtree(cid, dx, dy)
        for cgid in grp.get("child_group_ids", []):
            if cgid in group_by_id:
                _translate_subtree(cgid, dx, dy)

    dir_norm = str(direction or "auto").strip().lower()
    if dir_norm == "auto":
        dir_mode = "execution" if (clean.get("settings") or {}).get("edge_direction") == "execution" else "dependency"
    elif dir_norm in ("execution", "left_to_right"):
        dir_mode = "execution"
    else:
        dir_mode = "dependency"

    def _layout_container(parent_gid: str | None) -> tuple[int, int]:
        if parent_gid is None:
            child_nids = [n["id"] for n in clean["nodes"] if not n.get("parent_group_id")]
            child_gids = [g["id"] for g in clean["groups"] if not g.get("parent_group_id")]
        else:
            pgrp = group_by_id[parent_gid]
            child_nids = list(pgrp.get("node_ids") or [])
            child_gids = list(pgrp.get("child_group_ids") or [])

        siblings = [*child_gids, *child_nids]
        if not siblings:
            if parent_gid is not None:
                p = group_by_id[parent_gid]
                w = max(EMPTY_GRP_W, _snap(p.get("width") or EMPTY_GRP_W)) if preserve_sizes else EMPTY_GRP_W
                h = max(EMPTY_GRP_H, _snap(p.get("height") or EMPTY_GRP_H)) if preserve_sizes else EMPTY_GRP_H
                p["x"], p["y"], p["width"], p["height"] = 0, 0, w, h
                return w, h
            return 0, 0

        sib_sizes: dict[str, tuple[int, int]] = {}
        for cgid in child_gids:
            sib_sizes[cgid] = _layout_container(cgid)
        for nid in child_nids:
            n = node_by_id[nid]
            nw = max(220, _snap(n.get("width") or 250)) if preserve_sizes else 250
            nh = max(120, _snap(n.get("height") or 140)) if preserve_sizes else 140
            n["width"], n["height"] = nw, nh
            sib_sizes[nid] = (nw, nh)

        # Map every descendant inside this container to its direct sibling representative
        member_to_sib: dict[str, str] = {}
        for s in siblings:
            member_to_sib[s] = s
            if s in group_by_id:
                for nid in expand_nodes(clean, s):
                    member_to_sib[nid] = s
                for sub_gid in _expand_groups(clean, s):
                    member_to_sib[sub_gid] = s

        # Build prerequisite -> dependent DAG edges lifted onto direct siblings
        prereq_to_deps: dict[str, set[str]] = {s: set() for s in siblings}
        dep_count: dict[str, int] = {s: 0 for s in siblings}
        for e in clean.get("edges") or []:
            if e.get("is_loop"):
                continue
            su = member_to_sib.get(e["from"])
            sv = member_to_sib.get(e["to"])
            if su and sv and su != sv and su not in prereq_to_deps[sv]:
                prereq_to_deps[sv].add(su)
                dep_count[su] += 1

        # Topological ranking with deterministic cycle-breaking fallback
        rank: dict[str, int] = {s: 0 for s in siblings}
        remaining = set(siblings)
        indeg = dict(dep_count)
        while remaining:
            ready = sorted(s for s in remaining if indeg[s] == 0)
            if not ready:
                # Pick node with minimum current in-degree to break any residual cycle safely
                pick = min(sorted(remaining), key=lambda s: (indeg[s], s))
                ready = [pick]
            for u in ready:
                remaining.remove(u)
                for v in prereq_to_deps[u]:
                    if v in remaining:
                        rank[v] = max(rank[v], rank[u] + 1)
                        indeg[v] = max(0, indeg[v] - 1)

        max_rank = max(rank.values()) if rank else 0
        cols: dict[int, list[str]] = {}
        for s in siblings:
            c_idx = rank[s] if dir_mode == "execution" else (max_rank - rank[s])
            cols.setdefault(c_idx, []).append(s)
        for c_idx in cols:
            cols[c_idx].sort(key=lambda s: (0 if s in group_by_id else 1, rank[s], s))

        ordered_cols = [cols[k] for k in sorted(cols.keys())]
        col_widths = [max(sib_sizes[s][0] for s in col) for col in ordered_cols]
        col_heights = [
            sum(sib_sizes[s][1] for s in col) + GAP_Y * max(0, len(col) - 1)
            for col in ordered_cols
        ]
        max_col_h = max(col_heights) if col_heights else 0

        pad_x = 0 if parent_gid is None else PAD_X
        pad_top = 0 if parent_gid is None else PAD_TOP
        pad_bot = 0 if parent_gid is None else PAD_BOT

        cur_x = pad_x
        for col, col_w, col_h in zip(ordered_cols, col_widths, col_heights):
            y_off = _snap((max_col_h - col_h) / 2.0)
            cur_y = pad_top + y_off
            for s in col:
                sw, sh = sib_sizes[s]
                tx = cur_x + _snap((col_w - sw) / 2.0)
                ty = cur_y
                if s in node_by_id:
                    node_by_id[s]["x"] = tx
                    node_by_id[s]["y"] = ty
                else:
                    grp = group_by_id[s]
                    _translate_subtree(s, tx - int(grp["x"]), ty - int(grp["y"]))
                cur_y += sh + GAP_Y
            cur_x += col_w + GAP_X

        total_w = cur_x - GAP_X + pad_x
        total_h = pad_top + max_col_h + pad_bot
        if parent_gid is not None:
            pgrp = group_by_id[parent_gid]
            pgrp["x"], pgrp["y"], pgrp["width"], pgrp["height"] = 0, 0, total_w, total_h
        return total_w, total_h

    _layout_container(None)
    # Shift root entities to clean canvas margins (>= 60, >= 64)
    all_boxes = [*clean["nodes"], *clean["groups"]]
    if all_boxes:
        min_x = min(int(b["x"]) for b in all_boxes)
        min_y = min(int(b["y"]) for b in all_boxes)
        dx = MARGIN_X - min_x
        dy = MARGIN_Y - min_y
        for b in all_boxes:
            b["x"] = int(b["x"]) + dx
            b["y"] = int(b["y"]) + dy

    return validate_prompt_graph(clean)


# ------------------------------------------------------------------ Real MCP Server
def _serialize_node_info(
    graph: dict[str, Any],
    node_id: str,
    node_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node_by_id, _ = _indexes(graph)
    node = node_by_id[node_id]
    return {
        "id": node["id"],
        "label": node.get("label") or node["id"],
        "prompt": node.get("prompt") or "",
        "temp_prompt": node.get("temp_prompt") or "",
        "output": node.get("output") or "",
        "model": node.get("model") or graph.get("settings", {}).get("default_model") or "",
        "dependencies": list(node.get("dependencies") or []),
        "extra": deepcopy(node.get("extra") or {}),
        "completed": bool(node.get("completed", False)),
        "needs_rerun": bool(node.get("needs_rerun", False)),
        "rerun_reason": node.get("rerun_reason") or "",
        "parent_group_id": node.get("parent_group_id"),
        "pre_prompts": effective_pre_prompts(graph, node["id"]),
        "status": (node_status or {}).get("status", "idle"),
    }


def _serialize_group_info(
    graph: dict[str, Any],
    group_id: str,
    recursive: bool = True,
    node_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    node_by_id, group_by_id = _indexes(graph)
    grp = group_by_id[group_id]
    direct_nodes = [
        _serialize_node_info(graph, nid, (node_states or {}).get(nid))
        for nid in grp.get("node_ids", [])
        if nid in node_by_id
    ]
    child_groups: list[dict[str, Any]] = []
    if recursive:
        for cgid in grp.get("child_group_ids", []):
            if cgid in group_by_id:
                child_groups.append(_serialize_group_info(graph, cgid, recursive=True, node_states=node_states))
    return {
        "id": grp["id"],
        "label": grp.get("label") or grp["id"],
        "pre_prompt": grp.get("pre_prompt") or "",
        "temp_pre_prompt": grp.get("temp_pre_prompt") or "",
        "completed": bool(grp.get("completed", False)),
        "needs_rerun": bool(grp.get("needs_rerun", False)),
        "rerun_reason": grp.get("rerun_reason") or "",
        "parent_group_id": grp.get("parent_group_id"),
        "tier": group_tier(graph, group_id),
        "dependencies": list(grp.get("dependencies") or []),
        "node_ids": list(grp.get("node_ids") or []),
        "child_group_ids": list(grp.get("child_group_ids") or []),
        "nodes": direct_nodes,
        "child_groups": child_groups,
    }


MCP_TOOLS = [
    (
        "list_dependencies",
        "List the dependency nodes and pre-prompt groups accessible to the current prompt node.",
        {},
        [],
    ),
    (
        "get_node_info",
        "Read detailed info (prompt, output, model, dependencies, extra, pre_prompts) of one or more dependency prompt nodes.",
        {
            "node_id": {"type": "string", "description": "Single dependency prompt node ID"},
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of dependency prompt node IDs",
            },
        },
        [],
    ),
    (
        "get_group_info",
        "Read full info of a dependency pre-prompt group (pre_prompt, direct nodes, and recursively nested child_groups and their nodes).",
        {
            "group_id": {"type": "string", "description": "Pre-prompt group ID"},
            "recursive": {"type": "boolean", "description": "Include nested child groups recursively (default true)"},
        },
        ["group_id"],
    ),
]


def make_prompt_graph_mcp_server(
    graph_accessor: Callable[[], dict[str, Any]],
    current_node_id: str,
    scope: dict[str, Any],
    state_accessor: Callable[[], dict[str, dict[str, Any]]] | None = None,
    *,
    has_agent_loop: bool = False,
    loop_decision_cb: Callable[[str, bool, str], None] | None = None,
) -> Server:
    """Create an isolated real MCP server scoped strictly to `current_node_id`'s dependency graph."""
    server = Server("irade-prompt-graph-mcp")

    @server.list_tools()
    async def list_tools():
        defs = list(MCP_TOOLS)
        if has_agent_loop:
            defs.append(
                (
                    "signal_loop_decision",
                    "Signal whether a conditional agent_decide loop edge from this node should continue iterating.",
                    {
                        "continue_loop": {"type": "boolean", "description": "True to loop back again, False to finish and exit loop"},
                        "edge_id": {"type": "string", "description": "Optional specific loop edge ID (defaults to '*')"},
                        "reason": {"type": "string", "description": "Brief rationale for loop decision"},
                        "temp_prompt": {"type": "string", "description": "Optional one-shot temporary prompt to inject into the loop target node or group for the next iteration"},
                    },
                    ["continue_loop"],
                )
            )
        return [
            Tool(
                name=name,
                description=description,
                inputSchema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )
            for name, description, properties, required in defs
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None):
        args = arguments if isinstance(arguments, dict) else {}
        graph = graph_accessor()
        states = state_accessor() if state_accessor else {}
        node_by_id, group_by_id = _indexes(graph)

        if name == "list_dependencies":
            payload = {
                "current_node_id": current_node_id,
                "direct_dependencies": scope.get("direct_dependencies", []),
                "accessible_nodes": [
                    {
                        "id": nid,
                        "label": node_by_id[nid].get("label") or nid,
                        "model": node_by_id[nid].get("model") or "",
                        "has_output": bool(str(node_by_id[nid].get("output") or "").strip()),
                        "has_temp_prompt": bool(str(node_by_id[nid].get("temp_prompt") or "").strip()),
                        "completed": bool(node_by_id[nid].get("completed", False)),
                        "parent_group_id": node_by_id[nid].get("parent_group_id"),
                    }
                    for nid in sorted(scope.get("allowed_node_ids", set()))
                    if nid in node_by_id
                ],
                "accessible_groups": [
                    {
                        "id": gid,
                        "label": group_by_id[gid].get("label") or gid,
                        "has_temp_pre_prompt": bool(str(group_by_id[gid].get("temp_pre_prompt") or "").strip()),
                        "completed": bool(group_by_id[gid].get("completed", False)),
                        "parent_group_id": group_by_id[gid].get("parent_group_id"),
                        "node_ids": list(group_by_id[gid].get("node_ids") or []),
                        "child_group_ids": list(group_by_id[gid].get("child_group_ids") or []),
                    }
                    for gid in sorted(scope.get("allowed_group_ids", set()))
                    if gid in group_by_id
                ],
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "get_node_info":
            req_ids: list[str] = []
            if isinstance(args.get("node_id"), str) and args["node_id"].strip():
                req_ids.append(args["node_id"].strip())
            if isinstance(args.get("node_ids"), list):
                for item in args["node_ids"]:
                    if isinstance(item, str) and item.strip() and item.strip() not in req_ids:
                        req_ids.append(item.strip())
            if not req_ids:
                return [TextContent(type="text", text=json.dumps({"error": "NODE_ID_REQUIRED"}, ensure_ascii=False))]

            allowed = scope.get("allowed_node_ids", set())
            results: list[dict[str, Any]] = []
            for nid in req_ids:
                if nid not in allowed:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "ACCESS_DENIED",
                                    "node_id": nid,
                                    "message": f"노드 {nid!r}은(는) 현재 노드의 의존 범위에 속하지 않습니다.",
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                if nid not in node_by_id:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps({"error": "NODE_NOT_FOUND", "node_id": nid}, ensure_ascii=False),
                        )
                    ]
                results.append(_serialize_node_info(graph, nid, states.get(nid)))

            payload: Any = results[0] if len(results) == 1 and "node_ids" not in args else {"nodes": results}
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "get_group_info":
            gid = str(args.get("group_id") or "").strip()
            if not gid:
                return [TextContent(type="text", text=json.dumps({"error": "GROUP_ID_REQUIRED"}, ensure_ascii=False))]
            if gid not in scope.get("allowed_group_ids", set()):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "ACCESS_DENIED",
                                "group_id": gid,
                                "message": f"그룹 {gid!r}은(는) 현재 노드의 의존 범위에 속하지 않습니다.",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if gid not in group_by_id:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "GROUP_NOT_FOUND", "group_id": gid}, ensure_ascii=False),
                    )
                ]
            recursive = args.get("recursive", True) is not False
            payload = _serialize_group_info(graph, gid, recursive=recursive, node_states=states)
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "signal_loop_decision" and has_agent_loop:
            cont = bool(args.get("continue_loop", False))
            eid = str(args.get("edge_id") or "*").strip() or "*"
            reason = _s(args.get("reason"), 400).strip()
            dyn_temp = _s(args.get("temp_prompt"), MAX_PROMPT_CHARS)
            if loop_decision_cb is not None:
                loop_decision_cb(eid, cont, reason, dyn_temp)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"ok": True, "edge_id": eid, "continue_loop": cont, "reason": reason, "has_temp_prompt": bool(dyn_temp.strip())},
                        ensure_ascii=False,
                    ),
                )
            ]

        raise ValueError(f"Unknown MCP tool: {name}")

    return server


# ------------------------------------------------------------------ Node execution via MCP
def _build_node_messages(
    graph: dict[str, Any],
    node: dict[str, Any],
    scope: dict[str, Any],
    *,
    loop_feedback: list[dict[str, Any]] | None = None,
    artifact_context: str = "",
) -> list[dict[str, Any]]:
    pre_prompts = effective_pre_prompts(graph, node["id"])
    sys_parts = [
        "당신은 IRADE Prompt Dependency Graph 실행 에이전트입니다.",
        f"현재 실행 중인 노드: {node.get('label') or node['id']} (id={node['id']}).",
        "사용자의 프롬프트를 정확하게 수행하고 최종 결과만 명확히 작성하세요.",
    ]

    if pre_prompts:
        sys_parts.append("\n[적용된 프리 프롬프트 (바깥 그룹 -> 안쪽 그룹 순)]")
        for idx, item in enumerate(pre_prompts, 1):
            if str(item.get("pre_prompt") or "").strip():
                sys_parts.append(
                    f"--- 프리 프롬프트 #{idx}: {item['label']} (group_id={item['group_id']}) ---\n{item['pre_prompt']}"
                )
            if str(item.get("temp_pre_prompt") or "").strip():
                sys_parts.append(
                    f"--- [임시 프리 프롬프트 (1회성): {item['label']} (group_id={item['group_id']})] ---\n"
                    f"{item['temp_pre_prompt']}"
                )

    direct = scope.get("direct_dependencies", [])
    if direct:
        dep_lines = []
        for d in direct:
            via = "노드 직접 의존" if d["via"] == "self" else f"상위 그룹 {d['via']}로부터 상속된 의존"
            kind_lbl = "프리 프롬프트 그룹" if d["kind"] == "group" else "프롬프트 노드"
            dep_lines.append(f"- {kind_lbl} `{d['id']}` ({via})")
        sys_parts.append(
            "\n[의존 대상 및 MCP 조회 안내]\n"
            "이 노드는 아래 의존 대상들의 실행이 완료된 뒤 실행되었습니다.\n"
            + "\n".join(dep_lines)
            + "\n필요한 선행 노드의 프롬프트, 출력 결과, 실행 모델, 추가 정보 및 의존 그룹의 프리 프롬프트·내부 노드 정보는 "
            "반드시 MCP 도구(`list_dependencies`, `get_node_info`, `get_group_info`)로 직접 조회하여 반영하세요."
        )

    user_parts = [node.get("prompt") or "(프롬프트가 비어 있습니다.)"]
    temp_prompt = str(node.get("temp_prompt") or "")
    if temp_prompt.strip():
        user_parts.append("\n[임시 프롬프트 (1회성 추가 지시)]\n" + temp_prompt)
    if str(artifact_context or "").strip():
        user_parts.append("\n[연결된 프로젝트 산출물 내용]\n" + str(artifact_context)[:16000])
    if loop_feedback:
        fb_lines = []
        for fb in loop_feedback[-3:]:
            errs = "; ".join(fb.get("contract_errors") or [])
            fb_lines.append(
                f"- 반복 #{fb.get('iteration', '?')} (트리거: {fb.get('from_node', '')})"
                + (f" [계약 오류: {errs}]" if errs else "")
                + f"\n{str(fb.get('trigger_output') or '')[:3000]}"
            )
        user_parts.append("\n[조건부 재귀/반복 실행 피드백 — 위 지적 사항을 반영해 결과를 개선하세요]\n" + "\n\n".join(fb_lines))
    extra = node.get("extra") or {}
    if extra:
        user_parts.append(
            "\n[노드 추가 정보 (extra)]\n" + json.dumps(extra, ensure_ascii=False, indent=2, default=str)
        )

    return [
        {"role": "system", "content": "\n".join(sys_parts)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _extract_choice_message(completion: Any) -> tuple[str, list[dict[str, Any]], str | None, dict[str, Any]]:
    """Normalize both OpenAI SDK ChatCompletion objects and raw dicts (e.g. FakeGateway)."""
    if isinstance(completion, dict):
        choices = completion.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
        calls = [
            {
                "id": str(c.get("id") or f"call_{i}"),
                "type": "function",
                "function": {
                    "name": (c.get("function") or {}).get("name") or "",
                    "arguments": (c.get("function") or {}).get("arguments") or "{}",
                },
            }
            for i, c in enumerate(raw_calls)
        ]
        model = completion.get("model")
        usage = completion.get("usage") or {}
        return str(content), calls, model, usage if isinstance(usage, dict) else {}

    choice = completion.choices[0]
    msg = choice.message
    content = getattr(msg, "content", None) or ""
    raw_calls = getattr(msg, "tool_calls", None) or []
    calls = []
    for i, c in enumerate(raw_calls):
        if hasattr(c, "model_dump"):
            dumped = c.model_dump(exclude_none=True)
            calls.append(dumped)
        else:
            fn = getattr(c, "function", None)
            calls.append(
                {
                    "id": str(getattr(c, "id", None) or f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": getattr(fn, "name", "") if fn else "",
                        "arguments": getattr(fn, "arguments", "{}") if fn else "{}",
                    },
                }
            )
    usage_obj = getattr(completion, "usage", None)
    usage = usage_obj.model_dump() if hasattr(usage_obj, "model_dump") else (usage_obj if isinstance(usage_obj, dict) else {})
    return str(content), calls, getattr(completion, "model", None), usage


async def execute_prompt_node_with_mcp(
    graph_accessor: Callable[[], dict[str, Any]],
    node_id: str,
    chat_fn: Callable[..., Any],
    *,
    state_accessor: Callable[[], dict[str, dict[str, Any]]] | None = None,
    on_mcp_call: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    loop_feedback: list[dict[str, Any]] | None = None,
    artifact_context: str = "",
    on_contract_retry: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Execute one prompt node using a real MCP server bound to its dependency scope."""
    graph = graph_accessor()
    node_by_id, _ = _indexes(graph)
    if node_id not in node_by_id:
        raise PromptGraphError(f"unknown node {node_id!r}")
    node = node_by_id[node_id]
    scope = dependency_scope(graph, node_id)
    messages = _build_node_messages(
        graph,
        node,
        scope,
        loop_feedback=loop_feedback,
        artifact_context=artifact_context,
    )

    settings = graph.get("settings") or {}
    model = node.get("model") or settings.get("default_model") or "mid"
    max_steps = _int(settings.get("max_tool_steps", 8), 1, 16, 8)
    extra = node.get("extra") or {}

    owner_ids = {node_id, *(g["id"] for g in ancestor_groups(graph, node_id))}
    has_agent_loop = any(
        e.get("is_loop")
        and (e.get("condition") or {}).get("type") == "agent_decide"
        and e.get("to") in owner_ids
        for e in graph.get("edges") or []
    )
    agent_loop_decisions: dict[str, dict[str, Any]] = {}

    def _on_loop_dec(eid: str, cont: bool, reason: str, temp_prompt: str = "") -> None:
        rec = {"continue_loop": cont, "reason": reason, "temp_prompt": temp_prompt}
        agent_loop_decisions[eid] = rec
        agent_loop_decisions["*"] = rec

    server = make_prompt_graph_mcp_server(
        graph_accessor,
        node_id,
        scope,
        state_accessor=state_accessor,
        has_agent_loop=has_agent_loop,
        loop_decision_cb=_on_loop_dec,
    )
    mcp_calls: list[dict[str, Any]] = []
    actual_model = model
    total_usage: dict[str, int] = {}
    contract_cfg = _normalize_contract(node.get("contract"))

    async def _run_mcp_turn() -> str:
        nonlocal actual_model
        output_text = ""
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.description,
                                "parameters": t.inputSchema,
                            },
                        }
                        for t in (await session.list_tools()).tools
                    ]

                    for _step in range(max_steps):
                        resp = chat_fn(
                            model=model,
                            messages=deepcopy(messages),
                            tools=tools,
                            node=deepcopy(node),
                            extra=deepcopy(extra),
                        )
                        if inspect.isawaitable(resp):
                            resp = await resp
                        content, calls, resp_model, usage = _extract_choice_message(resp)
                        if resp_model:
                            actual_model = resp_model
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            if isinstance(usage.get(k), int):
                                total_usage[k] = total_usage.get(k, 0) + usage[k]

                        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                        if calls:
                            assistant_msg["tool_calls"] = calls
                        messages.append(assistant_msg)

                        if not calls:
                            output_text = content
                            break

                        for call in calls:
                            fn = call.get("function") or {}
                            tool_name = str(fn.get("name") or "")
                            raw_args = fn.get("arguments") or "{}"
                            try:
                                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                            except json.JSONDecodeError:
                                args = {}
                            try:
                                tool_resp = await session.call_tool(tool_name, args if isinstance(args, dict) else {})
                                result_text = "\n".join(
                                    getattr(block, "text", "") for block in (tool_resp.content or [])
                                )
                            except Exception as exc:
                                result_text = json.dumps(
                                    {"error": "MCP_TOOL_FAILED", "message": str(exc)[:300]},
                                    ensure_ascii=False,
                                )
                            if len(result_text) > MAX_TOOL_RESULT_CHARS:
                                result_text = json.dumps(
                                    {"error": "MCP_RESULT_TOO_LARGE", "limit": MAX_TOOL_RESULT_CHARS},
                                    ensure_ascii=False,
                                )

                            call_record = {
                                "tool": tool_name,
                                "arguments": args,
                                "result_preview": result_text[:600],
                            }
                            mcp_calls.append(call_record)
                            if on_mcp_call is not None:
                                maybe = on_mcp_call({"node_id": node_id, **call_record, "result": result_text})
                                if inspect.isawaitable(maybe):
                                    await maybe

                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.get("id") or f"call_{tool_name}",
                                    "content": result_text,
                                }
                            )
                    else:
                        output_text = messages[-1].get("content") or "" if messages[-1].get("role") == "assistant" else ""
                        if not output_text:
                            output_text = "MCP 도구 조회 단계 상한에 도달했습니다."

                tg.cancel_scope.cancel()
        return output_text

    output_text = await _run_mcp_turn()
    contract_res = evaluate_output_contract(output_text, contract_cfg)
    retries_left = contract_cfg["max_retries"] if contract_cfg["enabled"] else 0
    retry_attempt = 0
    while not contract_res["passed"] and retries_left > 0:
        retries_left -= 1
        retry_attempt += 1
        if on_contract_retry is not None:
            maybe = on_contract_retry({"node_id": node_id, "attempt": retry_attempt, "errors": contract_res["errors"]})
            if inspect.isawaitable(maybe):
                await maybe
        messages.append(
            {
                "role": "user",
                "content": (
                    "[출력 계약 검증 실패 — 아래 조건을 모두 만족하도록 결과를 다시 작성하세요]\n- "
                    + "\n- ".join(contract_res["errors"])
                ),
            }
        )
        output_text = await _run_mcp_turn()
        contract_res = evaluate_output_contract(output_text, contract_cfg)

    if not contract_res["passed"]:
        has_fail_edge = any(
            (e.get("condition") or {}).get("type") == "on_contract_fail"
            and e.get("to") in owner_ids
            for e in graph.get("edges") or []
        )
        if contract_cfg["on_fail"] != "continue" and not has_fail_edge:
            raise RuntimeError("출력 계약 검증 실패: " + "; ".join(contract_res["errors"]))

    return {
        "output": output_text,
        "model": actual_model,
        "mcp_calls": mcp_calls,
        "usage": total_usage,
        "contract_passed": bool(contract_res["passed"]),
        "contract_errors": list(contract_res["errors"]),
        "contract_result": contract_res,
        "agent_loop_decisions": agent_loop_decisions,
    }


# ------------------------------------------------------------------ Dependent impact review via MCP
MCP_IMPACT_TOOLS = [
    (
        "list_dependents",
        "List the prompt nodes and pre-prompt groups that directly depend on the current node.",
        {},
        [],
    ),
    (
        "get_dependent_info",
        "Read detailed info (prompt, current output, model, extra, pre_prompts, child nodes/groups) of a dependent node or group.",
        {
            "entity_id": {"type": "string", "description": "Dependent node or group ID"},
            "recursive": {"type": "boolean", "description": "Include nested child groups recursively (default true)"},
        },
        ["entity_id"],
    ),
    (
        "mark_needs_rerun",
        "Mark one or more dependent completed nodes or groups as needs_rerun ONLY when this node's updated output invalidates them.",
        {
            "entity_id": {"type": "string", "description": "Single dependent node or group ID to mark needs_rerun"},
            "entity_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of dependent node or group IDs to mark needs_rerun",
            },
            "reason": {"type": "string", "description": "Brief explanation of why re-execution is needed"},
        },
        [],
    ),
    (
        "record_no_impact",
        "Record why a dependent completed node or group does NOT need re-execution after inspecting it.",
        {
            "entity_id": {"type": "string", "description": "Dependent node or group ID that remains valid"},
            "reason": {"type": "string", "description": "Brief explanation of why current output stays valid"},
        },
        ["entity_id", "reason"],
    ),
]


def make_dependent_impact_mcp_server(
    graph_accessor: Callable[[], dict[str, Any]],
    current_node_id: str,
    dep_scope: dict[str, Any],
    state_accessor: Callable[[], dict[str, dict[str, Any]]] | None = None,
    on_mark_needs_rerun: Callable[[str, str], None] | None = None,
    on_record_impact: Callable[[str, bool, str], None] | None = None,
) -> Server:
    """Create a real MCP server scoped strictly to entities depending on `current_node_id`."""
    server = Server("irade-prompt-graph-impact-mcp")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=name,
                description=description,
                inputSchema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )
            for name, description, properties, required in MCP_IMPACT_TOOLS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None):
        args = arguments if isinstance(arguments, dict) else {}
        graph = graph_accessor()
        states = state_accessor() if state_accessor else {}
        node_by_id, group_by_id = _indexes(graph)
        allowed_nodes = dep_scope.get("allowed_node_ids", set())
        allowed_groups = dep_scope.get("allowed_group_ids", set())

        if name == "list_dependents":
            payload = {
                "current_node_id": current_node_id,
                "direct_dependents": dep_scope.get("direct_dependents", []),
                "dependent_nodes": [
                    {
                        "id": nid,
                        "label": node_by_id[nid].get("label") or nid,
                        "completed": bool(node_by_id[nid].get("completed", False)),
                        "needs_rerun": bool(node_by_id[nid].get("needs_rerun", False)),
                        "rerun_reason": node_by_id[nid].get("rerun_reason") or "",
                        "parent_group_id": node_by_id[nid].get("parent_group_id"),
                    }
                    for nid in sorted(allowed_nodes)
                    if nid in node_by_id
                ],
                "dependent_groups": [
                    {
                        "id": gid,
                        "label": group_by_id[gid].get("label") or gid,
                        "completed": bool(group_by_id[gid].get("completed", False)),
                        "needs_rerun": bool(group_by_id[gid].get("needs_rerun", False)),
                        "rerun_reason": group_by_id[gid].get("rerun_reason") or "",
                        "node_ids": list(group_by_id[gid].get("node_ids") or []),
                        "child_group_ids": list(group_by_id[gid].get("child_group_ids") or []),
                    }
                    for gid in sorted(allowed_groups)
                    if gid in group_by_id
                ],
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "get_dependent_info":
            eid = str(args.get("entity_id") or "").strip()
            if not eid:
                return [TextContent(type="text", text=json.dumps({"error": "ENTITY_ID_REQUIRED"}, ensure_ascii=False))]
            if eid not in allowed_nodes and eid not in allowed_groups:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "ACCESS_DENIED",
                                "entity_id": eid,
                                "message": f"대상 {eid!r}은(는) 현재 노드에 의존하는 대상이 아닙니다.",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if eid in node_by_id:
                payload = {"kind": "node", **_serialize_node_info(graph, eid, states.get(eid))}
            elif eid in group_by_id:
                recursive = args.get("recursive", True) is not False
                payload = {"kind": "group", **_serialize_group_info(graph, eid, recursive=recursive, node_states=states)}
            else:
                payload = {"error": "ENTITY_NOT_FOUND", "entity_id": eid}
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "mark_needs_rerun":
            req_ids: list[str] = []
            if isinstance(args.get("entity_id"), str) and args["entity_id"].strip():
                req_ids.append(args["entity_id"].strip())
            if isinstance(args.get("entity_ids"), list):
                for item in args["entity_ids"]:
                    if isinstance(item, str) and item.strip() and item.strip() not in req_ids:
                        req_ids.append(item.strip())
            if not req_ids:
                return [TextContent(type="text", text=json.dumps({"error": "ENTITY_ID_REQUIRED"}, ensure_ascii=False))]

            for eid in req_ids:
                if eid not in allowed_nodes and eid not in allowed_groups:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "ACCESS_DENIED",
                                    "entity_id": eid,
                                    "message": f"대상 {eid!r}은(는) 현재 노드에 의존하는 대상이 아닙니다.",
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]

            reason = _s(args.get("reason"), 1000).strip() or f"선행 노드 {current_node_id} 변경 반영 필요"
            marked: list[str] = []
            skipped_unexecuted: list[str] = []
            for eid in req_ids:
                ent = node_by_id.get(eid) or group_by_id.get(eid)
                if not ent or not ent.get("completed", False):
                    skipped_unexecuted.append(eid)
                    continue
                if on_mark_needs_rerun is not None:
                    on_mark_needs_rerun(eid, reason)
                if on_record_impact is not None:
                    on_record_impact(eid, True, reason)
                marked.append(eid)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"ok": True, "marked": marked, "skipped_unexecuted": skipped_unexecuted, "reason": reason},
                        ensure_ascii=False,
                    ),
                )
            ]

        if name == "record_no_impact":
            eid = str(args.get("entity_id") or "").strip()
            if not eid:
                return [TextContent(type="text", text=json.dumps({"error": "ENTITY_ID_REQUIRED"}, ensure_ascii=False))]
            if eid not in allowed_nodes and eid not in allowed_groups:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "ACCESS_DENIED",
                                "entity_id": eid,
                                "message": f"대상 {eid!r}은(는) 현재 노드에 의존하는 대상이 아닙니다.",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            reason = _s(args.get("reason"), 1000).strip() or f"선행 노드 {current_node_id} 변경의 영향 없음"
            if on_record_impact is not None:
                on_record_impact(eid, False, reason)
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"ok": True, "entity_id": eid, "needs_rerun": False, "reason": reason}, ensure_ascii=False),
                )
            ]

        raise ValueError(f"Unknown MCP impact tool: {name}")

    return server


async def evaluate_dependents_with_mcp(
    graph_accessor: Callable[[], dict[str, Any]],
    current_node_id: str,
    dep_scope: dict[str, Any],
    chat_fn: Callable[..., Any],
    *,
    prev_output: str,
    new_output: str,
    state_accessor: Callable[[], dict[str, dict[str, Any]]] | None = None,
    on_mark_needs_rerun: Callable[[str, str], None] | None = None,
    on_record_impact: Callable[[str, bool, str], None] | None = None,
    on_mcp_call: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Ask an agent via real MCP tools to inspect dependent nodes/groups and mark only those needing rerun."""
    graph = graph_accessor()
    node_by_id, _ = _indexes(graph)
    node = node_by_id[current_node_id]
    settings = graph.get("settings") or {}
    model = node.get("model") or settings.get("default_model") or "mid"
    max_steps = _int(settings.get("max_tool_steps", 8), 1, 16, 8)
    extra = node.get("extra") or {}

    direct_lines = [
        f"- {d['kind']} `{d['id']}` (via={d['via']})"
        for d in dep_scope.get("direct_dependents", [])
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "당신은 IRADE Prompt Dependency Graph 변경 영향 평가 에이전트입니다.\n"
                f"방금 실행 완료된 선행 노드 `{node.get('label') or current_node_id}` (id={current_node_id})의 출력이 갱신되었습니다.\n"
                "MCP 도구(`list_dependents`, `get_dependent_info`)로 이 노드에 의존하는 완료된 후속 노드/그룹들의 프롬프트와 기존 출력을 확인한 뒤,\n"
                "변경된 내용 때문에 기존 출력이 무효화되어 **재실행이 실제로 필요한 대상만** 골라 `mark_needs_rerun` 도구로 표시하세요.\n"
                "영향이 없거나 기존 출력이 여전히 유효한 의존 대상에는 `mark_needs_rerun`을 호출하지 말고 필요 시 `record_no_impact`로 근거를 남기세요."
            ),
        },
        {
            "role": "user",
            "content": (
                f"[실행 완료 노드: {current_node_id}]\n"
                f"프롬프트:\n{node.get('prompt') or ''}\n\n"
                f"이전 출력:\n{prev_output or '(없음)'}\n\n"
                f"새 출력:\n{new_output or '(없음)'}\n\n"
                "[확인할 직접 의존 대상 목록]\n"
                + ("\n".join(direct_lines) if direct_lines else "(없음)")
            ),
        },
    ]

    server = make_dependent_impact_mcp_server(
        graph_accessor,
        current_node_id,
        dep_scope,
        state_accessor=state_accessor,
        on_mark_needs_rerun=on_mark_needs_rerun,
        on_record_impact=on_record_impact,
    )
    mcp_calls: list[dict[str, Any]] = []
    total_usage: dict[str, int] = {}

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema,
                        },
                    }
                    for t in (await session.list_tools()).tools
                ]

                for _step in range(max_steps):
                    resp = chat_fn(
                        model=model,
                        messages=deepcopy(messages),
                        tools=tools,
                        node=deepcopy(node),
                        extra=deepcopy(extra),
                    )
                    if inspect.isawaitable(resp):
                        resp = await resp
                    content, calls, _resp_model, usage = _extract_choice_message(resp)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        if isinstance(usage.get(k), int):
                            total_usage[k] = total_usage.get(k, 0) + usage[k]

                    assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                    if calls:
                        assistant_msg["tool_calls"] = calls
                    messages.append(assistant_msg)
                    if not calls:
                        break

                    for call in calls:
                        fn = call.get("function") or {}
                        tool_name = str(fn.get("name") or "")
                        raw_args = fn.get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        except json.JSONDecodeError:
                            args = {}
                        try:
                            tool_resp = await session.call_tool(tool_name, args if isinstance(args, dict) else {})
                            result_text = "\n".join(
                                getattr(block, "text", "") for block in (tool_resp.content or [])
                            )
                        except Exception as exc:
                            result_text = json.dumps(
                                {"error": "MCP_TOOL_FAILED", "message": str(exc)[:300]},
                                ensure_ascii=False,
                            )
                        if len(result_text) > MAX_TOOL_RESULT_CHARS:
                            result_text = json.dumps(
                                {"error": "MCP_RESULT_TOO_LARGE", "limit": MAX_TOOL_RESULT_CHARS},
                                ensure_ascii=False,
                            )
                        call_record = {
                            "tool": tool_name,
                            "arguments": args,
                            "result_preview": result_text[:600],
                        }
                        mcp_calls.append(call_record)
                        if on_mcp_call is not None:
                            maybe = on_mcp_call({"node_id": current_node_id, **call_record, "result": result_text})
                            if inspect.isawaitable(maybe):
                                await maybe
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id") or f"call_{tool_name}",
                                "content": result_text,
                            }
                        )

            tg.cancel_scope.cancel()

    return {"mcp_calls": mcp_calls, "usage": total_usage}


# ------------------------------------------------------------------ Admin MCP Server (Automatic Graph Management)
MCP_ADMIN_TOOLS = [
    (
        "get_graph_overview",
        "Inspect current prompt graph counts, recommended workflow stage (early_simple vs late_complex), overlap report, and change status.",
        {},
        [],
    ),
    (
        "list_entities",
        "List all prompt nodes, N-groups, and dependency/loop edges cleanly.",
        {},
        [],
    ),
    (
        "get_entity_detail",
        "Read complete attributes of a node, group, or edge by ID.",
        {"entity_id": {"type": "string", "description": "Node, group, or edge ID"}},
        ["entity_id"],
    ),
    (
        "validate_graph",
        "Validate graph invariants and detect any layout overlaps or containment violations.",
        {},
        [],
    ),
    (
        "create_node",
        "Create a new prompt node and automatically arrange layout without overlaps.",
        {
            "id": {"type": "string", "description": "Optional node ID"},
            "label": {"type": "string", "description": "Display label"},
            "prompt": {"type": "string", "description": "Prompt instructions"},
            "temp_prompt": {"type": "string", "description": "One-shot temporary prompt"},
            "model": {"type": "string", "description": "Model alias/name"},
            "parent_group_id": {"type": ["string", "null"], "description": "Optional enclosing pre-prompt group ID"},
            "extra": {"type": "object", "description": "Optional extra metadata"},
            "contract": {"type": "object", "description": "Optional output contract rules"},
            "artifact": {"type": "object", "description": "Optional repo_file/spec_query artifact binding"},
        },
        ["label"],
    ),
    (
        "update_node",
        "Update an existing prompt node's fields safely and maintain overlap-free layout.",
        {
            "node_id": {"type": "string", "description": "Target node ID"},
            "label": {"type": "string"},
            "prompt": {"type": "string"},
            "temp_prompt": {"type": "string"},
            "model": {"type": "string"},
            "parent_group_id": {"type": ["string", "null"]},
            "extra": {"type": "object"},
            "contract": {"type": "object"},
            "artifact": {"type": "object"},
            "completed": {"type": "boolean"},
            "needs_rerun": {"type": "boolean"},
            "rerun_reason": {"type": "string"},
        },
        ["node_id"],
    ),
    (
        "create_group",
        "Create a pre-prompt N-group, optionally nested inside parent_group_id and/or adopting existing nodes/groups, then auto-layout without overlaps.",
        {
            "id": {"type": "string", "description": "Optional group ID"},
            "label": {"type": "string", "description": "Display label"},
            "pre_prompt": {"type": "string", "description": "Shared pre-prompt for members"},
            "temp_pre_prompt": {"type": "string"},
            "parent_group_id": {"type": ["string", "null"], "description": "Optional outer parent group ID"},
            "adopt_entity_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Existing node or group IDs to move inside this group",
            },
        },
        ["label"],
    ),
    (
        "update_group",
        "Update a pre-prompt group's fields safely.",
        {
            "group_id": {"type": "string", "description": "Target group ID"},
            "label": {"type": "string"},
            "pre_prompt": {"type": "string"},
            "temp_pre_prompt": {"type": "string"},
            "parent_group_id": {"type": ["string", "null"]},
            "completed": {"type": "boolean"},
            "needs_rerun": {"type": "boolean"},
            "rerun_reason": {"type": "string"},
        },
        ["group_id"],
    ),
    (
        "move_entity_to_group",
        "Move an existing node or group into parent_group_id (or null for root) and re-layout without overlaps.",
        {
            "entity_id": {"type": "string", "description": "Node or group ID to move"},
            "parent_group_id": {"type": ["string", "null"], "description": "Target parent group ID or null for top-level"},
        },
        ["entity_id"],
    ),
    (
        "delete_entity",
        "Remove a node or group (optionally assigning one-shot temp_prompts to dependent entities) and re-layout cleanly.",
        {
            "entity_id": {"type": "string", "description": "Node or group ID to remove"},
            "temp_prompts": {
                "type": "object",
                "description": "Optional map {dependent_id: temp_prompt_text} when destroying a dependency",
            },
        },
        ["entity_id"],
    ),
    (
        "add_edge",
        "Connect an edge between nodes/groups (e.g. from='A', to='B' means A depends on B; or set is_loop=true for conditional recursion) and auto-layout.",
        {
            "from": {"type": "string", "description": "Dependent entity ID (or loop re-run target when is_loop=true)"},
            "to": {"type": "string", "description": "Upstream dependency entity ID (or loop trigger when is_loop=true)"},
            "id": {"type": "string"},
            "is_loop": {"type": "boolean", "description": "True to create a conditional recursion/loop edge"},
            "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20},
            "condition": {"type": "object", "description": "{type, value, case_sensitive}"},
            "temp_prompt": {"type": "string", "description": "One-shot temporary prompt injected automatically on execution/loop"},
            "label": {"type": "string"},
        },
        ["from", "to"],
    ),
    (
        "update_edge",
        "Update properties of an existing edge (condition, is_loop, max_iterations, temp_prompt, label).",
        {
            "edge_id": {"type": "string", "description": "Edge ID"},
            "is_loop": {"type": "boolean"},
            "max_iterations": {"type": "integer", "minimum": 0, "maximum": 20},
            "condition": {"type": "object"},
            "temp_prompt": {"type": "string"},
            "label": {"type": "string"},
        },
        ["edge_id"],
    ),
    (
        "remove_edge",
        "Remove an edge by edge_id or by (from, to) pair.",
        {
            "edge_id": {"type": "string"},
            "from": {"type": "string"},
            "to": {"type": "string"},
        },
        [],
    ),
    (
        "update_settings",
        "Update graph-wide settings such as edge_direction ('dependency'|'execution'), auto_rerun_needs_rerun, cross_graph_cache, parallelism, default_model.",
        {
            "parallelism": {"type": "integer", "minimum": 1, "maximum": 8},
            "max_tool_steps": {"type": "integer", "minimum": 1, "maximum": 16},
            "default_model": {"type": "string"},
            "auto_rerun_needs_rerun": {"type": "boolean"},
            "edge_direction": {"type": "string", "enum": ["dependency", "execution"]},
            "cross_graph_cache": {"type": "boolean"},
        },
        [],
    ),
    (
        "auto_layout_graph",
        "Arrange all nodes and nested N-groups into an overlap-free layout honoring edge_direction.",
        {
            "direction": {
                "type": "string",
                "enum": ["auto", "execution", "dependency"],
                "description": "Layout flow direction (default 'auto' follows settings.edge_direction)",
            }
        },
        [],
    ),
]


def _fresh_entity_id(prefix: str, used: set[str]) -> str:
    idx = 1
    while f"{prefix}_{idx}" in used:
        idx += 1
    return f"{prefix}_{idx}"


def make_prompt_graph_admin_mcp_server(
    graph_accessor: Callable[[], dict[str, Any]],
    graph_mutator: Callable[[dict[str, Any]], Any] | None = None,
) -> Server:
    """Create a real transactional MCP server for managing prompt graphs with guaranteed zero-overlap layout."""
    server = Server("irade-prompt-graph-admin-mcp")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=name,
                description=description,
                inputSchema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )
            for name, description, properties, required in MCP_ADMIN_TOOLS
        ]

    def _commit(candidate: dict[str, Any], *, relayout: bool = True, direction: str = "auto") -> tuple[dict[str, Any], dict[str, Any]]:
        clean = validate_prompt_graph(candidate)
        final_g = auto_layout_graph(clean, direction=direction) if relayout else clean
        report = detect_layout_overlaps(final_g)
        if not report["ok"]:
            final_g = auto_layout_graph(final_g, direction=direction)
            report = detect_layout_overlaps(final_g)
        if graph_mutator is not None:
            graph_mutator(final_g)
        return final_g, report

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None):
        args = arguments if isinstance(arguments, dict) else {}
        cur = deepcopy(graph_accessor() or {"nodes": [], "groups": [], "edges": [], "settings": {}})

        if name == "get_graph_overview":
            nc = len(cur.get("nodes") or [])
            gc = len(cur.get("groups") or [])
            ec = len(cur.get("edges") or [])
            max_tier = max([group_tier(cur, g["id"]) for g in cur.get("groups") or []] or [0])
            stage = "early_simple" if (nc < 5 and gc < 2) else ("mid" if nc < 10 else "late_complex")
            report = detect_layout_overlaps(cur)
            payload = {
                "ok": True,
                "counts": {"nodes": nc, "groups": gc, "edges": ec, "max_group_tier": max_tier},
                "recommended_stage": stage,
                "stage_note": (
                    "초반 소규모 그래프입니다. 직접 캔버스 편집이 빠를 수 있으나 필요시 MCP 관리도 사용할 수 있습니다."
                    if stage == "early_simple"
                    else "후반기/다계층 그래프에 적합합니다. 자동 레이아웃이 겹침 없이 정돈된 배치를 유지합니다."
                ),
                "settings": cur.get("settings") or {},
                "layout_report": report,
                "change_summary": {
                    st: sum(1 for v in compute_change_status(cur)["nodes"].values() if v["state"] == st)
                    for st in ("clean", "modified", "needs_rerun", "unexecuted")
                },
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "list_entities":
            payload = {
                "ok": True,
                "nodes": [
                    {
                        "id": n["id"],
                        "label": n.get("label") or n["id"],
                        "parent_group_id": n.get("parent_group_id"),
                        "completed": bool(n.get("completed", False)),
                        "needs_rerun": bool(n.get("needs_rerun", False)),
                        "dependencies": list(n.get("dependencies") or []),
                        "x": n.get("x"),
                        "y": n.get("y"),
                    }
                    for n in cur.get("nodes") or []
                ],
                "groups": [
                    {
                        "id": g["id"],
                        "label": g.get("label") or g["id"],
                        "tier": group_tier(cur, g["id"]),
                        "parent_group_id": g.get("parent_group_id"),
                        "node_ids": list(g.get("node_ids") or []),
                        "child_group_ids": list(g.get("child_group_ids") or []),
                        "x": g.get("x"),
                        "y": g.get("y"),
                        "width": g.get("width"),
                        "height": g.get("height"),
                    }
                    for g in cur.get("groups") or []
                ],
                "edges": [
                    {
                        "id": e.get("id"),
                        "from": e.get("from"),
                        "to": e.get("to"),
                        "is_loop": bool(e.get("is_loop", False)),
                        "condition": e.get("condition") or {"type": "always"},
                        "has_temp_prompt": bool(str(e.get("temp_prompt") or "").strip()),
                    }
                    for e in cur.get("edges") or []
                ],
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        if name == "get_entity_detail":
            eid = str(args.get("entity_id") or "").strip()
            for col in ("nodes", "groups", "edges"):
                for item in cur.get(col) or []:
                    if item.get("id") == eid:
                        return [TextContent(type="text", text=json.dumps({"ok": True, "kind": col[:-1], "entity": item}, ensure_ascii=False))]
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"ENTITY_NOT_FOUND: {eid!r}"}, ensure_ascii=False))]

        if name == "validate_graph":
            try:
                clean = validate_prompt_graph(cur)
                report = detect_layout_overlaps(clean)
                return [TextContent(type="text", text=json.dumps({"ok": True, "layout_report": report}, ensure_ascii=False))]
            except PromptGraphError as exc:
                return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))]

        try:
            if name == "create_node":
                used = {x["id"] for x in (cur.get("nodes") or []) + (cur.get("groups") or [])}
                nid = str(args.get("id") or "").strip() or _fresh_entity_id("node", used)
                node_obj: dict[str, Any] = {
                    "id": nid,
                    "label": args.get("label") or nid,
                    "prompt": args.get("prompt") or "",
                    "temp_prompt": args.get("temp_prompt") or "",
                    "model": args.get("model") or "",
                    "parent_group_id": args.get("parent_group_id"),
                    "extra": args.get("extra") or {},
                    "contract": args.get("contract") or {},
                    "artifact": args.get("artifact") or {},
                    "x": 80,
                    "y": 80,
                    "width": 250,
                    "height": 140,
                }
                cur.setdefault("nodes", []).append(node_obj)
                final_g, report = _commit(cur)
                created = next(n for n in final_g["nodes"] if n["id"] == nid)
                return [TextContent(type="text", text=json.dumps({"ok": True, "node": created, "layout_report": report}, ensure_ascii=False))]

            if name == "update_node":
                nid = str(args.get("node_id") or "").strip()
                target = next((n for n in cur.get("nodes") or [] if n.get("id") == nid), None)
                if target is None:
                    raise PromptGraphError(f"unknown node {nid!r}")
                for k in ("label", "prompt", "temp_prompt", "model", "parent_group_id", "extra", "contract", "artifact", "completed", "needs_rerun", "rerun_reason"):
                    if k in args:
                        target[k] = args[k]
                final_g, report = _commit(cur)
                updated = next(n for n in final_g["nodes"] if n["id"] == nid)
                return [TextContent(type="text", text=json.dumps({"ok": True, "node": updated, "layout_report": report}, ensure_ascii=False))]

            if name == "create_group":
                used = {x["id"] for x in (cur.get("nodes") or []) + (cur.get("groups") or [])}
                gid = str(args.get("id") or "").strip() or _fresh_entity_id("group", used)
                grp_obj: dict[str, Any] = {
                    "id": gid,
                    "label": args.get("label") or gid,
                    "pre_prompt": args.get("pre_prompt") or "",
                    "temp_pre_prompt": args.get("temp_pre_prompt") or "",
                    "parent_group_id": args.get("parent_group_id"),
                    "node_ids": [],
                    "child_group_ids": [],
                    "x": 60,
                    "y": 60,
                    "width": 320,
                    "height": 200,
                }
                cur.setdefault("groups", []).append(grp_obj)
                adopts = args.get("adopt_entity_ids") or []
                if isinstance(adopts, list):
                    for aid in adopts:
                        a_str = str(aid or "").strip()
                        if a_str == gid:
                            continue
                        ent = next((x for x in cur["nodes"] + cur["groups"] if x["id"] == a_str), None)
                        if ent is None:
                            raise PromptGraphError(f"unknown entity {a_str!r} in adopt_entity_ids")
                        ent["parent_group_id"] = gid
                final_g, report = _commit(cur)
                created = next(g for g in final_g["groups"] if g["id"] == gid)
                return [TextContent(type="text", text=json.dumps({"ok": True, "group": created, "tier": group_tier(final_g, gid), "layout_report": report}, ensure_ascii=False))]

            if name == "update_group":
                gid = str(args.get("group_id") or "").strip()
                target = next((g for g in cur.get("groups") or [] if g.get("id") == gid), None)
                if target is None:
                    raise PromptGraphError(f"unknown group {gid!r}")
                for k in ("label", "pre_prompt", "temp_pre_prompt", "parent_group_id", "completed", "needs_rerun", "rerun_reason"):
                    if k in args:
                        target[k] = args[k]
                final_g, report = _commit(cur)
                updated = next(g for g in final_g["groups"] if g["id"] == gid)
                return [TextContent(type="text", text=json.dumps({"ok": True, "group": updated, "tier": group_tier(final_g, gid), "layout_report": report}, ensure_ascii=False))]

            if name == "move_entity_to_group":
                eid = str(args.get("entity_id") or "").strip()
                pgid = args.get("parent_group_id")
                ent = next((x for x in (cur.get("nodes") or []) + (cur.get("groups") or []) if x.get("id") == eid), None)
                if ent is None:
                    raise PromptGraphError(f"unknown entity {eid!r}")
                ent["parent_group_id"] = str(pgid).strip() if pgid else None
                final_g, report = _commit(cur)
                return [TextContent(type="text", text=json.dumps({"ok": True, "entity_id": eid, "parent_group_id": ent["parent_group_id"], "layout_report": report}, ensure_ascii=False))]

            if name == "delete_entity":
                eid = str(args.get("entity_id") or "").strip()
                tprompts = args.get("temp_prompts") if isinstance(args.get("temp_prompts"), dict) else {}
                updated = delete_entity_with_temp_prompts(cur, eid, tprompts)
                final_g, report = _commit(updated)
                return [TextContent(type="text", text=json.dumps({"ok": True, "deleted": eid, "layout_report": report}, ensure_ascii=False))]

            if name == "add_edge":
                used_e = {str(e.get("id") or "") for e in cur.get("edges") or []}
                eid = str(args.get("id") or "").strip() or _fresh_entity_id("e", used_e)
                edge_obj: dict[str, Any] = {
                    "id": eid,
                    "from": str(args.get("from") or "").strip(),
                    "to": str(args.get("to") or "").strip(),
                    "is_loop": bool(args.get("is_loop", False)),
                    "max_iterations": args.get("max_iterations", 3 if args.get("is_loop") else 0),
                    "condition": args.get("condition") or {"type": "always"},
                    "temp_prompt": args.get("temp_prompt") or "",
                    "label": args.get("label") or "",
                }
                cur.setdefault("edges", []).append(edge_obj)
                final_g, report = _commit(cur)
                created = next((e for e in final_g["edges"] if e["id"] == eid), final_g["edges"][-1])
                return [TextContent(type="text", text=json.dumps({"ok": True, "edge": created, "layout_report": report}, ensure_ascii=False))]

            if name == "update_edge":
                eid = str(args.get("edge_id") or "").strip()
                target = next((e for e in cur.get("edges") or [] if e.get("id") == eid), None)
                if target is None:
                    raise PromptGraphError(f"unknown edge {eid!r}")
                for k in ("is_loop", "max_iterations", "condition", "temp_prompt", "label"):
                    if k in args:
                        target[k] = args[k]
                final_g, report = _commit(cur)
                updated = next(e for e in final_g["edges"] if e["id"] == eid)
                return [TextContent(type="text", text=json.dumps({"ok": True, "edge": updated, "layout_report": report}, ensure_ascii=False))]

            if name == "remove_edge":
                eid = str(args.get("edge_id") or "").strip()
                frm = str(args.get("from") or "").strip()
                to = str(args.get("to") or "").strip()
                before_n = len(cur.get("edges") or [])
                if eid:
                    cur["edges"] = [e for e in cur.get("edges") or [] if e.get("id") != eid]
                elif frm and to:
                    cur["edges"] = [e for e in cur.get("edges") or [] if not (e.get("from") == frm and e.get("to") == to)]
                else:
                    raise PromptGraphError("edge_id or (from, to) required")
                if len(cur.get("edges") or []) == before_n:
                    raise PromptGraphError("matching edge not found")
                final_g, report = _commit(cur)
                return [TextContent(type="text", text=json.dumps({"ok": True, "edges_remaining": len(final_g["edges"]), "layout_report": report}, ensure_ascii=False))]

            if name == "update_settings":
                st = cur.setdefault("settings", {})
                for k in ("parallelism", "max_tool_steps", "default_model", "auto_rerun_needs_rerun", "edge_direction", "cross_graph_cache"):
                    if k in args:
                        st[k] = args[k]
                final_g, report = _commit(cur, relayout=("edge_direction" in args))
                return [TextContent(type="text", text=json.dumps({"ok": True, "settings": final_g["settings"], "layout_report": report}, ensure_ascii=False))]

            if name == "auto_layout_graph":
                dir_arg = str(args.get("direction") or "auto")
                final_g, report = _commit(cur, relayout=True, direction=dir_arg)
                return [TextContent(type="text", text=json.dumps({"ok": True, "layout_report": report, "direction": dir_arg}, ensure_ascii=False))]

        except PromptGraphError as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))]

        raise ValueError(f"Unknown admin MCP tool: {name}")

    return server


async def call_prompt_graph_admin_mcp_tool(
    graph: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one graph-management tool through the real Admin MCP Server."""
    state = {"graph": validate_prompt_graph(deepcopy(graph or {"nodes": [], "groups": [], "edges": [], "settings": {}}))}
    server = make_prompt_graph_admin_mcp_server(
        lambda: state["graph"],
        lambda new_g: state.__setitem__("graph", new_g),
    )
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                resp = await session.call_tool(tool_name, arguments or {})
                raw_txt = "\n".join(getattr(b, "text", "") for b in (resp.content or []))
                try:
                    parsed = json.loads(raw_txt) if raw_txt else {}
                except json.JSONDecodeError:
                    parsed = {"ok": False, "raw": raw_txt}
            tg.cancel_scope.cancel()
    return {
        "ok": bool(parsed.get("ok", True)),
        "result": parsed,
        "graph": state["graph"],
        "layout_report": detect_layout_overlaps(state["graph"]),
    }


async def manage_prompt_graph_with_mcp(
    graph: dict[str, Any],
    instruction: str,
    chat_fn: Callable[..., Any],
    *,
    max_steps: int = 16,
    on_mcp_call: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    """Run an agent connected to the real Admin MCP Server to construct/refactor a graph cleanly without overlaps."""
    clean_init = validate_prompt_graph(deepcopy(graph or {"nodes": [], "groups": [], "edges": [], "settings": {}}))
    state = {"graph": clean_init, "changed": False}

    def _mutate(new_g: dict[str, Any]) -> None:
        state["graph"] = new_g
        state["changed"] = True

    server = make_prompt_graph_admin_mcp_server(lambda: state["graph"], _mutate)
    settings = clean_init.get("settings") or {}
    model = settings.get("default_model") or "mid"
    steps_limit = _int(max_steps, 1, 24, 16)
    node_stub = {"id": "__admin__", "label": "MCP Graph Manager", "model": model, "extra": {}}

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "당신은 IRADE Prompt Dependency Graph 설계·리팩토링 에이전트입니다.\n"
                "연결된 MCP 도구를 사용해 노드, 중첩 N-그룹(1-그룹/2-그룹/…), 의존/조건/순환 간선, 간선 임시 프롬프트, 출력 계약을 구성·정리하세요.\n"
                "모든 변경은 서버가 자동 검증 및 겹침 없는 계층형 자동 레이아웃(auto_layout_graph)을 수행합니다.\n"
                "초반 소규모 작업보다는 다수 노드/그룹 구조화, 순환/검증 루프 추가, 의존성 리팩토링에 최적화되어 있으며,\n"
                "작업 시작 전 `get_graph_overview` 또는 `list_entities`로 현재 상태를 파악한 뒤 필요한 도구만 호출하고 마지막에 요약을 한국어로 답하세요."
            ),
        },
        {"role": "user", "content": str(instruction or "").strip() or "현재 그래프의 그룹·간선·레이아웃을 점검하고 깔끔하게 정리하세요."},
    ]

    mcp_calls: list[dict[str, Any]] = []
    total_usage: dict[str, int] = {}
    summary = ""

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema,
                        },
                    }
                    for t in (await session.list_tools()).tools
                ]

                for _step in range(steps_limit):
                    resp = chat_fn(
                        model=model,
                        messages=deepcopy(messages),
                        tools=tools,
                        node=node_stub,
                        extra={},
                    )
                    if inspect.isawaitable(resp):
                        resp = await resp
                    content, calls, _resp_model, usage = _extract_choice_message(resp)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        if isinstance(usage.get(k), int):
                            total_usage[k] = total_usage.get(k, 0) + usage[k]

                    assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                    if calls:
                        assistant_msg["tool_calls"] = calls
                    messages.append(assistant_msg)

                    if not calls:
                        summary = content
                        break

                    for call in calls:
                        fn = call.get("function") or {}
                        tool_name = str(fn.get("name") or "")
                        raw_args = fn.get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        except json.JSONDecodeError:
                            args = {}
                        try:
                            tool_resp = await session.call_tool(tool_name, args if isinstance(args, dict) else {})
                            result_text = "\n".join(getattr(b, "text", "") for b in (tool_resp.content or []))
                        except Exception as exc:
                            result_text = json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)
                        rec = {"tool": tool_name, "arguments": args, "result_preview": result_text[:600]}
                        mcp_calls.append(rec)
                        if on_mcp_call is not None:
                            maybe = on_mcp_call(rec)
                            if inspect.isawaitable(maybe):
                                await maybe
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id") or f"call_{tool_name}",
                                "content": result_text,
                            }
                        )
                else:
                    summary = messages[-1].get("content") or "MCP 그래프 관리 단계가 완료되었습니다."
            tg.cancel_scope.cancel()

    final_g = auto_layout_graph(state["graph"]) if state["changed"] else state["graph"]
    report = detect_layout_overlaps(final_g)
    return {
        "graph": final_g,
        "summary": summary,
        "mcp_calls": mcp_calls,
        "usage": total_usage,
        "layout_report": report,
        "changed": state["changed"],
    }


# ------------------------------------------------------------------ Parallel DAG Scheduler
def _render_edge_temp_prompt(
    template: str,
    *,
    iteration: int = 1,
    max_iterations: int = 0,
    source_id: str = "",
    source_label: str = "",
    source_output: str = "",
    contract_errors: list[str] | None = None,
) -> str:
    """Safely render placeholders inside an edge's temporary prompt without str.format brace issues."""
    txt = str(template or "").strip()
    if not txt:
        return ""
    errs = "; ".join(str(x) for x in (contract_errors or []) if str(x).strip())
    for k, v in (
        ("{iteration}", str(iteration)),
        ("{max_iterations}", str(max_iterations)),
        ("{source_id}", str(source_id)),
        ("{source_label}", str(source_label)),
        ("{source_output}", str(source_output or "")[:4000]),
        ("{contract_errors}", errs),
    ):
        if k in txt:
            txt = txt.replace(k, v)
    return txt[:MAX_PROMPT_CHARS]


class PromptGraphRun:
    """Executes a prompt dependency graph in parallel while honoring DAG dependencies, contracts, conditional branches, and loops."""

    def __init__(
        self,
        graph: dict[str, Any],
        chat_fn: Callable[..., Any],
        *,
        target_type: str = "all",
        target_id: str | None = None,
        reuse_existing_outputs: bool = False,
        target_ids: list[str] | None = None,
        force_rerun: bool = False,
        auto_rerun: bool | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        cancel: threading.Event | asyncio.Event | None = None,
        cache_lookup: Callable[[str], dict[str, Any] | None] | None = None,
        on_node_version: Callable[[dict[str, Any]], None] | None = None,
        on_impact_review: Callable[[dict[str, Any]], None] | None = None,
        artifact_reader: Callable[[dict[str, Any], dict[str, Any]], tuple[str, str] | None] | None = None,
        artifact_writer: Callable[[dict[str, Any], str, dict[str, Any]], str | None] | None = None,
    ):
        self.plan = plan_execution(
            graph,
            target_type=target_type,
            target_id=target_id,
            reuse_existing_outputs=reuse_existing_outputs,
            target_ids=target_ids,
            force_rerun=force_rerun,
        )
        self.force_rerun = bool(force_rerun)
        self.graph = deepcopy(self.plan["graph"])
        self.auto_rerun = (
            bool((self.graph.get("settings") or {}).get("auto_rerun_needs_rerun", False))
            if auto_rerun is None
            else bool(auto_rerun)
        )
        self.chat_fn = chat_fn
        self.on_event = on_event or (lambda _ev: None)
        self.cancel = cancel
        self.cache_lookup = cache_lookup
        self.on_node_version = on_node_version
        self.on_impact_review = on_impact_review
        self.artifact_reader = artifact_reader
        self.artifact_writer = artifact_writer

        self._lock = threading.RLock()
        self._seq = 0
        self.node_state: dict[str, dict[str, Any]] = {}
        run_set = set(self.plan["run_nodes"])
        required_set = set(self.plan["required_nodes"])
        for n in self.graph["nodes"]:
            nid = n["id"]
            if nid in run_set:
                self.node_state[nid] = {"status": "pending", "model": n.get("model") or ""}
            elif nid in required_set:
                self.node_state[nid] = {"status": "reused", "model": n.get("model") or ""}
            else:
                self.node_state[nid] = {"status": "idle", "model": n.get("model") or ""}

    def _is_cancelled(self) -> bool:
        if self.cancel is None:
            return False
        return bool(self.cancel.is_set())

    def _emit(
        self,
        ev_type: str,
        node: str | None = None,
        target: str | None = None,
        content: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "type": ev_type,
                "node": node,
                "target": target,
                "content": content,
                "meta": meta or {},
            }
        self.on_event(ev)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self.node_state)

    def graph_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self.graph)

    def run_sync(self) -> dict[str, Any]:
        return asyncio.run(self.run())

    async def run(self) -> dict[str, Any]:
        run_nodes = list(self.plan["run_nodes"])
        eff_deps = {nid: set(self.plan["effective_deps"][nid]) for nid in run_nodes}
        dependents: dict[str, list[str]] = {nid: [] for nid in run_nodes}
        for nid, deps in eff_deps.items():
            for d in deps:
                dependents[d].append(nid)

        self._emit(
            "run_started",
            node=self.plan["target_id"],
            content=f"실행 시작 (대상: {self.plan['target_type']}, 실행 노드 {len(run_nodes)}개)",
            meta={
                "target_type": self.plan["target_type"],
                "target_id": self.plan["target_id"],
                "run_nodes": run_nodes,
            },
        )

        if not run_nodes:
            target_groups = set(self.plan.get("target_groups") or [])
            chg_before = compute_change_status(self.graph)
            # Mark targeted/empty groups completed if not already clean (or force_rerun) and clear temp_pre_prompt
            for g in self.graph.get("groups", []):
                gid = g["id"]
                desc = expand_nodes(self.graph, gid)
                targeted = (
                    self.plan["target_type"] == "all"
                    or (self.plan["target_type"] == "group" and self.plan["target_id"] == gid)
                    or (self.plan["target_type"] == "subset" and gid in target_groups)
                )
                is_clean = chg_before["groups"].get(gid, {}).get("state") == "clean"
                if not desc and targeted and (self.force_rerun or not is_clean):
                    with self._lock:
                        g["completed"] = True
                        cleared_pre = bool(str(g.get("temp_pre_prompt") or "").strip())
                        g["temp_pre_prompt"] = ""
                        g["last_run_pre_prompt"] = g.get("pre_prompt") or ""
                        g["last_run_temp_pre_prompt"] = ""
                        g["needs_rerun"] = False
                        g["rerun_reason"] = ""
                    self._emit(
                        "group_finished",
                        node=gid,
                        content=f"프리 프롬프트 그룹 '{g['label']}' 완료",
                        meta={
                            "group_id": gid,
                            "status": "SUCCEEDED",
                            "completed": True,
                            "cleared_temp_pre_prompt": cleared_pre,
                        },
                    )
            self._emit("run_finished", content="변경되었거나 미실행된 노드가 없어 재실행을 생략했습니다.", meta={"status": "SUCCEEDED"})
            return {
                "status": "SUCCEEDED",
                "graph": self.graph_snapshot(),
                "node_state": self.snapshot(),
                "result": {"outputs": {n["id"]: n["output"] for n in self.graph["nodes"]}},
            }

        parallelism = self.graph["settings"]["parallelism"]
        sem = asyncio.Semaphore(parallelism)
        completed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        started_groups: set[str] = set()
        finished_groups: set[str] = set()

        run_set = set(run_nodes)
        all_eff_deps = effective_node_dependencies(self.graph)
        all_dependents: dict[str, list[str]] = {n["id"]: [] for n in self.graph["nodes"]}
        for n_id, deps_s in all_eff_deps.items():
            for d in deps_s:
                all_dependents.setdefault(d, []).append(n_id)
        auto_queued: set[str] = set()
        loop_counts: dict[str, int] = {}
        injected_forward_edges: set[tuple[str, int]] = set()
        node_iterations: dict[str, int] = {n["id"]: 1 for n in self.graph["nodes"]}
        loop_feedback: dict[str, list[dict[str, Any]]] = {}
        node_contracts: dict[str, dict[str, Any]] = {}
        node_agent_decisions: dict[str, dict[str, dict[str, Any]]] = {}
        group_all_members: dict[str, list[str]] = {
            g["id"]: expand_nodes(self.graph, g["id"])
            for g in self.graph.get("groups", [])
        }
        group_run_members: dict[str, set[str]] = {
            gid: set(members) & run_set
            for gid, members in group_all_members.items()
        }
        group_by_id = {g["id"]: g for g in self.graph.get("groups", [])}
        groups_by_depth = sorted(
            self.graph.get("groups", []),
            key=lambda g: len(ancestor_groups(self.graph, g["id"])),
            reverse=True,
        )
        node_by_id = {n["id"]: n for n in self.graph["nodes"]}

        in_flight: dict[asyncio.Task[dict[str, Any]], str] = {}

        def _compute_loop_body(from_entity: str, to_entity: str) -> set[str]:
            start_set = set(expand_nodes(self.graph, from_entity))
            end_set = set(expand_nodes(self.graph, to_entity))
            if from_entity == to_entity:
                return set(end_set)
            fwd: set[str] = set()
            q = list(start_set)
            while q:
                u = q.pop(0)
                if u in fwd:
                    continue
                fwd.add(u)
                for v in all_dependents.get(u, []):
                    if v not in fwd:
                        q.append(v)
            bwd: set[str] = set()
            q = list(end_set)
            while q:
                u = q.pop(0)
                if u in bwd:
                    continue
                bwd.add(u)
                for v in all_eff_deps.get(u, set()):
                    if v not in bwd:
                        q.append(v)
            body = fwd & bwd
            return body or (start_set | end_set)

        def _will_trigger_loop(nid: str, output_str: str, contract_res: dict[str, Any] | None, agent_decs: dict[str, Any] | None) -> bool:
            snap = self.graph_snapshot()
            for e in snap.get("edges") or []:
                if not e.get("is_loop"):
                    continue
                try:
                    triggers = set(expand_nodes(snap, e["to"]))
                except PromptGraphError:
                    continue
                if nid not in triggers:
                    continue
                if loop_counts.get(e["id"], 0) >= int(e.get("max_iterations") or 3):
                    continue
                if evaluate_edge_condition(e, output_str, contract_res, agent_decs):
                    return True
            return False

        def _check_incoming_conditions(nid: str) -> tuple[bool, str]:
            snap = self.graph_snapshot()
            for e in snap.get("edges") or []:
                if e.get("is_loop"):
                    continue
                cond = e.get("condition") or {}
                if cond.get("type", "always") == "always":
                    continue
                try:
                    downstream = set(expand_nodes(snap, e["from"]))
                    upstream = expand_nodes(snap, e["to"])
                except PromptGraphError:
                    continue
                if nid not in downstream or not upstream:
                    continue
                passed_any = any(
                    evaluate_edge_condition(
                        e,
                        str((node_by_id.get(up) or {}).get("output") or ""),
                        node_contracts.get(up),
                        node_agent_decisions.get(up),
                    )
                    for up in upstream
                )
                if not passed_any:
                    return False, f"조건 미충족 ({cond.get('type')}: {cond.get('value') or ''})"
            return True, ""

        def _inject_temp_prompt_to_entity(
            target_eid: str,
            rendered_text: str,
            edge_id: str,
            trigger_nid: str,
            *,
            is_loop_edge: bool,
            iteration: int,
        ) -> None:
            txt = _s(rendered_text, MAX_PROMPT_CHARS).strip()
            if not txt:
                return
            with self._lock:
                if target_eid in node_by_id:
                    existing = str(node_by_id[target_eid].get("temp_prompt") or "").strip()
                    combined = f"{existing}\n\n{txt}" if existing else txt
                    node_by_id[target_eid]["temp_prompt"] = combined[:MAX_PROMPT_CHARS]
                elif target_eid in group_by_id:
                    existing = str(group_by_id[target_eid].get("temp_pre_prompt") or "").strip()
                    combined = f"{existing}\n\n{txt}" if existing else txt
                    group_by_id[target_eid]["temp_pre_prompt"] = combined[:MAX_PROMPT_CHARS]
                else:
                    return
            self._emit(
                "edge_temp_prompt_injected",
                node=trigger_nid,
                target=target_eid,
                content=txt,
                meta={
                    "edge_id": edge_id,
                    "is_loop": is_loop_edge,
                    "iteration": iteration,
                    "injected_temp_prompt": txt,
                },
            )

        def _mark_group_started(nid: str) -> None:
            for g in ancestor_groups(self.graph, nid):
                gid = g["id"]
                if gid not in started_groups:
                    started_groups.add(gid)
                    self._emit(
                        "group_started",
                        node=gid,
                        content=f"프리 프롬프트 그룹 '{g['label']}' 실행 시작",
                        meta={"group_id": gid},
                    )

        target_groups_set = set(self.plan.get("target_groups") or [])

        def _is_group_ready_to_finish(g: dict[str, Any], all_desc: list[str]) -> bool:
            gid = g["id"]
            if not all_desc:
                return False
            cur_pre_entry = {"pre": g.get("pre_prompt") or "", "temp": g.get("temp_pre_prompt") or ""}
            for did in all_desc:
                dn = node_by_id.get(did)
                if not dn or not dn.get("completed", False):
                    return False
                if dn.get("last_run_prompt") is not None and (dn.get("prompt") or "") != dn["last_run_prompt"]:
                    return False
                if (dn.get("temp_prompt") or "").strip():
                    return False
                if dn.get("needs_rerun"):
                    return False
                lrpp = dn.get("last_run_pre_prompts")
                if not isinstance(lrpp, dict) or lrpp.get(gid) != cur_pre_entry:
                    return False
            # Also ensure no nested child group has an un-applied own temp/pre change
            for cgid in _expand_groups(self.graph, gid):
                if cgid == gid:
                    continue
                cg = group_by_id.get(cgid)
                if cg and not cg.get("completed", False):
                    return False
            return True

        def _check_groups_finished() -> None:
            for g in groups_by_depth:
                gid = g["id"]
                if gid in finished_groups:
                    continue
                members = group_run_members.get(gid, set())
                all_desc = group_all_members.get(gid, [])
                if not members:
                    targeted = (
                        self.plan["target_type"] == "all"
                        or (self.plan["target_type"] == "group" and self.plan["target_id"] == gid)
                        or (self.plan["target_type"] == "subset" and gid in target_groups_set)
                    )
                    if not all_desc and targeted:
                        finished_groups.add(gid)
                        with self._lock:
                            g["completed"] = True
                            cleared_pre = bool(str(g.get("temp_pre_prompt") or "").strip())
                            g["temp_pre_prompt"] = ""
                            g["last_run_pre_prompt"] = g.get("pre_prompt") or ""
                            g["last_run_temp_pre_prompt"] = ""
                            g["needs_rerun"] = False
                            g["rerun_reason"] = ""
                        self._emit(
                            "group_finished",
                            node=gid,
                            content=f"프리 프롬프트 그룹 '{g['label']}' 완료",
                            meta={
                                "group_id": gid,
                                "status": "SUCCEEDED",
                                "completed": True,
                                "cleared_temp_pre_prompt": cleared_pre,
                            },
                        )
                    continue
                if members <= (completed | failed | skipped):
                    finished_groups.add(gid)
                    any_fail = bool(members & (failed | skipped))
                    cleared_pre = False
                    if not any_fail and _is_group_ready_to_finish(g, all_desc):
                        with self._lock:
                            g["completed"] = True
                            cleared_pre = bool(str(g.get("temp_pre_prompt") or "").strip())
                            g["temp_pre_prompt"] = ""
                            g["last_run_pre_prompt"] = g.get("pre_prompt") or ""
                            g["last_run_temp_pre_prompt"] = ""
                            g["needs_rerun"] = False
                            g["rerun_reason"] = ""
                            if cleared_pre:
                                for did in all_desc:
                                    dn = node_by_id.get(did)
                                    if dn and isinstance(dn.get("last_run_pre_prompts"), dict) and gid in dn["last_run_pre_prompts"]:
                                        dn["last_run_pre_prompts"][gid]["temp"] = ""
                    self._emit(
                        "group_finished",
                        node=gid,
                        content=f"프리 프롬프트 그룹 '{g['label']}' 완료",
                        meta={
                            "group_id": gid,
                            "status": "PARTIAL" if any_fail else "SUCCEEDED",
                            "completed": bool(g.get("completed", False)),
                            "cleared_temp_pre_prompt": cleared_pre,
                        },
                    )

        def _cascade_skip(root_nid: str) -> None:
            q = list(dependents.get(root_nid, []))
            while q:
                dep_id = q.pop(0)
                if dep_id in completed or dep_id in failed or dep_id in skipped:
                    continue
                skipped.add(dep_id)
                with self._lock:
                    self.node_state[dep_id] = {
                        "status": "skipped",
                        "model": node_by_id[dep_id].get("model") or "",
                        "reason": f"선행 의존 노드 {root_nid} 실패",
                    }
                self._emit(
                    "node_skipped",
                    node=dep_id,
                    content=f"선행 의존 노드 '{root_nid}' 실패로 건너뜀",
                    meta={"failed_dependency": root_nid},
                )
                q.extend(dependents.get(dep_id, []))

        def _mark_entity_needs_rerun(source_nid: str, eid: str, reason: str) -> list[str]:
            marked_nodes: list[str] = []
            with self._lock:
                if eid in node_by_id:
                    target_node = node_by_id[eid]
                    if target_node.get("completed", False):
                        target_node["needs_rerun"] = True
                        target_node["rerun_reason"] = reason
                        target_node.setdefault("impact_history", []).insert(
                            0,
                            {
                                "source_node_id": source_nid,
                                "needs_rerun": True,
                                "reason": reason,
                                "decision": "AUTO_RERUN" if self.auto_rerun else "PENDING",
                                "created_at": str(int(time.time())),
                            },
                        )
                        target_node["impact_history"] = target_node["impact_history"][:20]
                        marked_nodes.append(eid)
                elif eid in group_by_id:
                    target_grp = group_by_id[eid]
                    if target_grp.get("completed", False):
                        target_grp["needs_rerun"] = True
                        target_grp["rerun_reason"] = reason
                        for cgid in _expand_groups(self.graph, eid):
                            cg = group_by_id.get(cgid)
                            if cg and cg.get("completed", False):
                                cg["needs_rerun"] = True
                                cg["rerun_reason"] = reason
                        for dnid in expand_nodes(self.graph, eid):
                            dn = node_by_id.get(dnid)
                            if dn and dn.get("completed", False):
                                dn["needs_rerun"] = True
                                dn["rerun_reason"] = reason
                                marked_nodes.append(dnid)
            if marked_nodes or (eid in group_by_id and group_by_id[eid].get("needs_rerun")):
                self._emit(
                    "needs_rerun_marked",
                    node=source_nid,
                    target=eid,
                    content=reason,
                    meta={"entity_id": eid, "reason": reason, "marked_nodes": marked_nodes},
                )
            return marked_nodes

        def _enqueue_auto_rerun(marked_nids: list[str], triggered_by: str) -> None:
            added: list[str] = []
            for dnid in marked_nids:
                if dnid in run_set or dnid not in node_by_id:
                    continue
                run_set.add(dnid)
                run_nodes.append(dnid)
                auto_queued.add(dnid)
                added.append(dnid)
                for gid, members in group_all_members.items():
                    if dnid in members:
                        group_run_members.setdefault(gid, set()).add(dnid)
                        finished_groups.discard(gid)
            if not added:
                return
            for x in run_nodes:
                eff_deps[x] = all_eff_deps[x] & run_set
            for nid_r in run_nodes:
                dependents[nid_r] = []
            for nid_r, deps_r in eff_deps.items():
                for d in deps_r:
                    dependents[d].append(nid_r)
            for dnid in added:
                bad_deps = all_eff_deps[dnid] & (failed | skipped)
                if bad_deps:
                    failed_dep = next(iter(sorted(bad_deps)))
                    skipped.add(dnid)
                    with self._lock:
                        self.node_state[dnid] = {
                            "status": "skipped",
                            "model": node_by_id[dnid].get("model") or "",
                            "reason": f"선행 의존 노드 {failed_dep} 실패",
                        }
                    self._emit(
                        "node_skipped",
                        node=dnid,
                        content=f"선행 의존 노드 '{failed_dep}' 실패로 건너뜀",
                        meta={"failed_dependency": failed_dep},
                    )
                    continue
                reason = node_by_id[dnid].get("rerun_reason") or ""
                with self._lock:
                    self.node_state[dnid] = {
                        "status": "pending",
                        "model": node_by_id[dnid].get("model") or "",
                        "auto_rerun": True,
                    }
                self._emit(
                    "auto_rerun_queued",
                    node=dnid,
                    target=triggered_by,
                    content=f"'{node_by_id[dnid]['label']}' 자동 재실행 대기열에 추가됨",
                    meta={"triggered_by": triggered_by, "reason": reason},
                )

        async def _run_one(nid: str) -> dict[str, Any]:
            async with sem:
                started_at = time.monotonic()
                n = node_by_id[nid]
                iter_no = node_iterations.get(nid, 1)
                prev_output = str(n.get("output") or "")
                with self._lock:
                    self.node_state[nid] = {
                        "status": "running",
                        "model": n.get("model") or self.graph["settings"].get("default_model") or "",
                        "iteration": iter_no,
                    }
                _mark_group_started(nid)
                self._emit(
                    "node_started",
                    node=nid,
                    content=f"노드 '{n['label']}' 실행 시작" + (f" (반복 #{iter_no})" if iter_no > 1 else ""),
                    meta={"model": self.node_state[nid]["model"], "iteration": iter_no},
                )

                # Inject any non-loop edge temporary prompt targeted at this node or its ancestor groups
                snap_in = self.graph_snapshot()
                for e in snap_in.get("edges") or []:
                    if e.get("is_loop"):
                        continue
                    raw_etmp = str(e.get("temp_prompt") or "")
                    if not raw_etmp.strip():
                        continue
                    try:
                        downstream = set(expand_nodes(snap_in, e["from"]))
                        upstream = expand_nodes(snap_in, e["to"])
                    except PromptGraphError:
                        continue
                    if nid not in downstream:
                        continue
                    wave = min(node_iterations.get(m, 1) for m in downstream) if downstream else iter_no
                    inj_key = (e["id"], wave)
                    should_inject = False
                    with self._lock:
                        if inj_key not in injected_forward_edges:
                            injected_forward_edges.add(inj_key)
                            should_inject = True
                    if not should_inject:
                        continue
                    src_id = upstream[-1] if upstream else e["to"]
                    src_ent = node_by_id.get(src_id) or group_by_id.get(src_id) or {}
                    src_out = str(src_ent.get("output") or "") if src_id in node_by_id else ""
                    c_err = (node_contracts.get(src_id) or {}).get("errors") or []
                    rendered_etmp = _render_edge_temp_prompt(
                        raw_etmp,
                        iteration=wave,
                        max_iterations=int(e.get("max_iterations") or 0),
                        source_id=src_id,
                        source_label=str(src_ent.get("label") or src_id),
                        source_output=src_out,
                        contract_errors=c_err,
                    )
                    if rendered_etmp:
                        _inject_temp_prompt_to_entity(
                            e["from"],
                            rendered_etmp,
                            e["id"],
                            src_id,
                            is_loop_edge=False,
                            iteration=wave,
                        )

                artifact_context = ""
                artifact_hash = ""
                art = n.get("artifact") or {}
                if self.artifact_reader is not None and art.get("kind", "none") != "none" and art.get("mode", "read") in ("read", "sync"):
                    try:
                        loaded_art = self.artifact_reader(deepcopy(n), self.graph_snapshot())
                        if loaded_art:
                            artifact_context, artifact_hash = str(loaded_art[0] or ""), str(loaded_art[1] or "")
                            with self._lock:
                                n.setdefault("artifact", {})["last_content_hash"] = artifact_hash
                    except Exception as art_exc:
                        self._emit(
                            "artifact_synced",
                            node=nid,
                            content=f"산출물 읽기 실패: {str(art_exc)[:200]}",
                            meta={"kind": art.get("kind"), "ok": False},
                        )

                snap_for_hash = self.graph_snapshot()
                input_hash = compute_node_input_hash(snap_for_hash, nid, artifact_hash=artifact_hash)
                use_cache = bool((snap_for_hash.get("settings") or {}).get("cross_graph_cache", False))
                pending_fb = list(loop_feedback.get(nid) or [])
                used_temp_prompt = str(n.get("temp_prompt") or "")

                if use_cache and self.cache_lookup is not None and not self.force_rerun and not pending_fb:
                    cached = self.cache_lookup(input_hash)
                    if cached and cached.get("output") is not None:
                        output_str = _s(cached.get("output"), MAX_OUTPUT_CHARS)
                        with self._lock:
                            cleared_temp = bool(str(n.get("temp_prompt") or "").strip())
                            n["output"] = output_str
                            n["completed"] = True
                            n["last_run_prompt"] = n.get("prompt") or ""
                            n["temp_prompt"] = ""
                            n["last_run_temp_prompt"] = ""
                            n["last_run_pre_prompts"] = {
                                agrp["id"]: {
                                    "pre": agrp.get("pre_prompt") or "",
                                    "temp": agrp.get("temp_pre_prompt") or "",
                                }
                                for agrp in ancestor_groups(self.graph, nid)
                            }
                            n["needs_rerun"] = False
                            n["rerun_reason"] = ""
                        self._emit(
                            "node_cache_hit",
                            node=nid,
                            content=f"동일 입력 해시({input_hash[:10]}…) 크로스-그래프 캐시 적중",
                            meta={"input_hash": input_hash, "model": cached.get("model") or "", "cache_hit": True},
                        )
                        contract_res = evaluate_output_contract(output_str, n.get("contract"))
                        node_contracts[nid] = contract_res
                        node_agent_decisions[nid] = {}
                        return {
                            "output": output_str,
                            "model": cached.get("model") or n.get("model") or "",
                            "mcp_calls": [],
                            "usage": cached.get("usage") or {},
                            "ms": 0,
                            "cost_usd": 0.0,
                            "cache_hit": True,
                            "input_hash": input_hash,
                            "used_temp_prompt": used_temp_prompt,
                            "cleared_temp": cleared_temp,
                            "marked_rerun_nodes": [],
                            "contract_passed": bool(contract_res["passed"]),
                            "contract_errors": list(contract_res["errors"]),
                            "contract_result": contract_res,
                            "agent_loop_decisions": {},
                            "iteration": iter_no,
                        }

                def _on_mcp(ev: dict[str, Any]) -> None:
                    args = ev.get("arguments") or {}
                    tgt = str(args.get("node_id") or args.get("group_id") or args.get("entity_id") or "")
                    self._emit(
                        "mcp_tool_call",
                        node=nid,
                        target=tgt,
                        content=ev.get("result_preview") or "",
                        meta={"tool": ev.get("tool"), "arguments": args},
                    )

                def _on_retry(ev: dict[str, Any]) -> None:
                    self._emit(
                        "contract_retry",
                        node=nid,
                        content=f"출력 계약 재시도 #{ev.get('attempt')}: {', '.join(ev.get('errors') or [])}",
                        meta={"attempt": ev.get("attempt"), "errors": ev.get("errors") or []},
                    )

                res = await execute_prompt_node_with_mcp(
                    self.graph_snapshot,
                    nid,
                    self.chat_fn,
                    state_accessor=self.snapshot,
                    on_mcp_call=_on_mcp,
                    loop_feedback=pending_fb,
                    artifact_context=artifact_context,
                    on_contract_retry=_on_retry,
                )

                output_str = _s(res.get("output"), MAX_OUTPUT_CHARS)
                contract_res = res.get("contract_result") or {"passed": bool(res.get("contract_passed", True)), "errors": []}
                agent_decs = res.get("agent_loop_decisions") or {}
                node_contracts[nid] = contract_res
                node_agent_decisions[nid] = agent_decs
                if not contract_res.get("passed", True):
                    self._emit(
                        "contract_failed",
                        node=nid,
                        content="출력 계약 미충족: " + "; ".join(contract_res.get("errors") or []),
                        meta={"errors": contract_res.get("errors") or []},
                    )

                with self._lock:
                    cleared_temp = bool(str(n.get("temp_prompt") or "").strip())
                    n["output"] = output_str
                    n["completed"] = True
                    n["last_run_prompt"] = n.get("prompt") or ""
                    n["temp_prompt"] = ""
                    n["last_run_temp_prompt"] = ""
                    n["last_run_pre_prompts"] = {
                        agrp["id"]: {
                            "pre": agrp.get("pre_prompt") or "",
                            "temp": agrp.get("temp_pre_prompt") or "",
                        }
                        for agrp in ancestor_groups(self.graph, nid)
                    }
                    n["needs_rerun"] = False
                    n["rerun_reason"] = ""

                loop_expected = _will_trigger_loop(nid, output_str, contract_res, agent_decs)
                # Phase 2: Agent MCP impact review on clean completed dependents (skip mid-loop)
                marked_rerun_nodes: list[str] = []
                snap = self.graph_snapshot()
                dep_scope = dependent_scope(snap, nid)
                chg_now = compute_change_status(snap)
                has_clean_dep = any(
                    dnid not in run_set and chg_now["nodes"].get(dnid, {}).get("state") == "clean"
                    for dnid in dep_scope["allowed_node_ids"]
                ) or any(
                    chg_now["groups"].get(dgid, {}).get("state") == "clean"
                    for dgid in dep_scope["allowed_group_ids"]
                )
                if has_clean_dep and not loop_expected and not self._is_cancelled():
                    def _on_mark(eid: str, reason: str) -> None:
                        for mnid in _mark_entity_needs_rerun(nid, eid, reason):
                            if mnid not in marked_rerun_nodes:
                                marked_rerun_nodes.append(mnid)

                    def _on_record(eid: str, needs_rerun_flag: bool, reason: str) -> None:
                        rec = {
                            "source_node_id": nid,
                            "target_id": eid,
                            "needs_rerun": bool(needs_rerun_flag),
                            "reason": reason,
                            "decision": ("AUTO_RERUN" if self.auto_rerun else "PENDING") if needs_rerun_flag else "DISMISSED",
                        }
                        if not needs_rerun_flag and eid in node_by_id:
                            with self._lock:
                                hist = node_by_id[eid].setdefault("impact_history", [])
                                hist.insert(
                                    0,
                                    {
                                        "source_node_id": nid,
                                        "needs_rerun": False,
                                        "reason": reason,
                                        "decision": "DISMISSED",
                                        "created_at": str(int(time.time())),
                                    },
                                )
                                node_by_id[eid]["impact_history"] = hist[:20]
                        self._emit(
                            "impact_reviewed",
                            node=nid,
                            target=eid,
                            content=reason,
                            meta={"needs_rerun": bool(needs_rerun_flag), "reason": reason},
                        )
                        if self.on_impact_review is not None:
                            self.on_impact_review(rec)

                    imp = await evaluate_dependents_with_mcp(
                        self.graph_snapshot,
                        nid,
                        dep_scope,
                        self.chat_fn,
                        prev_output=prev_output,
                        new_output=output_str,
                        state_accessor=self.snapshot,
                        on_mark_needs_rerun=_on_mark,
                        on_record_impact=_on_record,
                        on_mcp_call=_on_mcp,
                    )
                    res["mcp_calls"] = (res.get("mcp_calls") or []) + (imp.get("mcp_calls") or [])
                    imp_usage = imp.get("usage") or {}
                    cur_usage = res.get("usage") or {}
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        if isinstance(imp_usage.get(k), int):
                            cur_usage[k] = cur_usage.get(k, 0) + imp_usage[k]
                    res["usage"] = cur_usage

                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                usage_final = res.get("usage") or {}
                cost_usd = estimate_cost_usd(
                    res.get("model") or "",
                    int(usage_final.get("prompt_tokens") or 0),
                    int(usage_final.get("completion_tokens") or 0),
                )
                res["ms"] = elapsed_ms
                res["cost_usd"] = cost_usd
                res["cache_hit"] = False
                res["input_hash"] = input_hash
                res["used_temp_prompt"] = used_temp_prompt
                res["output"] = output_str
                res["cleared_temp"] = cleared_temp
                res["marked_rerun_nodes"] = marked_rerun_nodes
                res["iteration"] = iter_no
                return res

        def _launch_ready() -> None:
            if self._is_cancelled():
                return
            changed = True
            while changed:
                changed = False
                for nid in list(run_nodes):
                    if (
                        nid in completed
                        or nid in failed
                        or nid in skipped
                        or nid in in_flight.values()
                    ):
                        continue
                    if eff_deps[nid] <= completed:
                        cond_ok, cond_reason = _check_incoming_conditions(nid)
                        if not cond_ok:
                            skipped.add(nid)
                            with self._lock:
                                self.node_state[nid] = {
                                    "status": "skipped",
                                    "model": node_by_id[nid].get("model") or "",
                                    "reason": cond_reason,
                                }
                            self._emit(
                                "node_skipped",
                                node=nid,
                                content=f"조건부 간선 미충족으로 건너뜀: {cond_reason}",
                                meta={"condition_reason": cond_reason},
                            )
                            _cascade_skip(nid)
                            changed = True
                            continue
                        task = asyncio.create_task(_run_one(nid))
                        in_flight[task] = nid

        _launch_ready()

        while in_flight:
            done_tasks, _ = await asyncio.wait(
                in_flight.keys(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done_tasks:
                nid = in_flight.pop(task)
                try:
                    res = task.result()
                except Exception as exc:
                    failed.add(nid)
                    err_msg = str(exc)[:2000]
                    with self._lock:
                        self.node_state[nid] = {
                            "status": "failed",
                            "model": node_by_id[nid].get("model") or "",
                            "error": err_msg,
                        }
                    self._emit("node_failed", node=nid, content=err_msg, meta={"error": err_msg})
                    _cascade_skip(nid)
                else:
                    completed.add(nid)
                    output_str = _s(res.get("output"), MAX_OUTPUT_CHARS)
                    cleared_temp = bool(res.get("cleared_temp", False))
                    marked_reruns = list(res.get("marked_rerun_nodes") or [])
                    contract_passed = bool(res.get("contract_passed", True))
                    contract_errors = list(res.get("contract_errors") or [])
                    cache_hit = bool(res.get("cache_hit", False))
                    cost_usd = float(res.get("cost_usd") or 0.0)
                    iter_no = int(res.get("iteration") or node_iterations.get(nid, 1))
                    with self._lock:
                        self.node_state[nid] = {
                            "status": "done",
                            "model": res.get("model") or "",
                            "ms": res.get("ms", 0),
                            "mcp_calls": len(res.get("mcp_calls") or []),
                            "usage": res.get("usage") or {},
                            "cost_usd": cost_usd,
                            "cache_hit": cache_hit,
                            "input_hash": res.get("input_hash") or "",
                            "contract_passed": contract_passed,
                            "contract_errors": contract_errors,
                            "iteration": iter_no,
                            "completed": True,
                            "cleared_temp_prompt": cleared_temp,
                            "marked_needs_rerun": marked_reruns,
                        }
                    if self.on_node_version is not None:
                        pre_joined = "\n\n".join(
                            f"[{p['label']}]\n{p.get('pre_prompt') or ''}"
                            for p in effective_pre_prompts(self.graph_snapshot(), nid)
                            if (p.get("pre_prompt") or "").strip()
                        )
                        try:
                            self.on_node_version(
                                {
                                    "node_id": nid,
                                    "node_label": node_by_id[nid].get("label") or nid,
                                    "iteration": iter_no,
                                    "input_hash": res.get("input_hash") or compute_node_input_hash(self.graph_snapshot(), nid),
                                    "prompt": node_by_id[nid].get("prompt") or "",
                                    "temp_prompt": res.get("used_temp_prompt") or "",
                                    "effective_pre_prompt": pre_joined,
                                    "output": output_str,
                                    "model": res.get("model") or "",
                                    "status": "done",
                                    "contract_passed": contract_passed,
                                    "contract_errors": contract_errors,
                                    "usage": res.get("usage") or {},
                                    "cost_usd": cost_usd,
                                    "ms": int(res.get("ms") or 0),
                                    "cache_hit": cache_hit,
                                }
                            )
                        except Exception:
                            pass
                    self._emit(
                        "node_finished",
                        node=nid,
                        content=output_str,
                        meta={
                            "model": res.get("model") or "",
                            "ms": res.get("ms", 0),
                            "cost_usd": cost_usd,
                            "cache_hit": cache_hit,
                            "contract_passed": contract_passed,
                            "iteration": iter_no,
                            "mcp_calls": len(res.get("mcp_calls") or []),
                            "completed": True,
                            "cleared_temp_prompt": cleared_temp,
                            "marked_needs_rerun": marked_reruns,
                        },
                    )

                    # Evaluate conditional loop / recursion edges triggered by `nid`
                    loop_requeued = False
                    if not self._is_cancelled():
                        snap_loop = self.graph_snapshot()
                        for e in snap_loop.get("edges") or []:
                            if not e.get("is_loop"):
                                continue
                            try:
                                triggers = set(expand_nodes(snap_loop, e["to"]))
                            except PromptGraphError:
                                continue
                            if nid not in triggers or not (triggers <= completed):
                                continue
                            cond_ok = evaluate_edge_condition(
                                e,
                                output_str,
                                res.get("contract_result") or node_contracts.get(nid),
                                res.get("agent_loop_decisions") or node_agent_decisions.get(nid),
                            )
                            if not cond_ok:
                                continue
                            eid = e["id"]
                            cur_cnt = loop_counts.get(eid, 0)
                            max_it = int(e.get("max_iterations") or 3)
                            if cur_cnt < max_it:
                                loop_counts[eid] = cur_cnt + 1
                                loop_body = _compute_loop_body(e["from"], e["to"])
                                fb_entry = {
                                    "from_node": nid,
                                    "edge_id": eid,
                                    "iteration": loop_counts[eid],
                                    "trigger_output": output_str,
                                    "contract_errors": contract_errors,
                                }
                                for body_nid in sorted(loop_body):
                                    completed.discard(body_nid)
                                    failed.discard(body_nid)
                                    skipped.discard(body_nid)
                                    if body_nid not in run_set:
                                        run_set.add(body_nid)
                                        run_nodes.append(body_nid)
                                    node_iterations[body_nid] = node_iterations.get(body_nid, 1) + 1
                                    for gid, members in group_all_members.items():
                                        if body_nid in members:
                                            group_run_members.setdefault(gid, set()).add(body_nid)
                                            finished_groups.discard(gid)
                                    with self._lock:
                                        self.node_state[body_nid] = {
                                            "status": "pending",
                                            "model": node_by_id[body_nid].get("model") or "",
                                            "iteration": node_iterations[body_nid],
                                            "loop_from": nid,
                                        }
                                for start_nid in sorted(expand_nodes(snap_loop, e["from"])):
                                    loop_feedback.setdefault(start_nid, []).append(fb_entry)

                                decs_map = res.get("agent_loop_decisions") or node_agent_decisions.get(nid) or {}
                                dyn_tmp = str(((decs_map.get(eid) or decs_map.get("*")) or {}).get("temp_prompt") or "").strip()
                                static_tmp = _render_edge_temp_prompt(
                                    e.get("temp_prompt") or "",
                                    iteration=loop_counts[eid],
                                    max_iterations=max_it,
                                    source_id=nid,
                                    source_label=str(node_by_id[nid].get("label") or nid),
                                    source_output=output_str,
                                    contract_errors=contract_errors,
                                )
                                injected_text = "\n\n".join(p for p in (static_tmp, dyn_tmp) if p)
                                if injected_text:
                                    _inject_temp_prompt_to_entity(
                                        e["from"],
                                        injected_text,
                                        eid,
                                        nid,
                                        is_loop_edge=True,
                                        iteration=loop_counts[eid],
                                    )

                                for x in run_nodes:
                                    eff_deps[x] = all_eff_deps[x] & run_set
                                for nid_r in run_nodes:
                                    dependents[nid_r] = []
                                for nid_r, deps_r in eff_deps.items():
                                    for d in deps_r:
                                        dependents[d].append(nid_r)
                                self._emit(
                                    "loop_triggered",
                                    node=nid,
                                    target=e["from"],
                                    content=f"조건부 순환 #{loop_counts[eid]}/{max_it} 실행 ({nid} → {e['from']})",
                                    meta={
                                        "edge_id": eid,
                                        "iteration": loop_counts[eid],
                                        "max_iterations": max_it,
                                        "loop_body": sorted(loop_body),
                                        "injected_temp_prompt": injected_text,
                                    },
                                )
                                loop_requeued = True
                            else:
                                self._emit(
                                    "loop_limit_reached",
                                    node=nid,
                                    target=e["from"],
                                    content=f"순환 최대 반복 횟수({max_it}회) 도달로 루프 종료",
                                    meta={"edge_id": eid, "max_iterations": max_it},
                                )

                    if (
                        not loop_requeued
                        and contract_passed
                        and self.artifact_writer is not None
                        and (node_by_id[nid].get("artifact") or {}).get("kind") == "repo_file"
                        and (node_by_id[nid].get("artifact") or {}).get("mode") in ("write", "sync")
                    ):
                        try:
                            new_hash = self.artifact_writer(deepcopy(node_by_id[nid]), output_str, self.graph_snapshot())
                            with self._lock:
                                art_obj = node_by_id[nid].setdefault("artifact", {})
                                if new_hash:
                                    art_obj["last_content_hash"] = str(new_hash)
                                art_obj["last_synced_at"] = str(int(time.time()))
                            self._emit(
                                "artifact_synced",
                                node=nid,
                                content=f"산출물 기록 완료: {art_obj.get('path')}",
                                meta={"kind": "repo_file", "path": art_obj.get("path"), "ok": True},
                            )
                        except Exception as art_exc:
                            self._emit(
                                "artifact_synced",
                                node=nid,
                                content=f"산출물 기록 실패: {str(art_exc)[:200]}",
                                meta={"kind": "repo_file", "ok": False},
                            )

                    if not loop_requeued and self.auto_rerun and marked_reruns and not self._is_cancelled():
                        _enqueue_auto_rerun(marked_reruns, nid)
            _check_groups_finished()
            if self._is_cancelled():
                for t in list(in_flight.keys()):
                    t.cancel()
                if in_flight:
                    await asyncio.gather(*in_flight.keys(), return_exceptions=True)
                    in_flight.clear()
                break
            _launch_ready()

        if self._is_cancelled():
            for nid in run_nodes:
                if nid not in completed and nid not in failed and nid not in skipped:
                    skipped.add(nid)
                    with self._lock:
                        self.node_state[nid] = {"status": "skipped", "reason": "사용자 중지"}
            status = "CANCELLED"
        elif failed and completed:
            status = "PARTIAL"
        elif failed:
            status = "FAILED"
        else:
            status = "SUCCEEDED"

        profile = compute_run_profile(self.graph_snapshot(), self.snapshot())
        self._emit(
            "run_finished",
            node=self.plan["target_id"],
            content=f"실행 종료: {status} (완료 {len(completed)}, 실패 {len(failed)}, 건너뜀 {len(skipped)})",
            meta={
                "status": status,
                "completed": len(completed),
                "failed": len(failed),
                "skipped": len(skipped),
                "profile": profile,
            },
        )
        return {
            "status": status,
            "graph": self.graph_snapshot(),
            "node_state": self.snapshot(),
            "result": {
                "outputs": {n["id"]: n["output"] for n in self.graph["nodes"]},
                "completed": sorted(completed),
                "failed": sorted(failed),
                "skipped": sorted(skipped),
                "auto_rerun": sorted(auto_queued),
                "loop_counts": dict(loop_counts),
                "profile": profile,
            },
        }


# ------------------------------------------------------------------ Presets
PRESETS: dict[str, dict[str, Any]] = {
    "basic_dependency": {
        "nodes": [
            {
                "id": "research",
                "label": "핵심 자료 조사",
                "prompt": "주어진 연구 주제의 배경, 핵심 개념, 주요 쟁점 3가지를 구조화해 정리하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": [],
                "extra": {"domain": "research", "format": "bullet-list"},
                "completed": False,
                "parent_group_id": None,
                "x": 520,
                "y": 140,
                "width": 250,
                "height": 145,
            },
            {
                "id": "summary",
                "label": "최종 브리핑 작성",
                "prompt": "의존 노드의 조사 결과를 MCP 도구로 조회한 뒤, 경영진/연구자용 5줄 핵심 브리핑을 작성하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": ["research"],
                "extra": {"audience": "researchers"},
                "completed": False,
                "parent_group_id": "grp_style",
                "x": 110,
                "y": 165,
                "width": 250,
                "height": 145,
            },
        ],
        "groups": [
            {
                "id": "grp_style",
                "label": "간결한 한국어 스타일 프리 프롬프트",
                "pre_prompt": "항상 한국어로 답변하고, 불확실한 내용은 추정하지 말고 근거가 있는 항목만 간결하게 정리하세요.",
                "temp_pre_prompt": "",
                "node_ids": ["summary"],
                "child_group_ids": [],
                "parent_group_id": None,
                "dependencies": [],
                "completed": False,
                "x": 75,
                "y": 95,
                "width": 320,
                "height": 250,
            }
        ],
        "edges": [{"id": "e1", "from": "summary", "to": "research"}],
        "settings": {"parallelism": 4, "max_tool_steps": 8, "default_model": ""},
    },
    "nested_groups": {
        "nodes": [
            {
                "id": "b_market",
                "label": "시장·수요 분석",
                "prompt": "목표 기술 분야의 최근 산업 수요와 주요 활용 사례 3가지를 분석하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": [],
                "extra": {"track": "market"},
                "completed": False,
                "parent_group_id": "grp_b_parallel",
                "x": 620,
                "y": 150,
                "width": 240,
                "height": 135,
            },
            {
                "id": "b_tech",
                "label": "기술 타당성 분석",
                "prompt": "목표 기술의 구현 난이도와 핵심 리스크 3가지를 분석하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": [],
                "extra": {"track": "engineering"},
                "completed": False,
                "parent_group_id": "grp_b_parallel",
                "x": 620,
                "y": 310,
                "width": 240,
                "height": 135,
            },
            {
                "id": "a_design",
                "label": "아키텍처 제안",
                "prompt": "상위 그룹이 의존하는 grp_phase_b의 모든 하위 그룹/노드 정보를 MCP(get_group_info)로 조회해 통합 아키텍처를 설계하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": [],
                "extra": {"stage": "architecture"},
                "completed": False,
                "parent_group_id": "grp_a_synth",
                "x": 130,
                "y": 165,
                "width": 250,
                "height": 140,
            },
            {
                "id": "a_review",
                "label": "최종 실행 로드맵",
                "prompt": "a_design의 설계안과 grp_phase_b의 분석 결과를 MCP로 함께 조회해 3단계 실행 계획을 작성하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": ["a_design"],
                "extra": {"stage": "roadmap"},
                "completed": False,
                "parent_group_id": "grp_a_synth",
                "x": 130,
                "y": 335,
                "width": 250,
                "height": 140,
            },
        ],
        "groups": [
            {
                "id": "grp_phase_b",
                "label": "B 그룹 · 기초 분석 (선행)",
                "pre_prompt": "각 분석은 객관적 지표와 리스크를 반드시 항목화해 작성하세요.",
                "temp_pre_prompt": "",
                "node_ids": [],
                "child_group_ids": ["grp_b_parallel"],
                "parent_group_id": None,
                "dependencies": [],
                "completed": False,
                "x": 560,
                "y": 60,
                "width": 360,
                "height": 440,
            },
            {
                "id": "grp_b_parallel",
                "label": "B-1 · 병렬 리서치 하위 그룹",
                "pre_prompt": "핵심 요점 3개 이내로 압축해 출력하세요.",
                "temp_pre_prompt": "",
                "node_ids": ["b_market", "b_tech"],
                "child_group_ids": [],
                "parent_group_id": "grp_phase_b",
                "dependencies": [],
                "completed": False,
                "x": 590,
                "y": 105,
                "width": 300,
                "height": 365,
            },
            {
                "id": "grp_phase_a",
                "label": "A 그룹 · 종합 설계 (B 의존)",
                "pre_prompt": "선행 그룹 B의 모든 프리 프롬프트와 내부 노드 결과를 MCP로 조회해 일관성 있게 종합하세요.",
                "temp_pre_prompt": "",
                "node_ids": [],
                "child_group_ids": ["grp_a_synth"],
                "parent_group_id": None,
                "dependencies": ["grp_phase_b"],
                "completed": False,
                "x": 70,
                "y": 60,
                "width": 370,
                "height": 470,
            },
            {
                "id": "grp_a_synth",
                "label": "A-1 · 설계/로드맵 하위 그룹",
                "pre_prompt": "실행 가능한 단계별 산출물을 명시하세요.",
                "temp_pre_prompt": "",
                "node_ids": ["a_design", "a_review"],
                "child_group_ids": [],
                "parent_group_id": "grp_phase_a",
                "dependencies": [],
                "completed": False,
                "x": 100,
                "y": 115,
                "width": 310,
                "height": 385,
            },
        ],
        "edges": [
            {"id": "e1", "from": "grp_phase_a", "to": "grp_phase_b"},
            {"id": "e2", "from": "a_review", "to": "a_design"},
        ],
        "settings": {"parallelism": 4, "max_tool_steps": 10, "default_model": ""},
    },
    "deep_nested_groups": {
        "nodes": [
            {
                "id": "n_leaf",
                "label": "세부 실행 프롬프트",
                "prompt": "프로젝트 목표에 대한 구체적 아키텍처와 검증 체크리스트를 작성하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": [],
                "extra": {},
                "completed": False,
                "parent_group_id": "g_tier1",
                "x": 140,
                "y": 310,
                "width": 250,
                "height": 145,
            }
        ],
        "groups": [
            {
                "id": "g_tier3",
                "label": "3-그룹 (최상위 정책)",
                "pre_prompt": "전역 품질 기준과 보안 원칙을 준수하세요.",
                "temp_pre_prompt": "",
                "node_ids": [],
                "child_group_ids": ["g_tier2"],
                "parent_group_id": None,
                "dependencies": [],
                "completed": False,
                "x": 60,
                "y": 60,
                "width": 410,
                "height": 455,
            },
            {
                "id": "g_tier2",
                "label": "2-그룹 (도메인 컨텍스트)",
                "pre_prompt": "도메인 용어와 시스템 경계를 명확히 구분해 서술하세요.",
                "temp_pre_prompt": "",
                "node_ids": [],
                "child_group_ids": ["g_tier1"],
                "parent_group_id": "g_tier3",
                "dependencies": [],
                "completed": False,
                "x": 85,
                "y": 150,
                "width": 360,
                "height": 340,
            },
            {
                "id": "g_tier1",
                "label": "1-그룹 (출력 형식)",
                "pre_prompt": "마크다운 표와 번호 목록으로 정리하세요.",
                "temp_pre_prompt": "",
                "node_ids": ["n_leaf"],
                "child_group_ids": [],
                "parent_group_id": "g_tier2",
                "dependencies": [],
                "completed": False,
                "x": 110,
                "y": 235,
                "width": 310,
                "height": 235,
            },
        ],
        "edges": [],
        "settings": {"parallelism": 4, "max_tool_steps": 10, "default_model": ""},
    },
    "conditional_loop_review": {
        "nodes": [
            {
                "id": "draft",
                "label": "초안 생성",
                "prompt": "핵심 아키텍처 제안서 초안을 작성하세요. 피드백이 있다면 반드시 모두 보완해 JSON이 아닌 깔끔한 본문으로 제출하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": [],
                "extra": {},
                "completed": False,
                "parent_group_id": None,
                "x": 90,
                "y": 150,
                "width": 240,
                "height": 135,
            },
            {
                "id": "critic",
                "label": "품질 검증 게이트",
                "prompt": "선행 초안 노드를 MCP로 조회하고, 보안·성능·검증 항목이 모두 충분하면 첫 줄에 PASS, 보완이 필요하면 REVISE와 구체적 사유를 적으세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": ["draft"],
                "extra": {},
                "contract": {
                    "enabled": True,
                    "json_required": False,
                    "json_keys": [],
                    "must_include": ["PASS"],
                    "must_exclude": [],
                    "regex": "",
                    "min_chars": 4,
                    "max_retries": 0,
                    "on_fail": "continue",
                },
                "completed": False,
                "parent_group_id": None,
                "x": 430,
                "y": 150,
                "width": 240,
                "height": 135,
            },
            {
                "id": "publish",
                "label": "최종 승인 배포본",
                "prompt": "검증 게이트와 초안을 MCP로 조회하여 최종 승인 문서를 작성하세요.",
                "temp_prompt": "",
                "output": "",
                "model": "",
                "dependencies": ["critic"],
                "extra": {},
                "completed": False,
                "parent_group_id": None,
                "x": 770,
                "y": 150,
                "width": 240,
                "height": 135,
            },
        ],
        "groups": [],
        "edges": [
            {"id": "e_dc", "from": "critic", "to": "draft"},
            {
                "id": "e_loop_cd",
                "from": "draft",
                "to": "critic",
                "is_loop": True,
                "max_iterations": 3,
                "condition": {"type": "on_contract_fail", "value": "", "case_sensitive": False},
                "label": "검증 실패 시 재작성 루프",
                "temp_prompt": "이전 검증({source_label}, {iteration}/{max_iterations}회차)에서 계약 오류[{contract_errors}]로 반려되었습니다.\n검토 의견:\n{source_output}\n위 지적 사항을 모두 반영해 초안을 수정하세요.",
            },
            {
                "id": "e_cp",
                "from": "publish",
                "to": "critic",
                "is_loop": False,
                "max_iterations": 0,
                "condition": {"type": "on_contract_pass", "value": "", "case_sensitive": False},
                "label": "PASS 시 최종본 생성",
            },
        ],
        "settings": {
            "parallelism": 4,
            "max_tool_steps": 8,
            "default_model": "",
            "edge_direction": "execution",
        },
    },
}

PRESET_INFO: dict[str, dict[str, str]] = {
    "basic_dependency": {
        "title": "기본 의존성 + 프리 프롬프트 그룹",
        "desc": "summary → research 의존 관계와 점선 프리 프롬프트 그룹 기본 예제",
    },
    "nested_groups": {
        "title": "다중 중첩 그룹 간 의존성 (그룹 속 그룹)",
        "desc": "A 그룹(내부 하위그룹 포함) → B 그룹(병렬 노드 포함 하위그룹) 의존 실행 및 MCP 계층 조회",
    },
    "deep_nested_groups": {
        "title": "3계층 프리 프롬프트 그룹 (3-그룹 → 2-그룹 → 1-그룹)",
        "desc": "1-그룹·2-그룹·3-그룹으로 계속 확장되는 다층 중첩 프리 프롬프트 예제",
    },
    "conditional_loop_review": {
        "title": "조건부 순환·분기 (초안 ⇄ 검증 게이트 → 최종본)",
        "desc": "출력 계약 실패 시 최대 3회 자동 재귀 개선 루프를 돌고 PASS 시 최종본을 생성하는 예제 (실행 방향 표시)",
    },
}


def preset(name: str) -> dict[str, Any]:
    if name not in PRESETS:
        raise PromptGraphError(f"unknown preset {name!r}")
    return validate_prompt_graph(deepcopy(PRESETS[name]))
