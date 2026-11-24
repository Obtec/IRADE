"""Standalone stdio MCP server for automatic prompt graph management.

Usage:
    python -m irade_agent.agdd.prompt_graph_mcp_cli --workspace <ws-uuid> --graph-id <graph-uuid>
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

from mcp.server.stdio import stdio_server

from . import infra, prompt_graph_service
from .prompt_graph import make_prompt_graph_admin_mcp_server


def _load_graph_row(ws: str, gid: str) -> tuple[str, dict[str, Any]]:
    with infra.pg(readonly=True) as c:
        row = c.execute(
            "SELECT name, graph_json FROM prompt_graphs WHERE graph_id=%s AND workspace_id=%s",
            (gid, ws),
        ).fetchone()
    if row is None:
        raise LookupError(f"prompt graph {gid!r} not found in workspace {ws!r}")
    return str(row["name"] or "Prompt Graph"), dict(row["graph_json"] or {})


async def serve_stdio(ws: str, gid: str) -> None:
    name, initial_graph = _load_graph_row(ws, gid)
    state: dict[str, Any] = {"name": name, "graph": initial_graph}

    def accessor() -> dict[str, Any]:
        return state["graph"]

    def mutator(new_graph: dict[str, Any]) -> None:
        saved = prompt_graph_service.save_graph(ws, gid, state["name"], new_graph, None)
        state["graph"] = saved["graph_json"]
        state["name"] = saved["name"]

    server = make_prompt_graph_admin_mcp_server(accessor, mutator)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IRADE Prompt Graph Admin MCP Server (stdio)")
    parser.add_argument("--workspace", required=True, help="Workspace UUID")
    parser.add_argument("--graph-id", required=True, help="Target prompt graph UUID")
    args = parser.parse_args(argv)
    asyncio.run(serve_stdio(args.workspace, args.graph_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
