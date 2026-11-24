"""Comprehensive tests for Prompt Dependency Graph, Nested Pre-Prompt Groups, Real MCP Server, and API routes."""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import json
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    pg_mod = types.ModuleType("psycopg")
    rows_mod = types.ModuleType("psycopg.rows")
    types_mod = types.ModuleType("psycopg.types")
    json_mod = types.ModuleType("psycopg.types.json")

    class Jsonb:
        def __init__(self, obj):
            self.obj = obj

    rows_mod.dict_row = lambda *a, **k: None
    json_mod.Jsonb = Jsonb
    types_mod.json = json_mod
    pg_mod.rows = rows_mod
    pg_mod.types = types_mod
    pg_mod.connect = lambda *a, **k: None
    sys.modules["psycopg"] = pg_mod
    sys.modules["psycopg.rows"] = rows_mod
    sys.modules["psycopg.types"] = types_mod
    sys.modules["psycopg.types.json"] = json_mod

from irade_agent.agdd import api, infra, prompt_graph as pg, prompt_graph_service as pgs  # noqa: E402
from irade_agent.agdd.prompt_graph import (  # noqa: E402
    PromptGraphError,
    PromptGraphRun,
    compute_change_status,
    delete_entity_with_temp_prompts,
    dependency_scope,
    effective_pre_prompts,
    find_dependents,
    group_tier,
    plan_execution,
    validate_prompt_graph,
)


def _node(nid: str, **kw) -> dict:
    return {
        "id": nid,
        "label": kw.pop("label", nid),
        "prompt": kw.pop("prompt", f"prompt for {nid}"),
        "output": kw.pop("output", ""),
        "model": kw.pop("model", ""),
        "dependencies": kw.pop("dependencies", []),
        "extra": kw.pop("extra", {}),
        "parent_group_id": kw.pop("parent_group_id", None),
        **kw,
    }


def _group(gid: str, **kw) -> dict:
    return {
        "id": gid,
        "label": kw.pop("label", gid),
        "pre_prompt": kw.pop("pre_prompt", f"pre_prompt for {gid}"),
        "node_ids": kw.pop("node_ids", []),
        "child_group_ids": kw.pop("child_group_ids", []),
        "parent_group_id": kw.pop("parent_group_id", None),
        "dependencies": kw.pop("dependencies", []),
        **kw,
    }


def _reply(content: str = "", tool_calls: list | None = None, model: str = "fake-mid") -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }


def _tool_call(name: str, args: dict, call_id: str | None = None) -> dict:
    return {
        "id": call_id or f"call_{name}_{abs(hash((name, json.dumps(args, sort_keys=True)))) % 10000}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class TestPromptGraphValidation(unittest.TestCase):
    def test_presets_are_valid(self):
        for name in pg.PRESETS:
            clean = validate_prompt_graph(pg.preset(name))
            self.assertGreater(len(clean["nodes"]), 0)

    def test_edge_syncs_node_and_group_dependencies_immediately(self):
        graph = {
            "nodes": [_node("A"), _node("B"), _node("C", parent_group_id="G1")],
            "groups": [_group("G1"), _group("G2")],
            "edges": [
                {"from": "A", "to": "B"},
                {"from": "G1", "to": "G2"},
            ],
        }
        clean = validate_prompt_graph(graph)
        by_id = {x["id"]: x for x in clean["nodes"] + clean["groups"]}
        self.assertEqual(by_id["A"]["dependencies"], ["B"])
        self.assertEqual(by_id["B"]["dependencies"], [])
        self.assertEqual(by_id["G1"]["dependencies"], ["G2"])
        self.assertEqual(by_id["G1"]["node_ids"], ["C"])

    def test_rejects_duplicate_ids_across_nodes_and_groups(self):
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph({"nodes": [_node("dup")], "groups": [_group("dup")], "edges": []})

    def test_rejects_self_loop_and_direct_cycle(self):
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph({"nodes": [_node("A")], "groups": [], "edges": [{"from": "A", "to": "A"}]})
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [_node("A"), _node("B")],
                    "groups": [],
                    "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "A"}],
                }
            )

    def test_rejects_empty_group_cycle_and_ancestor_descendant_edges(self):
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [],
                    "groups": [_group("G1"), _group("G2")],
                    "edges": [{"from": "G1", "to": "G2"}, {"from": "G2", "to": "G1"}],
                }
            )
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [_node("N1", parent_group_id="G1")],
                    "groups": [_group("G1")],
                    "edges": [{"from": "G1", "to": "N1"}],
                }
            )
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [_node("N1", parent_group_id="G1")],
                    "groups": [_group("G1")],
                    "edges": [{"from": "N1", "to": "G1"}],
                }
            )

    def test_rejects_cross_hierarchy_deadlock(self):
        # G1 -> G2 (so every node in G1 depends on G2), while N2 (inside G2) -> N1 (inside G1)
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [
                        _node("N1", parent_group_id="G1"),
                        _node("N2", parent_group_id="G2"),
                    ],
                    "groups": [_group("G1"), _group("G2")],
                    "edges": [
                        {"from": "G1", "to": "G2"},
                        {"from": "N2", "to": "N1"},
                    ],
                }
            )

    def test_deep_nested_pre_prompts_outermost_to_innermost(self):
        graph = validate_prompt_graph(
            {
                "nodes": [_node("leaf", parent_group_id="g_inner")],
                "groups": [
                    _group("g_outer", pre_prompt="OUTER", temp_pre_prompt="T_OUTER"),
                    _group("g_mid", pre_prompt="MID", parent_group_id="g_outer"),
                    _group("g_inner", pre_prompt="INNER", temp_pre_prompt="T_INNER", parent_group_id="g_mid"),
                ],
                "edges": [],
            }
        )
        pres = effective_pre_prompts(graph, "leaf")
        self.assertEqual([p["group_id"] for p in pres], ["g_outer", "g_mid", "g_inner"])
        self.assertEqual([p["pre_prompt"] for p in pres], ["OUTER", "MID", "INNER"])
        self.assertEqual([p["temp_pre_prompt"] for p in pres], ["T_OUTER", "", "T_INNER"])

    def test_node_and_group_temp_prompt_and_completed_schema_fields(self):
        graph = validate_prompt_graph(
            {
                "nodes": [_node("A", temp_prompt="1-shot", completed=True)],
                "groups": [_group("G1", temp_pre_prompt="1-shot-pre", completed=True)],
                "edges": [],
            }
        )
        self.assertEqual(graph["nodes"][0]["temp_prompt"], "1-shot")
        self.assertTrue(graph["nodes"][0]["completed"])
        self.assertEqual(graph["groups"][0]["temp_pre_prompt"], "1-shot-pre")
        self.assertTrue(graph["groups"][0]["completed"])

    def test_destroy_node_with_batch_individual_partial_and_empty_temp_prompts(self):
        base = {
            "nodes": [
                _node("A", prompt="prompt-A", completed=True),
                _node("B", prompt="prompt-B", completed=True),
                _node("C", prompt="prompt-C", completed=True),
                _node("D", prompt="prompt-D", completed=True),
            ],
            "groups": [],
            "edges": [
                {"from": "A", "to": "B"},
                {"from": "C", "to": "B"},
                {"from": "D", "to": "B"},
            ],
        }
        deps = find_dependents(base, "B")
        self.assertEqual({d["id"] for d in deps}, {"A", "C", "D"})

        # Batch: apply same temp prompt to A and C, separate to D
        updated = delete_entity_with_temp_prompts(
            base,
            "B",
            {"A": "SHARED_TEMP", "C": "SHARED_TEMP", "D": "CUSTOM_D"},
        )
        by_id = {n["id"]: n for n in updated["nodes"]}
        chg_upd = compute_change_status(updated)["nodes"]
        self.assertNotIn("B", by_id)
        self.assertEqual(updated["edges"], [])
        self.assertEqual(by_id["A"]["prompt"], "prompt-A")
        self.assertEqual(by_id["A"]["temp_prompt"], "SHARED_TEMP")
        self.assertTrue(by_id["A"]["completed"])
        self.assertEqual(chg_upd["A"]["state"], "modified")
        self.assertTrue(chg_upd["A"]["needs_rerun"])
        self.assertEqual(by_id["A"]["dependencies"], [])
        self.assertEqual(by_id["C"]["temp_prompt"], "SHARED_TEMP")
        self.assertTrue(by_id["C"]["completed"])
        self.assertEqual(chg_upd["C"]["state"], "modified")
        self.assertEqual(by_id["D"]["temp_prompt"], "CUSTOM_D")
        self.assertTrue(by_id["D"]["completed"])
        self.assertEqual(chg_upd["D"]["state"], "modified")

        # Partial: only A gets temp prompt, C and D get none
        partial = delete_entity_with_temp_prompts(base, "B", {"A": "ONLY_A"})
        p_by_id = {n["id"]: n for n in partial["nodes"]}
        chg_part = compute_change_status(partial)["nodes"]
        self.assertEqual(p_by_id["A"]["temp_prompt"], "ONLY_A")
        self.assertTrue(p_by_id["A"]["completed"])
        self.assertEqual(chg_part["A"]["state"], "modified")
        self.assertEqual(p_by_id["C"]["temp_prompt"], "")
        self.assertTrue(p_by_id["C"]["completed"])
        self.assertEqual(chg_part["C"]["state"], "clean")

        # Empty: skip all temp prompts
        skipped = delete_entity_with_temp_prompts(base, "B", {})
        s_by_id = {n["id"]: n for n in skipped["nodes"]}
        self.assertEqual(s_by_id["A"]["temp_prompt"], "")
        self.assertTrue(s_by_id["A"]["completed"])
        self.assertEqual(compute_change_status(skipped)["nodes"]["A"]["state"], "clean")

        # Unknown entity in temp_prompts raises PromptGraphError
        with self.assertRaises(PromptGraphError):
            delete_entity_with_temp_prompts(base, "B", {"NO_SUCH_NODE": "x"})


