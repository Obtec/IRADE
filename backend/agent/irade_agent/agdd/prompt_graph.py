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
import inspect
import json
import os
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

    # Normalize edges and deduplicate (from, to)
    edges: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    used_edge_ids: set[str] = set()

    for idx, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise PromptGraphError("edge must be an object")
        frm = str(raw.get("from") or "").strip()
        to = str(raw.get("to") or "").strip()
        if frm not in used_ids:
            raise PromptGraphError(f"edge from unknown entity {frm!r}")
        if to not in used_ids:
            raise PromptGraphError(f"edge to unknown entity {to!r}")
        if frm == to:
            raise PromptGraphError(f"self dependency is not allowed on {frm!r}")
        if subtree(frm) & subtree(to):
            raise PromptGraphError(
                f"invalid dependency {frm!r} -> {to!r}: ancestor and descendant cannot depend on each other"
            )
        if (frm, to) in seen_pairs:
            continue
        seen_pairs.add((frm, to))
        eid = str(raw.get("id") or f"e{idx + 1}").strip() or f"e{idx + 1}"
        while eid in used_edge_ids:
            eid = f"{eid}_{len(used_edge_ids) + 1}"
        used_edge_ids.add(eid)
        edges.append({"id": eid, "from": frm, "to": to})

    # Immediate synchronization of node/group dependencies field from edges
    deps_map: dict[str, list[str]] = {eid: [] for eid in used_ids}
    for e in edges:
        deps_map[e["from"]].append(e["to"])
    for n in nodes:
        n["dependencies"] = deps_map[n["id"]]
    for g in groups:
        g["dependencies"] = deps_map[g["id"]]

    # Full hierarchical cycle detection:
    # Expand every edge u -> v to x -> y for all x in subtree(u), y in subtree(v).
    expanded_adj: dict[str, set[str]] = {eid: set() for eid in used_ids}
    for e in edges:
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

    # Edge connection invariant: completed entities cannot depend on unexecuted (incomplete) entities
    def _is_completed(eid: str) -> bool:
        if eid in node_by_id:
            return bool(node_by_id[eid]["completed"])
        return bool(group_by_id[eid]["completed"])

    for e in edges:
        comp_from = [x for x in sorted(subtree(e["from"])) if _is_completed(x)]
        uncomp_to = [y for y in sorted(subtree(e["to"])) if not _is_completed(y)]
        if comp_from and uncomp_to:
            raise PromptGraphError(
                f"완료된 대상({comp_from[0]})은 미실행 대상({uncomp_to[0]})에 의존하도록 연결할 수 없습니다."
            )

    default_model = _s(raw_settings.get("default_model"), 80).strip()
    if default_model and not _MODEL_RX.fullmatch(default_model):
        raise PromptGraphError(f"settings.default_model: invalid model name {default_model!r}")

    settings = {
        "parallelism": _int(raw_settings.get("parallelism", 4), 1, 8, 4),
        "max_tool_steps": _int(raw_settings.get("max_tool_steps", 8), 1, 16, 8),
        "default_model": default_model,
        "auto_rerun_needs_rerun": bool(raw_settings.get("auto_rerun_needs_rerun", False)),
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
) -> Server:
    """Create an isolated real MCP server scoped strictly to `current_node_id`'s dependency graph."""
    server = Server("irade-prompt-graph-mcp")

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
            for name, description, properties, required in MCP_TOOLS
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

        raise ValueError(f"Unknown MCP tool: {name}")

    return server


# ------------------------------------------------------------------ Node execution via MCP
def _build_node_messages(graph: dict[str, Any], node: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
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
) -> dict[str, Any]:
    """Execute one prompt node using a real MCP server bound to its dependency scope."""
    graph = graph_accessor()
    node_by_id, _ = _indexes(graph)
    if node_id not in node_by_id:
        raise PromptGraphError(f"unknown node {node_id!r}")
    node = node_by_id[node_id]
    scope = dependency_scope(graph, node_id)
    messages = _build_node_messages(graph, node, scope)

    settings = graph.get("settings") or {}
    model = node.get("model") or settings.get("default_model") or "mid"
    max_steps = _int(settings.get("max_tool_steps", 8), 1, 16, 8)
    extra = node.get("extra") or {}

    server = make_prompt_graph_mcp_server(graph_accessor, node_id, scope, state_accessor=state_accessor)
    mcp_calls: list[dict[str, Any]] = []
    actual_model = model
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

                output_text = ""
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
                    # One final turn without tools if tool steps exhausted
                    output_text = messages[-1].get("content") or "" if messages[-1].get("role") == "assistant" else ""
                    if not output_text:
                        output_text = "MCP 도구 조회 단계 상한에 도달했습니다."

            tg.cancel_scope.cancel()

    return {
        "output": output_text,
        "model": actual_model,
        "mcp_calls": mcp_calls,
        "usage": total_usage,
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
]


