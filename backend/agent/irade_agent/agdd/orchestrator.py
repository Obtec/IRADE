"""JSON graph interpreter + SKIP LOCKED queue protocol (DOC.md §2, §4.1, §5.2).

The DB layer is abstracted behind SQL constants executed by the caller's
PostgreSQL driver (psycopg style ``%(name)s`` params), following the 3-step
short-transaction rule: claim+commit -> run without connection -> optimistic commit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .spec_engine import SpecEngine

NODE_KINDS = {"SPEC", "AMBIGUITY_GATE", "DEV", "SEMANTIC_VERIFY", "VERIFY", "AUDIT", "HITL"}


class GraphError(ValueError):
    pass


def validate_graph(graph: dict) -> None:
    """Structural JSON-schema-equivalent validation (only valid graphs get published)."""
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise GraphError("graph must have nodes[] and edges[]")
    names = set()
    for n in graph["nodes"]:
        if not isinstance(n, dict) or not isinstance(n.get("name"), str) or not n["name"]:
            raise GraphError("node.name required")
        if n["name"] in names:
            raise GraphError(f"duplicate node {n['name']}")
        if n.get("kind") not in NODE_KINDS:
            raise GraphError(f"node {n['name']}: kind must be one of {sorted(NODE_KINDS)}")
        if not isinstance(n.get("allowed_mcp", []), list):
            raise GraphError(f"node {n['name']}: allowed_mcp must be a list")
        if "max_retries" in n and (not isinstance(n["max_retries"], int) or n["max_retries"] < 0):
            raise GraphError(f"node {n['name']}: max_retries must be int >= 0")
        names.add(n["name"])
    if graph.get("entry") not in names:
        raise GraphError("entry must reference a node")
    for e in graph["edges"]:
        if e.get("from") not in names or e.get("to") not in names:
            raise GraphError(f"edge {e} references unknown node")
        if e.get("on", "success") not in ("success", "failure"):
            raise GraphError(f"edge {e}: on must be success|failure")


@dataclass
class NodeInput:
    """Node entry resets history: input = fixed prefix + previous handoff + SQL-narrowed scope only."""
    node: str
    handoff: dict
    scope: dict
    allowed_mcp: list[str]


class GraphRun:
    def __init__(self, graph: dict, spec: SpecEngine):
        validate_graph(graph)
        self.graph = graph
        self.nodes = {n["name"]: n for n in graph["nodes"]}
        self.spec = spec
        self.attempts: dict[str, int] = {}

    def node_input(self, node: str, handoff: dict) -> NodeInput:
        n = self.nodes[node]
        return NodeInput(node, handoff, self.spec.executable_scope(n.get("domain")), list(n.get("allowed_mcp", [])))

    def next_node(self, node: str, success: bool) -> str | None:
        on = "success" if success else "failure"
        if not success:
            self.attempts[node] = self.attempts.get(node, 0) + 1
            if self.attempts[node] > self.nodes[node].get("max_retries", 2):
                return None  # escalate (HITL / advisor)
        for e in self.graph["edges"]:
            if e["from"] == node and e.get("on", "success") == on:
                return e["to"]
        return None

    def run(self, execute: Callable[[NodeInput], tuple[bool, dict]], max_steps: int = 100) -> dict:
        node, handoff, trail = self.graph["entry"], {}, []
        for _ in range(max_steps):
            ok, handoff = execute(self.node_input(node, handoff))
            trail.append((node, ok))
            nxt = self.next_node(node, ok)
            if nxt is None:
                status = "SUCCEEDED" if ok else "ESCALATED"
                return {"status": status, "trail": trail, "handoff": handoff, "last_node": node}
            node = nxt
        return {"status": "STEP_LIMIT", "trail": trail, "handoff": handoff, "last_node": node}


# ---------------- PostgreSQL queue protocol ----------------
CLAIM_JOB_SQL = """
WITH next AS (
  SELECT job_id FROM job_queue
   WHERE status = 'PENDING' AND available_at <= now()
   ORDER BY priority, available_at
   LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE job_queue q SET status = 'RUNNING', attempt_no = q.attempt_no + 1,
       lease_expires_at = now() + %(lease)s::interval
  FROM next WHERE q.job_id = next.job_id
RETURNING q.job_id, q.run_id, q.node_name, q.attempt_no;
"""

COMMIT_RUN_SQL = """
UPDATE workflow_runs SET current_node = %(next_node)s, status = %(status)s,
       handoff_json = %(handoff)s::jsonb, version = version + 1, updated_at = now()
 WHERE run_id = %(run_id)s AND version = %(version)s
RETURNING version;
"""

FINISH_JOB_SQL = "UPDATE job_queue SET status = %(status)s, lease_expires_at = NULL WHERE job_id = %(job_id)s;"

ENQUEUE_SQL = """
INSERT INTO job_queue (run_id, node_name, status, priority, available_at)
VALUES (%(run_id)s, %(node_name)s, 'PENDING', %(priority)s, now());
"""

REAP_EXPIRED_SQL = """
UPDATE job_queue SET status = 'PENDING', lease_expires_at = NULL
 WHERE status = 'RUNNING' AND lease_expires_at < now();
"""


def handoff_json(handoff: dict) -> str:
    return json.dumps(handoff, ensure_ascii=False, sort_keys=True)