class TestPromptGraphExecutionAndMCP(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_execution_and_dependency_ordering(self):
        # B1 and B2 are independent -> must run truly concurrently.
        # A depends on B1 and B2 -> must run only after both finish.
        graph = {
            "nodes": [
                _node("B1", prompt="task B1", model="model-b1"),
                _node("B2", prompt="task B2", model="model-b2"),
                _node("A", prompt="combine B1 and B2", model="model-a"),
            ],
            "groups": [],
            "edges": [
                {"from": "A", "to": "B1"},
                {"from": "A", "to": "B2"},
            ],
            "settings": {"parallelism": 4},
        }
        b1_started = asyncio.Event()
        b2_started = asyncio.Event()
        order: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            order.append(f"start:{nid}")
            if nid == "B1":
                b1_started.set()
                await asyncio.wait_for(b2_started.wait(), timeout=2.0)
            elif nid == "B2":
                b2_started.set()
                await asyncio.wait_for(b1_started.wait(), timeout=2.0)
            elif nid == "A":
                self.assertIn("end:B1", order)
                self.assertIn("end:B2", order)
            order.append(f"end:{nid}")
            return _reply(f"output-{nid}", model=model)

        tr = PromptGraphRun(graph, chat_fn)
        res = await tr.run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(res["result"]["outputs"]["A"], "output-A")
        self.assertEqual(res["result"]["outputs"]["B1"], "output-B1")
        self.assertEqual(res["result"]["outputs"]["B2"], "output-B2")

    async def test_real_mcp_node_dependency_lookup_and_isolation(self):
        # A -> B, C is unrelated.
        # A calls list_dependencies, get_node_info(B), and tries get_node_info(C) -> C must return ACCESS_DENIED.
        graph = {
            "nodes": [
                _node("B", prompt="compute 2+2", model="exaone-fast", extra={"source": "math", "priority": 1}),
                _node("A", prompt="verify B", temp_prompt="one-shot check", model="exaone-pro"),
                _node("C", prompt="secret unrelated", output="TOP_SECRET"),
            ],
            "groups": [
                _group("G_pre", pre_prompt="ALWAYS_APPLY_PRE_PROMPT", node_ids=["A"]),
            ],
            "edges": [{"from": "A", "to": "B"}],
        }
        observed_mcp_payloads: dict[str, dict] = {}
        seen_system_prompts: dict[str, str] = {}
        preview = api.prompt_graphs_preview_node({}, "ws-1", {"graph": graph, "node_id": "A"})["messages"]
        self.assertIn("ALWAYS_APPLY_PRE_PROMPT", preview[0]["content"])
        self.assertIn("get_node_info", preview[0]["content"])
        self.assertIn("one-shot check", preview[1]["content"])
        self.assertNotIn("TOP_SECRET", json.dumps(preview))

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            seen_system_prompts[nid] = messages[0]["content"]
            if nid == "B":
                return _reply("B_RESULT=4", model="exaone-fast")
            if nid == "C":
                return _reply("TOP_SECRET")
            # Node A scripted multi-step MCP tool conversation
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            if len(tool_msgs) == 0:
                return _reply(
                    tool_calls=[
                        _tool_call("list_dependencies", {}),
                        _tool_call("get_node_info", {"node_id": "B"}),
                        _tool_call("get_node_info", {"node_id": "C"}),
                    ]
                )
            observed_mcp_payloads["list"] = json.loads(tool_msgs[0]["content"])
            observed_mcp_payloads["B"] = json.loads(tool_msgs[1]["content"])
            observed_mcp_payloads["C"] = json.loads(tool_msgs[2]["content"])
            return _reply(f"A verified: {observed_mcp_payloads['B']['output']}", model="exaone-pro")

        tr = PromptGraphRun(graph, chat_fn, target_type="node", target_id="A")
        res = await tr.run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(res["node_state"]["A"]["initial_prompt"], {"iteration": 1, "messages": preview})
        self.assertIn("ALWAYS_APPLY_PRE_PROMPT", seen_system_prompts["A"])
        self.assertEqual(observed_mcp_payloads["B"]["prompt"], "compute 2+2")
        self.assertEqual(observed_mcp_payloads["B"]["output"], "B_RESULT=4")
        self.assertEqual(observed_mcp_payloads["B"]["model"], "exaone-fast")
        self.assertEqual(observed_mcp_payloads["B"]["extra"], {"source": "math", "priority": 1})
        self.assertEqual(observed_mcp_payloads["C"]["error"], "ACCESS_DENIED")
        self.assertEqual(res["result"]["outputs"]["A"], "A verified: B_RESULT=4")
        # Unrelated node C was NOT executed when running only node A
        self.assertEqual(res["node_state"]["C"]["status"], "idle")

    async def test_nested_group_to_group_dependency_and_recursive_mcp_inspection(self):
        # 3-level nested groups:
        # G_A (pre: "A_OUTER") -> G_A_sub (pre: "A_INNER") -> node a1
        # G_B (pre: "B_OUTER") -> G_B_sub (pre: "B_SUB") -> G_B_leaf (pre: "B_LEAF") -> nodes b1, b2
        # Edge: G_A -> G_B ("G_A depends on G_B")
        # Running G_A must execute b1 & b2 first (in parallel), then a1;
        # a1's system prompt has A_OUTER then A_INNER;
        # a1 calls MCP get_group_info(group_id="G_B") and receives G_B's pre_prompt AND nested G_B_sub / G_B_leaf + b1/b2!
        graph = {
            "nodes": [
                _node("b1", prompt="subtask b1", model="m-b1", extra={"k": 1}, parent_group_id="G_B_leaf"),
                _node("b2", prompt="subtask b2", model="m-b2", extra={"k": 2}, parent_group_id="G_B_leaf"),
                _node("a1", prompt="synthesize B group", model="m-a1", parent_group_id="G_A_sub"),
            ],
            "groups": [
                _group("G_B", pre_prompt="B_OUTER_PRE"),
                _group("G_B_sub", pre_prompt="B_SUB_PRE", parent_group_id="G_B"),
                _group("G_B_leaf", pre_prompt="B_LEAF_PRE", parent_group_id="G_B_sub"),
                _group("G_A", pre_prompt="A_OUTER_PRE"),
                _group("G_A_sub", pre_prompt="A_INNER_PRE", parent_group_id="G_A"),
            ],
            "edges": [{"from": "G_A", "to": "G_B"}],
            "settings": {"parallelism": 4},
        }
        events: list[dict] = []
        a1_system = ""
        mcp_group_info: dict = {}

        async def chat_fn(*, model, messages, tools, node, extra):
            nonlocal a1_system, mcp_group_info
            nid = node["id"]
            if nid in ("b1", "b2"):
                return _reply(f"done-{nid}", model=model)
            a1_system = messages[0]["content"]
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            if not tool_msgs:
                return _reply(tool_calls=[_tool_call("get_group_info", {"group_id": "G_B", "recursive": True})])
            mcp_group_info = json.loads(tool_msgs[0]["content"])
            leaf_nodes = mcp_group_info["child_groups"][0]["child_groups"][0]["nodes"]
            outs = ",".join(f"{n['id']}:{n['output']}" for n in leaf_nodes)
            return _reply(f"A1_SAW[{outs}]", model=model)

        tr = PromptGraphRun(graph, chat_fn, target_type="group", target_id="G_A", on_event=events.append)
        res = await tr.run()
        self.assertEqual(res["status"], "SUCCEEDED")
        # Order: A_OUTER_PRE appears before A_INNER_PRE
        self.assertLess(a1_system.index("A_OUTER_PRE"), a1_system.index("A_INNER_PRE"))
        # Recursive MCP payload contains 3-level group hierarchy & outputs
        self.assertEqual(mcp_group_info["pre_prompt"], "B_OUTER_PRE")
        sub = mcp_group_info["child_groups"][0]
        self.assertEqual(sub["id"], "G_B_sub")
        self.assertEqual(sub["pre_prompt"], "B_SUB_PRE")
        leaf = sub["child_groups"][0]
        self.assertEqual(leaf["id"], "G_B_leaf")
        self.assertEqual(leaf["pre_prompt"], "B_LEAF_PRE")
        self.assertEqual({n["id"]: n["output"] for n in leaf["nodes"]}, {"b1": "done-b1", "b2": "done-b2"})
        self.assertEqual(res["result"]["outputs"]["a1"], "A1_SAW[b1:done-b1,b2:done-b2]")
        self.assertTrue(any(e["type"] == "mcp_tool_call" and e["node"] == "a1" for e in events))

    async def test_failure_cascades_skip_while_unrelated_branch_succeeds(self):
        graph = {
            "nodes": [
                _node("root_fail", prompt="will fail"),
                _node("child_skip", prompt="should skip"),
                _node("independent_ok", prompt="will succeed"),
            ],
            "groups": [],
            "edges": [{"from": "child_skip", "to": "root_fail"}],
        }

        async def chat_fn(*, model, messages, tools, node, extra):
            if node["id"] == "root_fail":
                raise RuntimeError("boom")
            return _reply("ok-independent")

        tr = PromptGraphRun(graph, chat_fn)
        res = await tr.run()
        self.assertEqual(res["status"], "PARTIAL")
        self.assertEqual(res["node_state"]["root_fail"]["status"], "failed")
        self.assertEqual(res["node_state"]["child_skip"]["status"], "skipped")
        self.assertEqual(res["node_state"]["independent_ok"]["status"], "done")
        self.assertEqual(res["result"]["outputs"]["independent_ok"], "ok-independent")

    async def test_reuse_existing_outputs_skips_completed_dependencies(self):
        graph = {
            "nodes": [
                _node("dep", prompt="old", output="CACHED_OUTPUT"),
                _node("target", prompt="use dep"),
            ],
            "groups": [],
            "edges": [{"from": "target", "to": "dep"}],
        }
        called: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            called.append(node["id"])
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            if not tool_msgs:
                return _reply(tool_calls=[_tool_call("get_node_info", {"node_id": "dep"})])
            info = json.loads(tool_msgs[0]["content"])
            return _reply(f"got:{info['output']}")

        tr = PromptGraphRun(graph, chat_fn, target_type="node", target_id="target", reuse_existing_outputs=True)
        res = await tr.run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(called, ["target", "target"])
        self.assertEqual(res["node_state"]["dep"]["status"], "reused")
        self.assertEqual(res["result"]["outputs"]["target"], "got:CACHED_OUTPUT")

    async def test_node_temp_prompt_sent_and_cleared_once_completed_and_kept_on_failure(self):
        graph = {
            "nodes": [_node("A", prompt="BASE_PROMPT", temp_prompt="ONE_SHOT_ADDON")],
            "groups": [],
            "edges": [],
        }
        seen_user_prompts: list[str] = []

        async def chat_ok(*, model, messages, tools, node, extra):
            seen_user_prompts.append(messages[1]["content"])
            return _reply("OUTPUT_WITH_TEMP")

        res1 = await PromptGraphRun(graph, chat_ok, target_type="node", target_id="A").run()
        self.assertEqual(res1["status"], "SUCCEEDED")
        self.assertIn("BASE_PROMPT", seen_user_prompts[0])
        self.assertIn("ONE_SHOT_ADDON", seen_user_prompts[0])
        n1 = res1["graph"]["nodes"][0]
        self.assertEqual(n1["output"], "OUTPUT_WITH_TEMP")
        self.assertTrue(n1["completed"])
        self.assertEqual(n1["temp_prompt"], "")

        # Re-run on clean completed graph: skipped automatically without calling agent again
        res_skip = await PromptGraphRun(res1["graph"], chat_ok, target_type="node", target_id="A").run()
        self.assertEqual(res_skip["status"], "SUCCEEDED")
        self.assertEqual(res_skip["node_state"]["A"]["status"], "reused")
        self.assertEqual(len(seen_user_prompts), 1)

        # Modify prompt -> change detected -> runs again without cleared temp_prompt
        edited = deepcopy(res1["graph"])
        edited["nodes"][0]["prompt"] = "UPDATED_PROMPT"
        res2 = await PromptGraphRun(edited, chat_ok, target_type="node", target_id="A").run()
        self.assertEqual(res2["status"], "SUCCEEDED")
        self.assertEqual(len(seen_user_prompts), 2)
        self.assertIn("UPDATED_PROMPT", seen_user_prompts[1])
        self.assertNotIn("ONE_SHOT_ADDON", seen_user_prompts[1])

        # If execution fails, completed stays False and temp_prompt is preserved
        async def chat_fail(*, model, messages, tools, node, extra):
            raise RuntimeError("transient error")

        res_fail = await PromptGraphRun(graph, chat_fail, target_type="node", target_id="A").run()
        self.assertEqual(res_fail["status"], "FAILED")
        n_fail = res_fail["graph"]["nodes"][0]
        self.assertFalse(n_fail["completed"])
        self.assertEqual(n_fail["temp_prompt"], "ONE_SHOT_ADDON")

    async def test_group_destroy_temp_pre_prompt_parallel_siblings_and_partial_run(self):
        # G_A -> G_B; destroy G_B and assign temp_pre_prompt to G_A
        initial = {
            "nodes": [
                _node("a1", prompt="work 1", parent_group_id="G_A_in"),
                _node("a2", prompt="work 2", parent_group_id="G_A_in"),
                _node("b1", prompt="old B", parent_group_id="G_B"),
            ],
            "groups": [
                _group("G_A", pre_prompt="A_BASE_PRE"),
                _group("G_A_in", pre_prompt="A_IN_BASE", temp_pre_prompt="A_IN_TEMP", parent_group_id="G_A"),
                _group("G_B", pre_prompt="B_PRE"),
            ],
            "edges": [{"from": "G_A", "to": "G_B"}],
            "settings": {"parallelism": 4},
        }
        destroyed = delete_entity_with_temp_prompts(initial, "G_B", {"G_A": "A_TEMP_AFTER_B_REMOVED"})
        g_by_id = {g["id"]: g for g in destroyed["groups"]}
        self.assertNotIn("G_B", g_by_id)
        self.assertEqual(g_by_id["G_A"]["dependencies"], [])
        self.assertEqual(g_by_id["G_A"]["temp_pre_prompt"], "A_TEMP_AFTER_B_REMOVED")
        self.assertFalse(g_by_id["G_A"]["completed"])

        # Partial run: run ONLY a1 first -> a1 gets both outer & inner temp_pre_prompts,
        # a1 becomes completed, but G_A and G_A_in stay uncompleted and keep temp_pre_prompt until a2 also completes!
        seen_sys: dict[str, str] = {}

        async def chat_fn(*, model, messages, tools, node, extra):
            seen_sys[node["id"]] = messages[0]["content"]
            return _reply(f"out-{node['id']}")

        res_partial = await PromptGraphRun(destroyed, chat_fn, target_type="node", target_id="a1").run()
        self.assertEqual(res_partial["status"], "SUCCEEDED")
        self.assertIn("A_BASE_PRE", seen_sys["a1"])
        self.assertIn("A_TEMP_AFTER_B_REMOVED", seen_sys["a1"])
        self.assertIn("A_IN_TEMP", seen_sys["a1"])
        self.assertLess(seen_sys["a1"].index("A_TEMP_AFTER_B_REMOVED"), seen_sys["a1"].index("A_IN_TEMP"))

        p_nodes = {n["id"]: n for n in res_partial["graph"]["nodes"]}
        p_groups = {g["id"]: g for g in res_partial["graph"]["groups"]}
        self.assertTrue(p_nodes["a1"]["completed"])
        self.assertFalse(p_nodes["a2"]["completed"])
        self.assertFalse(p_groups["G_A"]["completed"])
        self.assertFalse(p_groups["G_A_in"]["completed"])
        self.assertEqual(p_groups["G_A"]["temp_pre_prompt"], "A_TEMP_AFTER_B_REMOVED")
        self.assertEqual(p_groups["G_A_in"]["temp_pre_prompt"], "A_IN_TEMP")

        # Now run a2 -> both a1 & a2 are completed -> both G_A_in and G_A become completed and clear temp_pre_prompt!
        res_rest = await PromptGraphRun(res_partial["graph"], chat_fn, target_type="node", target_id="a2").run()
        self.assertEqual(res_rest["status"], "SUCCEEDED")
        self.assertIn("A_TEMP_AFTER_B_REMOVED", seen_sys["a2"])
        self.assertIn("A_IN_TEMP", seen_sys["a2"])
        r_groups = {g["id"]: g for g in res_rest["graph"]["groups"]}
        self.assertTrue(r_groups["G_A_in"]["completed"])
        self.assertTrue(r_groups["G_A"]["completed"])
        self.assertEqual(r_groups["G_A_in"]["temp_pre_prompt"], "")
        self.assertEqual(r_groups["G_A"]["temp_pre_prompt"], "")

        # Also test full group execution with parallel siblings where a1 finishes faster than a2
        res_full_group = await PromptGraphRun(destroyed, chat_fn, target_type="group", target_id="G_A").run()
        self.assertEqual(res_full_group["status"], "SUCCEEDED")
        fg_nodes = {n["id"]: n for n in res_full_group["graph"]["nodes"]}
        fg_groups = {g["id"]: g for g in res_full_group["graph"]["groups"]}
        self.assertTrue(fg_nodes["a1"]["completed"] and fg_nodes["a2"]["completed"])
        self.assertTrue(fg_groups["G_A"]["completed"] and fg_groups["G_A_in"]["completed"])
        self.assertEqual(fg_groups["G_A"]["temp_pre_prompt"], "")
        self.assertEqual(fg_groups["G_A_in"]["temp_pre_prompt"], "")


class _Cursor:
    def __init__(self, rows=None, rowcount=None):
        self._rows = list(rows or [])
        self.rowcount = rowcount if rowcount is not None else len(self._rows)

    def fetchone(self):
        return deepcopy(self._rows[0]) if self._rows else None

    def fetchall(self):
        return deepcopy(self._rows)


class _FakeConn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        unwrap = lambda v: v.obj if hasattr(v, "obj") else v
        if q.startswith("SELECT graph_id, name, graph_json, updated_at FROM prompt_graphs"):
            ws = str(params[0])
            rows = [r for r in self.db["graphs"].values() if r["workspace_id"] == ws]
            return _Cursor(rows)
        if q.startswith("SELECT graph_json FROM prompt_graphs"):
            gid, ws = str(params[0]), str(params[1])
            row = self.db["graphs"].get(gid)
            return _Cursor([row] if row and row["workspace_id"] == ws else [])
        if q.startswith("INSERT INTO prompt_graphs"):
            ws, name, gjson, uid = params
            gid = f"10000000-0000-4000-8000-{len(self.db['graphs']) + 1:012d}"
            row = {
                "graph_id": gid,
                "workspace_id": str(ws),
                "name": name,
                "graph_json": unwrap(gjson),
                "updated_by": uid,
                "updated_at": "2026-11-24T00:00:00Z",
            }
            self.db["graphs"][gid] = row
            return _Cursor([row])
        if q.startswith("UPDATE prompt_graphs SET name="):
            name, gjson, uid, gid, ws = params
            row = self.db["graphs"].get(str(gid))
            if not row or row["workspace_id"] != str(ws):
                return _Cursor([])
            row["name"] = name
            row["graph_json"] = unwrap(gjson)
            return _Cursor([row])
        if q.startswith("UPDATE prompt_graphs SET graph_json="):
            gjson, gid, ws = params
            row = self.db["graphs"].get(str(gid))
            if row and row["workspace_id"] == str(ws):
                row["graph_json"] = unwrap(gjson)
            return _Cursor([], rowcount=1 if row else 0)
        if q.startswith("DELETE FROM prompt_graphs"):
            gid, ws = str(params[0]), str(params[1])
            row = self.db["graphs"].get(gid)
            if row and row["workspace_id"] == ws:
                del self.db["graphs"][gid]
                return _Cursor([], rowcount=1)
            return _Cursor([], rowcount=0)
        if q.startswith("INSERT INTO prompt_graph_runs"):
            gid, ws, ttype, tid, gjson, uid = params
            rid = f"20000000-0000-4000-8000-{len(self.db['runs']) + 1:012d}"
            row = {
                "run_id": rid,
                "graph_id": gid,
                "workspace_id": str(ws),
                "target_type": ttype,
                "target_id": tid,
                "graph_json": unwrap(gjson),
                "status": "RUNNING",
                "node_state": {},
                "result_json": {},
                "created_at": "2026-11-24T00:00:00Z",
                "updated_at": "2026-11-24T00:00:00Z",
                "stale": False,
            }
            self.db["runs"][rid] = row
            return _Cursor([row])
        if q.startswith("INSERT INTO prompt_graph_events"):
            rid, seq, typ, node, target, content, meta = params
            self.db["events"].append(
                {
                    "run_id": str(rid),
                    "seq": seq,
                    "type": typ,
                    "node": node,
                    "target": target,
                    "content": content,
                    "meta_json": unwrap(meta),
                    "created_at": "2026-11-24T00:00:00Z",
                }
            )
            return _Cursor([], rowcount=1)
        if q.startswith("UPDATE prompt_graph_runs SET node_state="):
            nstate, gjson, rid = params
            row = self.db["runs"][str(rid)]
            row["node_state"] = unwrap(nstate)
            row["graph_json"] = unwrap(gjson)
            return _Cursor([], rowcount=1)
        if q.startswith("UPDATE prompt_graph_runs SET status="):
            status, rjson, nstate, gjson, rid = params
            row = self.db["runs"][str(rid)]
            row["status"] = status
            row["result_json"] = unwrap(rjson)
            row["node_state"] = unwrap(nstate)
            row["graph_json"] = unwrap(gjson)
            return _Cursor([], rowcount=1)
        if q.startswith("SELECT run_id, graph_id, target_type, target_id, graph_json, status"):
            rid, ws = str(params[0]), str(params[1])
            row = self.db["runs"].get(rid)
            return _Cursor([row] if row and row["workspace_id"] == ws else [])
        if q.startswith("SELECT seq, type, node, target, content, meta_json"):
            rid, after = str(params[0]), int(params[1])
            rows = [e for e in self.db["events"] if e["run_id"] == rid and e["seq"] > after]
            return _Cursor(rows)
        if q.startswith("SELECT run_id, graph_id, target_type, target_id, status"):
            ws = str(params[0])
            rows = [r for r in self.db["runs"].values() if r["workspace_id"] == ws]
            return _Cursor(rows)
        if q.startswith("INSERT INTO prompt_node_versions"):
            (ws, gid, rid, nid, nlbl, it, ihash, pr, tpr, epre, out, mdl, st, cp, cerrs, pt, ct, tt, cost, ms, chit) = params
            vid = len(self.db["versions"]) + 1
            row = {
                "version_id": vid,
                "workspace_id": str(ws),
                "graph_id": gid,
                "run_id": rid,
                "node_id": nid,
                "node_label": nlbl,
                "iteration": it,
                "input_hash": ihash,
                "prompt": pr,
                "temp_prompt": tpr,
                "effective_pre_prompt": epre,
                "output": out,
                "model": mdl,
                "status": st,
                "contract_passed": cp,
                "contract_errors": unwrap(cerrs),
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "cost_usd": cost,
                "duration_ms": ms,
                "cache_hit": chit,
                "created_at": "2026-11-24T00:00:00Z",
            }
            self.db["versions"].append(row)
            return _Cursor([], rowcount=1)
        if q.startswith("SELECT output, model, prompt_tokens, completion_tokens, total_tokens FROM prompt_node_versions"):
            ws, ihash = str(params[0]), str(params[1])
            hits = [v for v in reversed(self.db["versions"]) if v["workspace_id"] == ws and v["input_hash"] == ihash and v["status"] == "done"]
            return _Cursor(hits[:1])
        if q.startswith("SELECT version_id, run_id, node_id, node_label"):
            ws, gid, nid, lim = str(params[0]), params[1], str(params[2]), int(params[3])
            hits = [v for v in reversed(self.db["versions"]) if v["workspace_id"] == ws and v["graph_id"] == gid and v["node_id"] == nid]
            return _Cursor(hits[:lim])
        if q.startswith("SELECT version_id, node_id, prompt, temp_prompt"):
            ws, gid, nid, vid = str(params[0]), params[1], str(params[2]), int(params[3])
            hits = [v for v in self.db["versions"] if v["workspace_id"] == ws and v["graph_id"] == gid and v["node_id"] == nid and v["version_id"] == vid]
            return _Cursor(hits[:1])
        if q.startswith("SELECT node_id, MAX(node_label) AS node_label"):
            ws, gid = str(params[0]), params[1]
            by_nid = {}
            for v in self.db["versions"]:
                if v["workspace_id"] == ws and v["graph_id"] == gid:
                    by_nid.setdefault(v["node_id"], []).append(v)
            rows = []
            for nid, vlist in by_nid.items():
                rows.append({
                    "node_id": nid,
                    "node_label": vlist[-1]["node_label"],
                    "executions": len(vlist),
                    "avg_ms": int(sum(x["duration_ms"] for x in vlist) / len(vlist)),
                    "avg_tokens": int(sum(x["total_tokens"] for x in vlist) / len(vlist)),
                    "total_tokens": sum(x["total_tokens"] for x in vlist),
                    "total_cost_usd": sum(float(x["cost_usd"]) for x in vlist),
                    "cache_hits": sum(1 for x in vlist if x["cache_hit"]),
                })
            return _Cursor(rows)
        if q.startswith("INSERT INTO prompt_node_impact_reviews"):
            ws, gid, rid, src, tgt, nr, rsn, dec, _dat = params
            rv_id = len(self.db["reviews"]) + 1
            self.db["reviews"].append({
                "review_id": rv_id,
                "workspace_id": str(ws),
                "graph_id": gid,
                "run_id": rid,
                "source_node_id": src,
                "target_id": tgt,
                "needs_rerun": nr,
                "reason": rsn,
                "decision": dec,
                "decided_at": "2026-11-24T00:00:00Z" if dec != "PENDING" else None,
                "created_at": "2026-11-24T00:00:00Z",
            })
            return _Cursor([], rowcount=1)
        if q.startswith("SELECT review_id, run_id, source_node_id"):
            ws, gid, lim = str(params[0]), params[1], int(params[2])
            hits = [r for r in reversed(self.db["reviews"]) if r["workspace_id"] == ws and r["graph_id"] == gid]
            return _Cursor(hits[:lim])
        if q.startswith("UPDATE prompt_node_impact_reviews SET decision='DISMISSED'"):
            ws, gid, key = str(params[0]), params[1], params[2]
            cnt = 0
            for r in self.db["reviews"]:
                if r["workspace_id"] == ws and r["graph_id"] == gid:
                    if isinstance(key, int) and r["review_id"] == key:
                        r["decision"] = "DISMISSED"
                        cnt += 1
                    elif isinstance(key, str) and r["target_id"] == key and r["decision"] == "PENDING":
                        r["decision"] = "DISMISSED"
                        cnt += 1
            return _Cursor([], rowcount=cnt)
        if q.startswith("SELECT graph_id, name, graph_json FROM prompt_graphs"):
            if "workspace_id=%s AND graph_id=%s" in q:
                ws, gid = str(params[0]), str(params[1])
                row = self.db["graphs"].get(gid)
                return _Cursor([row] if row and row["workspace_id"] == ws else [])
            gid, ws = str(params[0]), str(params[1])
            row = self.db["graphs"].get(gid)
            return _Cursor([row] if row and row["workspace_id"] == ws else [])
        if q.startswith("SELECT name, graph_json FROM prompt_graphs"):
            gid, ws = str(params[0]), str(params[1])
            row = self.db["graphs"].get(gid)
            return _Cursor([row] if row and row["workspace_id"] == ws else [])
        if q.startswith("SELECT name FROM prompt_graphs"):
            gid, ws = str(params[0]), str(params[1])
            row = self.db["graphs"].get(gid)
            return _Cursor([row] if row and row["workspace_id"] == ws else [])
        if q.startswith("SELECT template_id, name, description"):
            ws = str(params[0])
            rows = [r for r in self.db["templates"].values() if r["workspace_id"] == ws]
            return _Cursor(rows)
        if q.startswith("SELECT template_id, name, subtree_json FROM prompt_graph_templates"):
            tid, ws = str(params[0]), str(params[1])
            row = self.db["templates"].get(tid)
            return _Cursor([row] if row and row["workspace_id"] == ws else [])
        if q.startswith("INSERT INTO prompt_graph_templates"):
            ws, name, desc, td, nc, gc, sjson, uid = params
            tid = f"30000000-0000-4000-8000-{len(self.db['templates']) + 1:012d}"
            row = {
                "template_id": tid,
                "workspace_id": str(ws),
                "name": name,
                "description": desc,
                "tier_depth": td,
                "node_count": nc,
                "group_count": gc,
                "subtree_json": unwrap(sjson),
                "created_at": "2026-11-24T00:00:00Z",
                "updated_at": "2026-11-24T00:00:00Z",
            }
            self.db["templates"][tid] = row
            return _Cursor([row])
        if q.startswith("DELETE FROM prompt_graph_templates"):
            tid, ws = str(params[0]), str(params[1])
            row = self.db["templates"].get(tid)
            if row and row["workspace_id"] == ws:
                del self.db["templates"][tid]
                return _Cursor([], rowcount=1)
            return _Cursor([], rowcount=0)
        raise AssertionError(f"Unhandled SQL in fake DB: {q}")


class TestPromptGraphServiceAndApiRoutes(unittest.TestCase):
    def setUp(self):
        self.db = {"graphs": {}, "runs": {}, "events": [], "versions": [], "reviews": [], "templates": {}}
        self.orig_pg = infra.pg
        self.orig_chat = pgs.make_gateway_chat_fn
        self.orig_ws_model = pgs.workspace_default_model
        self.orig_gw_status = api._gateway_status

        @contextlib.contextmanager
        def fake_pg(readonly=False):
            yield _FakeConn(self.db)

        infra.pg = fake_pg
        pgs.workspace_default_model = lambda _ws: "mid"
        api._gateway_status = lambda: {"ok": True, "aliases": {"cheap": "c", "mid": "m", "advisor": "a"}}

    def tearDown(self):
        infra.pg = self.orig_pg
        pgs.make_gateway_chat_fn = self.orig_chat
        pgs.workspace_default_model = self.orig_ws_model
        api._gateway_status = self.orig_gw_status

    def test_crud_validate_and_run_lifecycle_routes(self):
        graph = {
            "nodes": [_node("n1", prompt="step1", temp_prompt="t1"), _node("n2", prompt="step2", temp_prompt="t2")],
            "groups": [_group("g1", pre_prompt="PRE", temp_pre_prompt="tp1", node_ids=["n2"])],
            "edges": [{"from": "n2", "to": "n1"}],
        }

        val = api.prompt_graphs_validate({}, "ws-1", {"graph": graph})
        self.assertTrue(val["ok"])
        self.assertEqual(val["graph"]["nodes"][1]["dependencies"], ["n1"])

        bad = api.prompt_graphs_validate({}, "ws-1", {"graph": {"nodes": [_node("x")], "groups": [], "edges": [{"from": "x", "to": "x"}]}})
        self.assertFalse(bad["ok"])

        saved = api.prompt_graphs_save({"user": None}, "ws-1", {"name": "My Graph", "graph": graph})
        gid = saved["graph_id"]
        listed = api.prompt_graphs_list({}, "ws-1", {})
        self.assertEqual(len(listed["graphs"]), 1)
        self.assertIn("basic_dependency", listed["presets"])

        def fake_chat_factory(ws, rid, default_model="mid"):
            async def chat_fn(*, model, messages, tools, node, extra):
                if node["id"] == "n1":
                    return _reply("n1-out")
                tool_msgs = [m for m in messages if m["role"] == "tool"]
                if not tool_msgs:
                    return _reply(tool_calls=[_tool_call("get_node_info", {"node_id": "n1"})])
                info = json.loads(tool_msgs[0]["content"])
                return _reply(f"n2-saw-{info.get('output', 'fallback')}")

            async def aclose():
                pass

            chat_fn.aclose = aclose
            return chat_fn

        pgs.make_gateway_chat_fn = fake_chat_factory
        started = api.prompt_graph_run_start(
            {"user": None},
            "ws-1",
            {"graph_id": gid, "target_type": "group", "target_id": "g1"},
        )
        rid = started["run_id"]

        for _ in range(200):
            run_row = api.prompt_graph_run_get({"query": {}}, "ws-1", {}, rid)
            if run_row["status"] != "RUNNING":
                break
            time.sleep(0.02)

        self.assertEqual(run_row["status"], "SUCCEEDED")
        self.assertEqual(run_row["result_json"]["outputs"]["n1"], "n1-out")
        self.assertEqual(run_row["result_json"]["outputs"]["n2"], "n2-saw-n1-out")
        # Saved graph outputs, completed flags, and cleared temp prompts were persisted after completion
        saved_after = self.db["graphs"][gid]["graph_json"]
        outs_after = {n["id"]: n["output"] for n in saved_after["nodes"]}
        self.assertEqual(outs_after, {"n1": "n1-out", "n2": "n2-saw-n1-out"})
        self.assertTrue(all(n["completed"] and n["temp_prompt"] == "" for n in saved_after["nodes"]))
        self.assertTrue(saved_after["groups"][0]["completed"])
        self.assertEqual(saved_after["groups"][0]["temp_pre_prompt"], "")

        runs_list = api.prompt_graph_runs_list({"query": {"graph_id": [gid]}}, "ws-1", {})
        self.assertEqual(len(runs_list["runs"]), 1)

        # Destroy n1 via HTTP route and assign temp_prompt to dependent n2
        destroyed_res = api.prompt_graphs_destroy_entity(
            {"user": None},
            "ws-1",
            {"graph": saved_after, "target_id": "n1", "temp_prompts": {"n2": "TEMP_AFTER_N1_GONE"}},
        )
        self.assertTrue(destroyed_res["ok"])
        d_nodes = {n["id"]: n for n in destroyed_res["graph"]["nodes"]}
        self.assertNotIn("n1", d_nodes)
        self.assertEqual(d_nodes["n2"]["temp_prompt"], "TEMP_AFTER_N1_GONE")
        self.assertTrue(d_nodes["n2"]["completed"])
        self.assertEqual(d_nodes["n2"]["dependencies"], [])
        chg_after_destroy = api.prompt_graphs_validate({}, "ws-1", {"graph": destroyed_res["graph"]})
        self.assertTrue(chg_after_destroy["ok"])
        self.assertEqual(chg_after_destroy["change_status"]["nodes"]["n2"]["state"], "modified")
        self.assertTrue(chg_after_destroy["change_status"]["nodes"]["n2"]["needs_rerun"])

        # Subset run API execution on modified n2
        subset_run = api.prompt_graph_run_start(
            {"user": None},
            "ws-1",
            {"graph": destroyed_res["graph"], "target_type": "subset", "target_ids": ["n2"]},
        )
        self.assertEqual(subset_run["target_type"], "subset")
        self.assertEqual(subset_run["run_nodes"], ["n2"])
        for _ in range(200):
            s_row = api.prompt_graph_run_get({"query": {}}, "ws-1", {}, subset_run["run_id"])
            if s_row["status"] != "RUNNING":
                break
            time.sleep(0.02)
        self.assertEqual(s_row["status"], "SUCCEEDED")

        # Also verify completed=True and cleared temp_prompt persisted after run
        self.assertTrue(api.prompt_graphs_delete({}, "ws-1", {}, gid)["ok"])

    def test_enhanced_api_versions_checkout_search_impact_templates_analytics_and_artifacts(self):
        graph = {
            "nodes": [
                _node(
                    "src",
                    prompt="produce spec brief",
                    artifact={"kind": "repo_file", "repo": "workspace", "path": "docs/spec.md", "mode": "read"},
                ),
                _node("dst", prompt="summarize spec"),
            ],
            "groups": [_group("g_outer", child_group_ids=["g_inner"]), _group("g_inner", parent_group_id="g_outer", node_ids=["dst"])],
            "edges": [{"from": "dst", "to": "src"}],
            "settings": {"edge_direction": "execution"},
        }
        saved = api.prompt_graphs_save({"user": None}, "ws-1", {"name": "Enhanced", "graph": graph})
        gid = saved["graph_id"]

        phase = [1]

        def fake_chat_factory(ws, rid, default_model="mid"):
            async def chat_fn(*, model, messages, tools, node, extra):
                return _reply(f"{node['id']}-v{phase[0]}")

            async def aclose():
                pass

            chat_fn.aclose = aclose
            return chat_fn

        pgs.make_gateway_chat_fn = fake_chat_factory

        def _run_sync_wait(target_type="all", target_id=None, g_override=None):
            st = api.prompt_graph_run_start({"user": None}, "ws-1", {"graph_id": gid, "graph": g_override, "target_type": target_type, "target_id": target_id})
            for _ in range(200):
                row = api.prompt_graph_run_get({"query": {}}, "ws-1", {}, st["run_id"])
                if row["status"] != "RUNNING":
                    return row
                time.sleep(0.02)
            return row

        r1 = _run_sync_wait()
        self.assertEqual(r1["status"], "SUCCEEDED")
        self.assertIn("profile", r1["result_json"])

        # Modify src prompt to force rerun, run second time to create v2 version and record MCP impact on dst
        g_cur = deepcopy(self.db["graphs"][gid]["graph_json"])
        g_cur["nodes"][0]["prompt"] = "produce spec brief v2"
        phase[0] = 2

        def fake_impact_chat_factory(ws, rid, default_model="mid"):
            async def chat_fn(*, model, messages, tools, node, extra):
                t_names = {t["function"]["name"] for t in (tools or [])}
                if "mark_needs_rerun" in t_names:
                    tmsgs = [m for m in messages if m["role"] == "tool"]
                    if not tmsgs:
                        return _reply(tool_calls=[_tool_call("mark_needs_rerun", {"entity_id": "dst", "reason": "v2 breaking change"})])
                    return _reply("impact done")
                return _reply(f"{node['id']}-v2")

            async def aclose():
                pass

            chat_fn.aclose = aclose
            return chat_fn

        pgs.make_gateway_chat_fn = fake_impact_chat_factory
        r2 = _run_sync_wait(g_override=g_cur)
        self.assertEqual(r2["status"], "SUCCEEDED")

        # Version list + Diff + Checkout back to v1
        vers = api.prompt_graph_node_versions({}, "ws-1", {}, gid, "src")["versions"]
        self.assertEqual(len(vers), 2)
        v1_id = min(v["version_id"] for v in vers)
        chk = api.prompt_graph_node_checkout({"user": None}, "ws-1", {"version_id": v1_id, "restore_prompt": True}, gid, "src")
        src_after = next(n for n in chk["graph"]["nodes"] if n["id"] == "src")
        self.assertEqual(src_after["output"], "src-v1")

        # Workspace search
        hits = api.prompt_graphs_search({"query": {"q": ["spec brief"]}}, "ws-1", {})["results"]
        self.assertTrue(any(h["node_id"] == "src" for h in hits))

        # Impact reviews list & dismiss on dst
        revs = api.prompt_graph_impact_reviews_list({}, "ws-1", {}, gid)["reviews"]
        self.assertTrue(any(r["target_id"] == "dst" and r["needs_rerun"] for r in revs))
        dis = api.prompt_graph_impact_review_dismiss({"user": None}, "ws-1", {"entity_id": "dst"}, gid)
        dst_after = next(n for n in dis["graph"]["nodes"] if n["id"] == "dst")
        self.assertFalse(dst_after["needs_rerun"])

        # Templates save, list, instantiate, delete
        tpl = api.prompt_graph_templates_save(
            {"user": None},
            "ws-1",
            {"name": "2티어 요약 모듈", "graph": dis["graph"], "group_id": "g_outer"},
        )
        self.assertEqual(tpl["tier_depth"], 2)
        tpls = api.prompt_graph_templates_list({}, "ws-1", {})["templates"]
        self.assertEqual(len(tpls), 1)
        inst = api.prompt_graph_templates_instantiate({}, "ws-1", {"graph": dis["graph"], "offset_x": 300}, tpl["template_id"])
        self.assertGreater(len(inst["graph"]["groups"]), len(dis["graph"]["groups"]))
        self.assertTrue(api.prompt_graph_templates_delete({}, "ws-1", {}, tpl["template_id"])["ok"])

        # Analytics
        ana = api.prompt_graph_analytics_get({}, "ws-1", {}, gid)
        self.assertGreaterEqual(ana["summary"]["total_executions"], 2)

        # Artifact bidirectional sync marks src needs_rerun when external file hash changes
        ext = pgs.sync_graph_artifacts(
            "ws-1",
            inst["graph"],
            graph_id=gid,
            artifact_reader=lambda n, _g: ("changed external spec content", "hash_ext_999"),
        )
        self.assertIn("src", ext["changed_nodes"])
        src_synced = next(n for n in ext["graph"]["nodes"] if n["id"] == "src")
        self.assertTrue(src_synced["needs_rerun"])

    def test_auto_layout_and_mcp_admin_api_routes(self):
        initial = validate_prompt_graph({
            "nodes": [_node("n1", x=50, y=50), _node("n2", x=55, y=55)],
            "groups": [],
            "edges": [],
        })
        step_r = [0]

        def _fake_chat_factory(ws, rid, default_model="mid"):
            async def _cfn(*, model, messages, tools, node, extra):
                step_r[0] += 1
                if step_r[0] == 1:
                    return _reply(tool_calls=[_tool_call("create_group", {"id": "g_pipe", "label": "파이프라인", "adopt_entity_ids": ["n1", "n2"]})])
                if step_r[0] == 2:
                    return _reply(tool_calls=[_tool_call("create_node", {"id": "n3", "label": "후속 단계"})])
                return _reply("그룹 생성 및 n3 추가 완료 (겹침 0건)")

            async def _ac():
                pass

            _cfn.aclose = _ac
            return _cfn

        pgs.make_gateway_chat_fn = _fake_chat_factory
        saved = api.prompt_graphs_save({"user": None}, "ws-1", {"name": "AutoLayoutTest", "graph": initial})
        gid = saved["graph_id"]
        al = api.prompt_graphs_auto_layout({"user": None}, "ws-1", {"graph_id": gid, "save": True})
        self.assertTrue(al["ok"] and al["layout_report"]["ok"])
        tl = api.prompt_graphs_mcp_tool(
            {"user": None},
            "ws-1",
            {"graph_id": gid, "tool": "update_settings", "arguments": {"edge_direction": "execution"}, "save": True},
        )
        self.assertTrue(tl["ok"] and tl["graph"]["settings"]["edge_direction"] == "execution")
        mg = api.prompt_graphs_mcp_manage(
            {"user": None},
            "ws-1",
            {"graph_id": gid, "instruction": "두 노드를 그룹으로 묶고 n3 추가", "save": True},
        )
        self.assertTrue(mg["ok"] and mg["changed"] and mg["layout_report"]["ok"])
        persisted = self.db["graphs"][gid]["graph_json"]
        self.assertTrue(any(n["id"] == "n3" for n in persisted["nodes"]))
        self.assertTrue(any(g["id"] == "g_pipe" for g in persisted["groups"]))
        self.assertTrue(pg.detect_layout_overlaps(persisted)["ok"])


class TestChangeDetectionAndIncrementalExecution(unittest.IsolatedAsyncioTestCase):
    async def test_completed_node_skipped_on_rerun_and_when_dependent_runs(self):
        graph = {
            "nodes": [
                _node("B", prompt="step B"),
                _node("A", prompt="step A using B"),
            ],
            "groups": [],
            "edges": [{"from": "A", "to": "B"}],
        }
        calls: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            calls.append(nid)
            if nid == "B":
                return _reply("B_OUT")
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            if not tool_msgs:
                return _reply(tool_calls=[_tool_call("get_node_info", {"node_id": "B"})])
            info = json.loads(tool_msgs[0]["content"])
            return _reply(f"A_SAW:{info['output']}")

        # Run B first -> B becomes completed=True (clean)
        r_b = await PromptGraphRun(graph, chat_fn, target_type="node", target_id="B").run()
        self.assertEqual(r_b["status"], "SUCCEEDED")
        self.assertEqual(calls, ["B"])

        # Now run A -> B is already clean completed, so B must NOT call agent again!
        calls.clear()
        r_a = await PromptGraphRun(r_b["graph"], chat_fn, target_type="node", target_id="A").run()
        self.assertEqual(r_a["status"], "SUCCEEDED")
        self.assertEqual(calls, ["A", "A"])
        self.assertEqual(r_a["node_state"]["B"]["status"], "reused")
        self.assertEqual(r_a["result"]["outputs"]["A"], "A_SAW:B_OUT")

        # Now run entire graph -> neither A nor B should be called
        calls.clear()
        planned = plan_execution(r_a["graph"], target_type="all")
        self.assertEqual(planned["run_nodes"], [])
        self.assertEqual(set(planned["skipped_clean_nodes"]), {"A", "B"})
        r_all = await PromptGraphRun(r_a["graph"], chat_fn, target_type="all").run()
        self.assertEqual(r_all["status"], "SUCCEEDED")
        self.assertEqual(calls, [])

    def test_rejects_edge_from_completed_to_unexecuted(self):
        # Completed node A cannot depend on unexecuted node B
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [_node("A", completed=True), _node("B", completed=False)],
                    "groups": [],
                    "edges": [{"from": "A", "to": "B"}],
                }
            )
        # Completed group G_A cannot depend on unexecuted group G_B
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [],
                    "groups": [_group("G_A", completed=True), _group("G_B", completed=False)],
                    "edges": [{"from": "G_A", "to": "G_B"}],
                }
            )
        # Node inside completed group G_A cannot cause G_A to depend on unexecuted node B
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph(
                {
                    "nodes": [_node("a1", parent_group_id="G_A", completed=True), _node("B", completed=False)],
                    "groups": [_group("G_A", completed=True)],
                    "edges": [{"from": "G_A", "to": "B"}],
                }
            )

    async def test_git_like_change_detection_revert_and_transitive_cascade(self):
        # A -> B -> C (all completed initially)
        base = validate_prompt_graph(
            {
                "nodes": [
                    _node("C", prompt="c-orig", output="c-out", completed=True),
                    _node("B", prompt="b-orig", output="b-out", completed=True),
                    _node("A", prompt="a-orig", output="a-out", completed=True),
                ],
                "groups": [],
                "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}],
            }
        )
        chg0 = compute_change_status(base)["nodes"]
        self.assertEqual({k: chg0[k]["state"] for k in ("A", "B", "C")}, {"A": "clean", "B": "clean", "C": "clean"})

        # Edit C.prompt -> ONLY C becomes modified; B and A stay clean until agent judges impact
        edited = deepcopy(base)
        edited["nodes"][0]["prompt"] = "c-v2"
        chg1 = compute_change_status(edited)["nodes"]
        self.assertEqual(chg1["C"]["state"], "modified")
        self.assertEqual(chg1["B"]["state"], "clean")
        self.assertEqual(chg1["A"]["state"], "clean")

        # Revert C.prompt -> all 3 back to clean
        reverted = deepcopy(edited)
        reverted["nodes"][0]["prompt"] = "c-orig"
        chg_rev = compute_change_status(reverted)["nodes"]
        self.assertEqual({k: chg_rev[k]["state"] for k in ("A", "B", "C")}, {"A": "clean", "B": "clean", "C": "clean"})

    async def test_group_pre_prompt_change_cascade_and_partial_member_run(self):
        # G_A(a1) -> G_B(b1, b2), all completed initially
        graph = validate_prompt_graph(
            {
                "nodes": [
                    _node("b1", prompt="b1-p", output="b1-o", parent_group_id="G_B", completed=True),
                    _node("b2", prompt="b2-p", output="b2-o", parent_group_id="G_B", completed=True),
                    _node("a1", prompt="a1-p", output="a1-o", parent_group_id="G_A", completed=True),
                ],
                "groups": [
                    _group("G_B", pre_prompt="B_PRE_1", completed=True),
                    _group("G_A", pre_prompt="A_PRE_1", completed=True),
                ],
                "edges": [{"from": "G_A", "to": "G_B"}],
            }
        )
        # Edit G_B.pre_prompt -> G_B=modified; b1, b2=needs_rerun (enclosing group), while G_A and a1 stay clean!
        edited = deepcopy(graph)
        edited["groups"][0]["pre_prompt"] = "B_PRE_2"
        chg1 = compute_change_status(edited)
        self.assertEqual(chg1["groups"]["G_B"]["state"], "modified")
        self.assertEqual(chg1["nodes"]["b1"]["state"], "needs_rerun")
        self.assertEqual(chg1["nodes"]["b2"]["state"], "needs_rerun")
        self.assertEqual(chg1["groups"]["G_A"]["state"], "clean")
        self.assertEqual(chg1["nodes"]["a1"]["state"], "clean")

        calls: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            tool_names = {t["function"]["name"] for t in tools}
            if "mark_needs_rerun" in tool_names:
                return _reply("no downstream impact")
            calls.append(node["id"])
            return _reply(f"{node['id']}-new")

        # Partial run: ONLY b1
        r_b1 = await PromptGraphRun(edited, chat_fn, target_type="node", target_id="b1").run()
        self.assertEqual(calls, ["b1"])
        chg2 = compute_change_status(r_b1["graph"])
        self.assertEqual(chg2["nodes"]["b1"]["state"], "clean")
        self.assertEqual(chg2["nodes"]["b2"]["state"], "needs_rerun")
        self.assertEqual(chg2["groups"]["G_B"]["state"], "modified")

        # Now run G_B -> b1 is clean & skipped, only b2 runs -> G_B becomes clean
        calls.clear()
        r_gb = await PromptGraphRun(r_b1["graph"], chat_fn, target_type="group", target_id="G_B").run()
        self.assertEqual(calls, ["b2"])
        self.assertEqual(r_gb["node_state"]["b1"]["status"], "reused")
        chg3 = compute_change_status(r_gb["graph"])
        self.assertEqual(chg3["nodes"]["b1"]["state"], "clean")
        self.assertEqual(chg3["nodes"]["b2"]["state"], "clean")
        self.assertEqual(chg3["groups"]["G_B"]["state"], "clean")
        self.assertEqual(chg3["groups"]["G_A"]["state"], "clean")
        self.assertEqual(chg3["nodes"]["a1"]["state"], "clean")

    async def test_agent_mcp_selective_needs_rerun_marking(self):
        # A -> B, C -> B (all clean completed), unrelated U.
        # Edit B.prompt and run B with auto_rerun_needs_rerun=False.
        # Agent inspects A and C via MCP, tries marking unrelated U (ACCESS_DENIED), and marks ONLY A!
        graph = validate_prompt_graph(
            {
                "nodes": [
                    _node("B", prompt="b-v1", output="b-old", completed=True),
                    _node("A", prompt="a-p", output="a-old", completed=True),
                    _node("C", prompt="c-p", output="c-old", completed=True),
                    _node("U", prompt="u-p", output="u-old", completed=True),
                ],
                "groups": [],
                "edges": [{"from": "A", "to": "B"}, {"from": "C", "to": "B"}],
                "settings": {"auto_rerun_needs_rerun": False},
            }
        )
        edited = deepcopy(graph)
        edited["nodes"][0]["prompt"] = "b-v2"

        seen_impact: dict = {}

        async def chat_fn(*, model, messages, tools, node, extra):
            tool_names = {t["function"]["name"] for t in tools}
            if "mark_needs_rerun" not in tool_names:
                return _reply("b-new-output")

            tmsgs = [m for m in messages if m["role"] == "tool"]
            if len(tmsgs) == 0:
                return _reply(
                    tool_calls=[
                        _tool_call("list_dependents", {}, "imp_1"),
                        _tool_call("get_dependent_info", {"entity_id": "A"}, "imp_2"),
                        _tool_call("get_dependent_info", {"entity_id": "C"}, "imp_3"),
                    ]
                )
            if len(tmsgs) == 3:
                seen_impact["list"] = json.loads(tmsgs[0]["content"])
                seen_impact["A"] = json.loads(tmsgs[1]["content"])
                seen_impact["C"] = json.loads(tmsgs[2]["content"])
                return _reply(
                    tool_calls=[
                        _tool_call("mark_needs_rerun", {"entity_id": "U", "reason": "should fail"}, "imp_4"),
                        _tool_call("mark_needs_rerun", {"entity_id": "A", "reason": "A relies on B metric"}, "imp_5"),
                    ]
                )
            seen_impact["mark_U"] = json.loads(tmsgs[3]["content"])
            seen_impact["mark_A"] = json.loads(tmsgs[4]["content"])
            return _reply("Marked A only")

        res = await PromptGraphRun(edited, chat_fn, target_type="node", target_id="B").run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual({d["id"] for d in seen_impact["list"]["direct_dependents"]}, {"A", "C"})
        self.assertEqual(seen_impact["A"]["prompt"], "a-p")
        self.assertEqual(seen_impact["C"]["prompt"], "c-p")
        self.assertEqual(seen_impact["mark_U"]["error"], "ACCESS_DENIED")
        self.assertEqual(seen_impact["mark_A"]["marked"], ["A"])

        chg = compute_change_status(res["graph"])["nodes"]
        self.assertEqual(chg["B"]["state"], "clean")
        self.assertEqual(chg["A"]["state"], "needs_rerun")
        self.assertEqual(chg["A"]["rerun_reason"], "A relies on B metric")
        self.assertEqual(chg["C"]["state"], "clean")
        self.assertEqual(chg["U"]["state"], "clean")
        self.assertEqual(res["result"]["auto_rerun"], [])

    async def test_auto_rerun_needs_rerun_setting_executes_marked_chain_and_stops_when_unmarked(self):
        # Z -> A -> B (all clean completed), auto_rerun_needs_rerun=True.
        # Edit B.prompt, run all: B updates -> marks A -> A auto-reruns and reads B's new output -> inspects Z & leaves Z clean!
        graph = validate_prompt_graph(
            {
                "nodes": [
                    _node("B", prompt="b-v1", output="b-old", completed=True),
                    _node("A", prompt="a-p", output="a-old", completed=True),
                    _node("Z", prompt="z-p", output="z-old", completed=True),
                ],
                "groups": [],
                "edges": [{"from": "A", "to": "B"}, {"from": "Z", "to": "A"}],
                "settings": {"auto_rerun_needs_rerun": True},
            }
        )
        edited = deepcopy(graph)
        edited["nodes"][0]["prompt"] = "b-v2"

        exec_calls: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            tool_names = {t["function"]["name"] for t in tools}
            tmsgs = [m for m in messages if m["role"] == "tool"]

            if "mark_needs_rerun" in tool_names:
                if nid == "B":
                    if len(tmsgs) == 0:
                        return _reply(tool_calls=[_tool_call("mark_needs_rerun", {"entity_id": "A", "reason": "B changed"}, "m_a")])
                    return _reply("marked A")
                # A inspects Z and chooses NOT to mark Z
                return _reply("Z unaffected")

            exec_calls.append(nid)
            if nid == "B":
                return _reply("b-v2-out")
            if nid == "A":
                if not tmsgs:
                    return _reply(tool_calls=[_tool_call("get_node_info", {"node_id": "B"}, "r_b")])
                b_info = json.loads(tmsgs[0]["content"])
                return _reply(f"A-updated:{b_info['output']}")
            raise AssertionError(f"Unexpected execution of {nid}")

        res = await PromptGraphRun(edited, chat_fn, target_type="all").run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(res["result"]["auto_rerun"], ["A"])
        self.assertEqual(res["result"]["outputs"]["B"], "b-v2-out")
        self.assertEqual(res["result"]["outputs"]["A"], "A-updated:b-v2-out")
        self.assertEqual(res["result"]["outputs"]["Z"], "z-old")
        self.assertNotIn("Z", exec_calls)
        chg = compute_change_status(res["graph"])["nodes"]
        self.assertEqual({k: chg[k]["state"] for k in ("B", "A", "Z")}, {"B": "clean", "A": "clean", "Z": "clean"})

    async def test_auto_rerun_group_dependency_waits_for_all_siblings(self):
        # G_A(a1) -> G_B(b1, b2), all clean completed, auto_rerun_needs_rerun=True.
        # Edit G_B.pre_prompt and run G_B -> b1 finishes faster and marks G_A via MCP;
        # a1 must wait until BOTH b1 and b2 finish before executing!
        graph = validate_prompt_graph(
            {
                "nodes": [
                    _node("b1", prompt="b1-p", output="b1-o", parent_group_id="G_B", completed=True),
                    _node("b2", prompt="b2-p", output="b2-o", parent_group_id="G_B", completed=True),
                    _node("a1", prompt="a1-p", output="a1-o", parent_group_id="G_A", completed=True),
                ],
                "groups": [
                    _group("G_B", pre_prompt="B_PRE_1", completed=True),
                    _group("G_A", pre_prompt="A_PRE_1", completed=True),
                ],
                "edges": [{"from": "G_A", "to": "G_B"}],
                "settings": {"parallelism": 4, "auto_rerun_needs_rerun": True},
            }
        )
        edited = deepcopy(graph)
        edited["groups"][0]["pre_prompt"] = "B_PRE_2"

        order: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            tool_names = {t["function"]["name"] for t in tools}
            tmsgs = [m for m in messages if m["role"] == "tool"]

            if "mark_needs_rerun" in tool_names:
                if nid == "b1" and len(tmsgs) == 0:
                    return _reply(tool_calls=[_tool_call("mark_needs_rerun", {"entity_id": "G_A", "reason": "b1 impact"}, "mark_ga")])
                return _reply("done impact")

            if nid == "b2":
                await asyncio.sleep(0.05)
            order.append(f"exec:{nid}")
            return _reply(f"out:{nid}")

        res = await PromptGraphRun(edited, chat_fn, target_type="group", target_id="G_B").run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(res["result"]["auto_rerun"], ["a1"])
        self.assertIn("exec:b1", order)
        self.assertIn("exec:b2", order)
        self.assertIn("exec:a1", order)
        self.assertGreater(order.index("exec:a1"), order.index("exec:b1"))
        self.assertGreater(order.index("exec:a1"), order.index("exec:b2"))
        chg = compute_change_status(res["graph"])
        self.assertEqual(chg["nodes"]["a1"]["state"], "clean")
        self.assertEqual(chg["groups"]["G_A"]["state"], "clean")
        self.assertEqual(chg["groups"]["G_B"]["state"], "clean")

    async def test_deep_multi_tier_groups_5_levels_execution_and_mcp(self):
        # 5-tier hierarchy on both sides: G5_A -> ... -> G1_A(a) depends on G5_B -> ... -> G1_B(b)
        groups = [
            _group("G5_B", pre_prompt="PRE_B5"),
            _group("G4_B", pre_prompt="PRE_B4", parent_group_id="G5_B"),
            _group("G3_B", pre_prompt="PRE_B3", parent_group_id="G4_B"),
            _group("G2_B", pre_prompt="PRE_B2", parent_group_id="G3_B"),
            _group("G1_B", pre_prompt="PRE_B1", parent_group_id="G2_B"),
            _group("G5_A", pre_prompt="PRE_A5"),
            _group("G4_A", pre_prompt="PRE_A4", parent_group_id="G5_A"),
            _group("G3_A", pre_prompt="PRE_A3", parent_group_id="G4_A"),
            _group("G2_A", pre_prompt="PRE_A2", parent_group_id="G3_A"),
            _group("G1_A", pre_prompt="PRE_A1", parent_group_id="G2_A"),
        ]
        nodes = [
            _node("leaf_b", prompt="work B", parent_group_id="G1_B"),
            _node("leaf_a", prompt="work A using G5_B", parent_group_id="G1_A"),
        ]
        graph = validate_prompt_graph({"nodes": nodes, "groups": groups, "edges": [{"from": "G5_A", "to": "G5_B"}]})

        for tier, gid in [(5, "G5_B"), (4, "G4_B"), (3, "G3_B"), (2, "G2_B"), (1, "G1_B"), (5, "G5_A"), (1, "G1_A")]:
            self.assertEqual(group_tier(graph, gid), tier)

        seen_sys: dict[str, str] = {}
        mcp_g5_b: dict = {}

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            seen_sys[nid] = messages[0]["content"]
            if nid == "leaf_b":
                return _reply("LEAF_B_OUT")
            tmsgs = [m for m in messages if m["role"] == "tool"]
            if not tmsgs:
                return _reply(tool_calls=[_tool_call("get_group_info", {"group_id": "G5_B", "recursive": True})])
            nonlocal mcp_g5_b
            mcp_g5_b = json.loads(tmsgs[0]["content"])
            return _reply("LEAF_A_OUT")

        res = await PromptGraphRun(graph, chat_fn, target_type="group", target_id="G5_A").run()
        self.assertEqual(res["status"], "SUCCEEDED")
        # All 5 pre-prompts appear outermost -> innermost
        for p_outer, p_inner in [("PRE_A5", "PRE_A4"), ("PRE_A4", "PRE_A3"), ("PRE_A3", "PRE_A2"), ("PRE_A2", "PRE_A1")]:
            self.assertLess(seen_sys["leaf_a"].index(p_outer), seen_sys["leaf_a"].index(p_inner))
        # MCP recursive group info traverses all 5 tiers
        cur = mcp_g5_b
        for expected_tier in (5, 4, 3, 2, 1):
            self.assertEqual(cur["tier"], expected_tier)
            if expected_tier > 1:
                self.assertEqual(len(cur["child_groups"]), 1)
                cur = cur["child_groups"][0]
        self.assertEqual(cur["nodes"][0]["id"], "leaf_b")
        self.assertEqual(cur["nodes"][0]["output"], "LEAF_B_OUT")
        # All 5 tiers in both branches became completed=True
        for g in res["graph"]["groups"]:
            self.assertTrue(g["completed"])

    async def test_subset_partial_execution_runs_only_selected_branches(self):
        graph = validate_prompt_graph(
            {
                "nodes": [
                    _node("X", prompt="run X"),
                    _node("Y", prompt="run Y"),
                    _node("Z", prompt="skip Z"),
                ],
                "groups": [],
                "edges": [],
            }
        )
        calls: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            calls.append(node["id"])
            return _reply(f"out-{node['id']}")

        res = await PromptGraphRun(graph, chat_fn, target_type="subset", target_ids=["X", "Y"]).run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(set(calls), {"X", "Y"})
        self.assertEqual(res["node_state"]["Z"]["status"], "idle")