def make_dependent_impact_mcp_server(
    graph_accessor: Callable[[], dict[str, Any]],
    current_node_id: str,
    dep_scope: dict[str, Any],
    state_accessor: Callable[[], dict[str, dict[str, Any]]] | None = None,
    on_mark_needs_rerun: Callable[[str, str], None] | None = None,
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
                "영향이 없거나 기존 출력이 여전히 유효한 의존 대상에는 `mark_needs_rerun`을 호출하지 마세요."
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


# ------------------------------------------------------------------ Parallel DAG Scheduler
class PromptGraphRun:
    """Executes a prompt dependency graph in parallel while honoring node & group DAG dependencies."""

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
        auto_queued: set[str] = set()
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
                prev_output = str(n.get("output") or "")
                with self._lock:
                    self.node_state[nid] = {
                        "status": "running",
                        "model": n.get("model") or self.graph["settings"].get("default_model") or "",
                    }
                _mark_group_started(nid)
                self._emit(
                    "node_started",
                    node=nid,
                    content=f"노드 '{n['label']}' 실행 시작",
                    meta={"model": self.node_state[nid]["model"]},
                )

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

                res = await execute_prompt_node_with_mcp(
                    self.graph_snapshot,
                    nid,
                    self.chat_fn,
                    state_accessor=self.snapshot,
                    on_mcp_call=_on_mcp,
                )

                output_str = _s(res.get("output"), MAX_OUTPUT_CHARS)
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

                # Phase 2: Agent MCP impact review on clean completed dependents (no auto edge cascade)
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
                if has_clean_dep and not self._is_cancelled():
                    def _on_mark(eid: str, reason: str) -> None:
                        for mnid in _mark_entity_needs_rerun(nid, eid, reason):
                            if mnid not in marked_rerun_nodes:
                                marked_rerun_nodes.append(mnid)

                    imp = await evaluate_dependents_with_mcp(
                        self.graph_snapshot,
                        nid,
                        dep_scope,
                        self.chat_fn,
                        prev_output=prev_output,
                        new_output=output_str,
                        state_accessor=self.snapshot,
                        on_mark_needs_rerun=_on_mark,
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
                res["ms"] = elapsed_ms
                res["output"] = output_str
                res["cleared_temp"] = cleared_temp
                res["marked_rerun_nodes"] = marked_rerun_nodes
                return res

        def _launch_ready() -> None:
            if self._is_cancelled():
                return
            for nid in run_nodes:
                if (
                    nid in completed
                    or nid in failed
                    or nid in skipped
                    or nid in in_flight.values()
                ):
                    continue
                if eff_deps[nid] <= completed:
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
                    with self._lock:
                        self.node_state[nid] = {
                            "status": "done",
                            "model": res.get("model") or "",
                            "ms": res.get("ms", 0),
                            "mcp_calls": len(res.get("mcp_calls") or []),
                            "usage": res.get("usage") or {},
                            "completed": True,
                            "cleared_temp_prompt": cleared_temp,
                            "marked_needs_rerun": marked_reruns,
                        }
                    self._emit(
                        "node_finished",
                        node=nid,
                        content=output_str,
                        meta={
                            "model": res.get("model") or "",
                            "ms": res.get("ms", 0),
                            "mcp_calls": len(res.get("mcp_calls") or []),
                            "completed": True,
                            "cleared_temp_prompt": cleared_temp,
                            "marked_needs_rerun": marked_reruns,
                        },
                    )
                    if self.auto_rerun and marked_reruns and not self._is_cancelled():
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

        self._emit(
            "run_finished",
            node=self.plan["target_id"],
            content=f"실행 종료: {status} (완료 {len(completed)}, 실패 {len(failed)}, 건너뜀 {len(skipped)})",
            meta={
                "status": status,
                "completed": len(completed),
                "failed": len(failed),
                "skipped": len(skipped),
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
}


def preset(name: str) -> dict[str, Any]:
    if name not in PRESETS:
        raise PromptGraphError(f"unknown preset {name!r}")
    return validate_prompt_graph(deepcopy(PRESETS[name]))
