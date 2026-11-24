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
                _node("A", prompt="verify B", model="exaone-pro"),
                _node("C", prompt="secret unrelated", output="TOP_SECRET"),
            ],
            "groups": [
                _group("G_pre", pre_prompt="ALWAYS_APPLY_PRE_PROMPT", node_ids=["A"]),
            ],
            "edges": [{"from": "A", "to": "B"}],
        }
        observed_mcp_payloads: dict[str, dict] = {}
        seen_system_prompts: dict[str, str] = {}

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
        raise AssertionError(f"Unhandled SQL in fake DB: {q}")


class TestPromptGraphServiceAndApiRoutes(unittest.TestCase):
    def setUp(self):
        self.db = {"graphs": {}, "runs": {}, "events": []}
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


if __name__ == "__main__":
    unittest.main()