class TestEnhancedFeaturesLoopsContractsAndDB(unittest.IsolatedAsyncioTestCase):
    def test_conditional_loop_schema_and_cycle_rules(self):
        # Non-loop self-loop still rejected
        with self.assertRaises(PromptGraphError):
            validate_prompt_graph({"nodes": [_node("A")], "groups": [], "edges": [{"from": "A", "to": "A", "is_loop": False}]})

        # Conditional self-loop allowed & non-loop dependencies stays empty
        g_self = validate_prompt_graph({
            "nodes": [_node("A")],
            "groups": [],
            "edges": [{"from": "A", "to": "A", "is_loop": True, "max_iterations": 4, "condition": {"type": "on_contract_fail"}}],
            "settings": {"edge_direction": "execution"},
        })
        self.assertEqual(g_self["nodes"][0]["dependencies"], [])
        self.assertTrue(g_self["edges"][0]["is_loop"])
        self.assertEqual(g_self["edges"][0]["max_iterations"], 4)
        self.assertEqual(g_self["settings"]["edge_direction"], "execution")

        # Multi-node conditional loop allowed alongside forward DAG edge
        g_cyc = validate_prompt_graph({
            "nodes": [_node("Draft"), _node("Critic")],
            "groups": [],
            "edges": [
                {"from": "Critic", "to": "Draft"},
                {"from": "Draft", "to": "Critic", "is_loop": True, "max_iterations": 3, "condition": {"type": "on_contract_fail"}},
            ],
        })
        self.assertEqual({e["id"]: e["is_loop"] for e in g_cyc["edges"]}, {"e1": False, "e2": True})

    async def test_conditional_loop_execution_converges_then_runs_downstream(self):
        graph = pg.preset("conditional_loop_review")
        seen_order: list[str] = []
        draft_calls = [0]

        async def chat_fn(*, model, messages, tools, node, extra):
            nid = node["id"]
            seen_order.append(nid)
            if nid == "draft":
                draft_calls[0] += 1
                user_txt = "\n".join(m["content"] for m in messages if m["role"] == "user")
                if draft_calls[0] == 1:
                    return _reply("초안 v1")
                self.assertIn("조건부 재귀/반복 실행 피드백", user_txt)
                return _reply("초안 v2 (보안·성능 보완 완료)")
            if nid == "critic":
                if draft_calls[0] == 1:
                    return _reply("REVISE: 보안 근거 부족")
                return _reply("PASS: 모든 검증 항목 충족")
            return _reply("PUBLISH: 최종 승인 완료")

        res = await PromptGraphRun(graph, chat_fn).run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(seen_order, ["draft", "critic", "draft", "critic", "publish"])
        self.assertEqual(res["result"]["loop_counts"].get("e_loop_cd"), 1)
        self.assertEqual(res["result"]["outputs"]["publish"], "PUBLISH: 최종 승인 완료")

    async def test_agent_decide_loop_via_mcp_and_max_iterations_cap(self):
        graph = validate_prompt_graph({
            "nodes": [_node("worker", prompt="work cleanly")],
            "groups": [],
            "edges": [{
                "id": "e_agent",
                "from": "worker",
                "to": "worker",
                "is_loop": True,
                "max_iterations": 2,
                "condition": {"type": "agent_decide"},
            }],
        })
        calls = [0]

        async def chat_fn(*, model, messages, tools, node, extra):
            tmsgs = [m for m in messages if m["role"] == "tool"]
            if not tmsgs:
                calls[0] += 1
                # Always ask to continue -> max_iterations (2) must stop loop after 3 total runs
                return _reply(tool_calls=[_tool_call("signal_loop_decision", {"continue_loop": True, "reason": "need refine"})])
            return _reply(f"round-{calls[0]}")

        res = await PromptGraphRun(graph, chat_fn).run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(calls[0], 3)  # 1 initial + 2 loop iterations
        self.assertEqual(res["result"]["loop_counts"].get("e_agent"), 2)

    async def test_contract_retry_and_conditional_non_loop_branching(self):
        graph = validate_prompt_graph({
            "nodes": [
                _node(
                    "judge",
                    prompt="return json decision",
                    contract={"enabled": True, "json_required": True, "json_keys": ["verdict"], "max_retries": 1},
                ),
                _node("ship", prompt="ship it"),
                _node("abort", prompt="abort it"),
            ],
            "groups": [],
            "edges": [
                {"from": "ship", "to": "judge", "condition": {"type": "json_eq", "value": "verdict=GO"}},
                {"from": "abort", "to": "judge", "condition": {"type": "json_eq", "value": "verdict=STOP"}},
            ],
        })
        judge_attempts = [0]

        async def chat_fn(*, model, messages, tools, node, extra):
            if node["id"] == "judge":
                judge_attempts[0] += 1
                if judge_attempts[0] == 1:
                    return _reply("not json yet")
                return _reply('```json\n{"verdict": "GO"}\n```')
            return _reply(f"ran-{node['id']}")

        res = await PromptGraphRun(graph, chat_fn).run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(judge_attempts[0], 2)
        self.assertEqual(res["node_state"]["ship"]["status"], "done")
        self.assertEqual(res["node_state"]["abort"]["status"], "skipped")

    async def test_cross_graph_cache_and_profile_critical_path(self):
        graph = validate_prompt_graph({
            "nodes": [
                _node("A", prompt="root"),
                _node("B", prompt="branch B"),
                _node("C", prompt="leaf"),
            ],
            "groups": [],
            "edges": [{"from": "C", "to": "A"}],
            "settings": {"cross_graph_cache": True},
        })
        cache_store: dict[str, dict] = {}
        llm_calls: list[str] = []

        async def chat_fn(*, model, messages, tools, node, extra):
            llm_calls.append(node["id"])
            return _reply(f"out-{node['id']}")

        res1 = await PromptGraphRun(
            graph,
            chat_fn,
            on_node_version=lambda v: cache_store.__setitem__(v["input_hash"], v),
        ).run()
        prof = res1["result"]["profile"]
        self.assertEqual(prof["total_tokens"], 210)
        self.assertEqual(set(llm_calls), {"A", "B", "C"})
        self.assertTrue(len(prof["critical_path_nodes"]) >= 1)

        # Second identical graph with completed=False must hit cross-graph hash cache with 0 LLM calls
        llm_calls.clear()
        res2 = await PromptGraphRun(
            graph,
            chat_fn,
            cache_lookup=lambda h: cache_store.get(h),
        ).run()
        self.assertEqual(llm_calls, [])
        self.assertEqual(res2["result"]["profile"]["cache_hit_count"], 3)
        self.assertEqual(res2["result"]["profile"]["total_cost_usd"], 0.0)

    async def test_loop_edge_temp_prompt_reinjects_each_iteration_and_clears_on_node(self):
        graph = validate_prompt_graph({
            "nodes": [
                _node("B", prompt="draft content"),
                _node(
                    "A",
                    prompt="review draft",
                    contract={"enabled": True, "must_include": ["PASS"], "on_fail": "continue"},
                ),
            ],
            "groups": [],
            "edges": [
                {"id": "e_forward", "from": "A", "to": "B"},
                {
                    "id": "e_loop_ba",
                    "from": "B",
                    "to": "A",
                    "is_loop": True,
                    "max_iterations": 3,
                    "condition": {"type": "on_contract_fail"},
                    "temp_prompt": "루프 #{iteration}/{max_iterations} 지시: {source_output}",
                },
            ],
            "settings": {"edge_direction": "execution"},
        })
        b_prompts: list[str] = []
        recorded_versions: list[dict] = []
        a_calls = [0]

        async def chat_fn(*, model, messages, tools, node, extra):
            user_txt = "\n".join(m["content"] for m in messages if m["role"] == "user")
            if node["id"] == "B":
                b_prompts.append(user_txt)
                return _reply(f"draft-attempt-{len(b_prompts)}")
            a_calls[0] += 1
            if a_calls[0] <= 2:
                return _reply(f"FAIL-round-{a_calls[0]}")
            return _reply("PASS-final")

        res = await PromptGraphRun(
            graph,
            chat_fn,
            on_node_version=lambda v: recorded_versions.append(v),
        ).run()
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(len(b_prompts), 3)
        self.assertNotIn("루프 #", b_prompts[0])
        self.assertIn("[임시 프롬프트 (1회성 추가 지시)]\n루프 #1/3 지시: FAIL-round-1", b_prompts[1])
        temp_sec_2 = b_prompts[2].split("[임시 프롬프트 (1회성 추가 지시)]\n", 1)[1].split("\n\n[조건부 재귀", 1)[0]
        self.assertEqual(temp_sec_2, "루프 #2/3 지시: FAIL-round-2")
        self.assertNotIn("FAIL-round-1", temp_sec_2)  # old one-shot prompt cleared cleanly after B2!
        recorded = res["node_state"]["B"]["initial_prompt"]
        self.assertEqual(recorded["iteration"], 3)
        self.assertEqual(recorded["messages"][1]["content"], b_prompts[2])

        b_final = next(n for n in res["graph"]["nodes"] if n["id"] == "B")
        self.assertEqual(b_final["temp_prompt"], "")
        self.assertTrue(b_final["completed"])
        loop_edge_final = next(e for e in res["graph"]["edges"] if e["id"] == "e_loop_ba")
        self.assertIn("루프 #{iteration}/{max_iterations}", loop_edge_final["temp_prompt"])
        self.assertEqual(compute_change_status(res["graph"])["nodes"]["B"]["state"], "clean")

        b_vers = [v for v in recorded_versions if v["node_id"] == "B"]
        self.assertEqual(len(b_vers), 3)
        self.assertEqual(b_vers[0]["temp_prompt"], "")
        self.assertIn("루프 #1/3 지시: FAIL-round-1", b_vers[1]["temp_prompt"])
        self.assertIn("루프 #2/3 지시: FAIL-round-2", b_vers[2]["temp_prompt"])

    async def test_agent_decide_dynamic_loop_temp_prompt_and_forward_edge_reinjection(self):
        # 1) MCP signal_loop_decision dynamic temp_prompt into a group target, plus non-loop edge temp_prompt re-injection
        graph = validate_prompt_graph({
            "nodes": [
                _node("B1", prompt="work inside group", parent_group_id="G_WORK"),
                _node("A", prompt="evaluate G_WORK and decide loop"),
            ],
            "groups": [_group("G_WORK", pre_prompt="base group pre", node_ids=["B1"])],
            "edges": [
                {
                    "id": "e_fwd",
                    "from": "A",
                    "to": "G_WORK",
                    "temp_prompt": "전달된 선행 간선 1회 지시",
                },
                {
                    "id": "e_dyn_loop",
                    "from": "G_WORK",
                    "to": "A",
                    "is_loop": True,
                    "max_iterations": 2,
                    "condition": {"type": "agent_decide"},
                },
            ],
        })
        b1_sys_prompts: list[str] = []
        a_user_prompts: list[str] = []
        a_turns = [0]

        async def chat_fn(*, model, messages, tools, node, extra):
            if node["id"] == "B1":
                b1_sys_prompts.append("\n".join(m["content"] for m in messages if m["role"] == "system"))
                return _reply(f"B1-iter-{len(b1_sys_prompts)}")
            a_user_prompts.append("\n".join(m["content"] for m in messages if m["role"] == "user"))
            tmsgs = [m for m in messages if m["role"] == "tool"]
            if not tmsgs:
                a_turns[0] += 1
                if a_turns[0] == 1:
                    return _reply(tool_calls=[_tool_call("signal_loop_decision", {
                        "continue_loop": True,
                        "reason": "보완 필요",
                        "temp_prompt": "에이전트 동적 1회 지시: 예외 케이스 포함",
                    })])
                return _reply(tool_calls=[_tool_call("signal_loop_decision", {
                    "continue_loop": False,
                    "reason": "충분함",
                })])
            return _reply(f"A-done-{a_turns[0]}")

        res1 = await PromptGraphRun(graph, chat_fn).run()
        self.assertEqual(res1["status"], "SUCCEEDED")
        self.assertEqual(len(b1_sys_prompts), 2)
        self.assertNotIn("에이전트 동적 1회 지시", b1_sys_prompts[0])
        self.assertIn("에이전트 동적 1회 지시: 예외 케이스 포함", b1_sys_prompts[1])
        self.assertEqual(len(a_user_prompts), 4)  # 2 iterations * 2 LLM turns each (tool call + final)
        self.assertTrue(all("전달된 선행 간선 1회 지시" in p for p in a_user_prompts))

        # Target node/group temporary prompts are cleared after run, while edge.temp_prompt persists for future runs
        g_after = res1["graph"]["groups"][0]
        a_after = next(n for n in res1["graph"]["nodes"] if n["id"] == "A")
        self.assertEqual(g_after["temp_pre_prompt"], "")
        self.assertEqual(a_after["temp_prompt"], "")
        self.assertEqual(next(e for e in res1["graph"]["edges"] if e["id"] == "e_fwd")["temp_prompt"], "전달된 선행 간선 1회 지시")

        # Re-running the saved graph with force_rerun=True must re-inject the forward edge temp_prompt again!
        a_user_prompts.clear()
        a_turns[0] = 0
        res2 = await PromptGraphRun(res1["graph"], chat_fn, force_rerun=True).run()
        self.assertEqual(res2["status"], "SUCCEEDED")
        self.assertIn("전달된 선행 간선 1회 지시", a_user_prompts[0])
        self.assertEqual(next(n for n in res2["graph"]["nodes"] if n["id"] == "A")["temp_prompt"], "")

    def test_auto_layout_guarantees_zero_overlap_on_deep_nested_groups_and_cycles(self):
        # Intentionally place every node and 4-tier nested group at overlapping (0, 0) coordinates
        messy = {
            "nodes": [
                _node("n_research_1", x=0, y=0, width=250, height=140, parent_group_id="g_tier1"),
                _node("n_research_2", x=0, y=0, width=250, height=140, parent_group_id="g_tier1"),
                _node("n_spec", x=0, y=0, width=250, height=140, parent_group_id="g_tier2"),
                _node("n_review", x=0, y=0, width=250, height=140, parent_group_id="g_review"),
                _node("n_release", x=0, y=0, width=250, height=140),
            ],
            "groups": [
                _group("g_tier4", x=0, y=0, width=300, height=200),
                _group("g_tier3", x=0, y=0, width=300, height=200, parent_group_id="g_tier4"),
                _group("g_tier2", x=0, y=0, width=300, height=200, parent_group_id="g_tier3"),
                _group("g_tier1", x=0, y=0, width=300, height=200, parent_group_id="g_tier2"),
                _group("g_review", x=0, y=0, width=300, height=200),
                _group("g_empty", x=0, y=0, width=260, height=160),
            ],
            "edges": [
                {"from": "n_spec", "to": "g_tier1"},
                {"from": "g_review", "to": "g_tier4"},
                {"from": "n_spec", "to": "g_review", "is_loop": True, "max_iterations": 3, "condition": {"type": "on_contract_fail"}},
                {"from": "n_release", "to": "g_review", "condition": {"type": "on_contract_pass"}},
            ],
            "settings": {"edge_direction": "execution"},
        }
        before_rep = pg.detect_layout_overlaps(messy)
        self.assertFalse(before_rep["ok"])

        laid_out = pg.auto_layout_graph(messy, direction="execution")
        after_rep = pg.detect_layout_overlaps(laid_out)
        self.assertTrue(after_rep["ok"], f"Unexpected layout issues: {after_rep}")
        self.assertEqual(after_rep["overlap_pairs"], [])
        self.assertEqual(after_rep["containment_violations"], [])
        self.assertEqual(after_rep["top_clips"], [])

        by_id = {x["id"]: x for x in laid_out["nodes"] + laid_out["groups"]}
        # Execution order: g_tier4 -> g_review -> n_release strictly left-to-right
        self.assertLess(by_id["g_tier4"]["x"] + by_id["g_tier4"]["width"], by_id["g_review"]["x"])
        self.assertLess(by_id["g_review"]["x"] + by_id["g_review"]["width"], by_id["n_release"]["x"])
        # Dependency order also produces zero overlaps with reversed horizontal ordering
        dep_laid = pg.auto_layout_graph(messy, direction="dependency")
        self.assertTrue(pg.detect_layout_overlaps(dep_laid)["ok"])

    async def test_admin_mcp_server_creates_nested_groups_edges_loops_and_stays_overlap_free(self):
        from mcp import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams

        state = {"graph": validate_prompt_graph({"nodes": [], "groups": [], "edges": [], "settings": {"edge_direction": "execution"}})}
        server = pg.make_prompt_graph_admin_mcp_server(
            lambda: state["graph"],
            lambda g: state.__setitem__("graph", g),
        )

        async def _call(session, name, arguments=None):
            resp = await session.call_tool(name, arguments or {})
            return json.loads("\n".join(b.text for b in resp.content))

        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with __import__("anyio").create_task_group() as tg:
                tg.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    ov0 = await _call(session, "get_graph_overview")
                    self.assertEqual(ov0["recommended_stage"], "early_simple")

                    n1 = await _call(session, "create_node", {
                        "id": "draft", "label": "초안", "prompt": "write draft", "shape": "circle", "width": 180,
                    })
                    self.assertTrue(n1["ok"] and n1["layout_report"]["ok"])
                    self.assertEqual((n1["node"]["shape"], n1["node"]["width"], n1["node"]["height"]), ("circle", 180, 180))
                    n2 = await _call(session, "create_node", {"id": "critic", "label": "검토", "prompt": "review draft"})
                    self.assertTrue(n2["ok"] and n2["layout_report"]["ok"])
                    self.assertEqual(n2["node"]["shape"], "rounded")
                    resized = await _call(session, "update_node", {
                        "node_id": "critic", "shape": "ellipse", "width": 170, "height": 95,
                    })
                    self.assertTrue(resized["ok"])
                    n3 = await _call(session, "create_node", {"id": "pub", "label": "배포", "prompt": "publish"})
                    self.assertTrue(n3["ok"] and n3["layout_report"]["ok"])

                    g1 = await _call(session, "create_group", {
                        "id": "grp_inner",
                        "label": "1-그룹 작업",
                        "pre_prompt": "한국어",
                        "adopt_entity_ids": ["draft", "critic"],
                    })
                    self.assertTrue(g1["ok"] and g1["tier"] == 1 and g1["layout_report"]["ok"])
                    g2 = await _call(session, "create_group", {
                        "id": "grp_outer",
                        "label": "2-그룹 품질",
                        "pre_prompt": "엄격 검증",
                        "adopt_entity_ids": ["grp_inner"],
                    })
                    self.assertTrue(g2["ok"] and g2["tier"] == 2 and g2["layout_report"]["ok"])

                    e_fwd = await _call(session, "add_edge", {"from": "critic", "to": "draft"})
                    self.assertTrue(e_fwd["ok"] and e_fwd["layout_report"]["ok"])
                    # Attempting an invalid non-loop cycle must fail cleanly and NOT corrupt graph state
                    bad_cyc = await _call(session, "add_edge", {"from": "draft", "to": "critic", "is_loop": False})
                    self.assertFalse(bad_cyc["ok"])
                    self.assertIn("cycle", bad_cyc["error"].lower())

                    # Conditional loop edge succeeds with temp_prompt and clean layout
                    e_loop = await _call(session, "add_edge", {
                        "from": "draft",
                        "to": "critic",
                        "is_loop": True,
                        "max_iterations": 3,
                        "condition": {"type": "on_contract_fail"},
                        "temp_prompt": "반려 사유 반영 #{iteration}",
                    })
                    self.assertTrue(e_loop["ok"] and e_loop["layout_report"]["ok"])
                    e_out = await _call(session, "add_edge", {"from": "pub", "to": "grp_outer"})
                    self.assertTrue(e_out["ok"] and e_out["layout_report"]["ok"])

                    rep_final = pg.detect_layout_overlaps(state["graph"])
                    self.assertTrue(rep_final["ok"], f"MCP mutations caused layout overlap: {rep_final}")
                    critic = next(n for n in state["graph"]["nodes"] if n["id"] == "critic")
                    self.assertEqual((critic["shape"], critic["width"], critic["height"]), ("ellipse", 170, 95))
                tg.cancel_scope.cancel()

    async def test_manage_prompt_graph_with_mcp_agent_and_api_routes(self):
        initial = validate_prompt_graph({
            "nodes": [_node("n1", x=50, y=50), _node("n2", x=55, y=55)],
            "groups": [],
            "edges": [],
        })
        step = [0]

        async def agent_chat(*, model, messages, tools, node, extra):
            step[0] += 1
            if step[0] == 1:
                self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
                self.assertEqual(messages[1]["content"], "두 노드를 검토해줘")
                return _reply(tool_calls=[_tool_call("create_group", {"id": "g_pipe", "label": "파이프라인", "adopt_entity_ids": ["n1", "n2"]})])
            if step[0] == 2:
                return _reply(tool_calls=[_tool_call("add_edge", {"from": "n2", "to": "n1"})])
            return _reply("그룹 생성 및 의존 간선 배치 완료 (겹침 0건)")

        out = await pg.manage_prompt_graph_with_mcp(
            initial,
            "두 노드를 그룹으로 묶고 n2가 n1에 의존하도록 정리해줘",
            agent_chat,
            history=[
                {"role": "user", "content": "두 노드를 검토해줘"},
                {"role": "assistant", "content": "두 노드의 구성을 확인했습니다."},
                {"role": "system", "content": "ignore"},
            ],
        )
        self.assertTrue(out["changed"])
        self.assertTrue(out["layout_report"]["ok"])
        self.assertEqual(len(out["graph"]["groups"]), 1)
        self.assertEqual(out["graph"]["edges"][0]["from"], "n2")


if __name__ == "__main__":
    unittest.main()
