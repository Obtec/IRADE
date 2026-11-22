"""Multi-agent teams orchestrated from a visual canvas graph.

A *team graph* is what the console's multi-agent canvas edits::

    {"nodes": [...], "edges": [...], "settings": {...}}

Node kinds
  input   the user request entering the team, plus optional default attachments (images, video, audio, PDF …)
  data    a data source resolved once at run start (spec scope, 2D UX layout, repo files, cells, text, files …)
  agent   a CLI/LLM agent: role, default system prompt, prompt template, which input kinds and media modalities
          it accepts, wait-for-all or react-to-any inputs, think (JSON answer) or code (autonomous run in the repo)
  output  a sink that collects the team's final results

Edges carry data from ``from`` to ``to``. ``when`` selects the orchestration semantics:
  always   fires on every completion
  approve  fires when the agent approves (or gives no verdict)
  revise   fires when the agent asks for a revision; the review/feedback loop edge
  handoff  fires only when the agent hands the conversation to that target (``handoff`` field)
  spawn    orchestrator -> worker: the orchestrator assigns ``tasks`` that run on the worker in parallel and
           whose results come back to the orchestrator as one ``subtask`` input (dynamic fan-out / fan-in)

These cover the multi-agent structures used in practice:
  - Anthropic: prompt chaining, routing, parallelization (sectioning / voting), orchestrator-workers
    (lead agent spawning sub-agents), evaluator-optimizer loops
  - OpenAI Agents SDK: handoffs between agents, agents-as-tools (spawn), input/output guardrails with tripwires
  - Google ADK: Sequential / Parallel / Loop workflow agents, LLM transfer (handoff), shared session state
    written through ``output_key`` and read with ``{{state:key}}`` templates

Agents also collaborate directly: an agent may address messages to teammates (``messages`` in think mode,
``<<TO:id>>…<</TO>>`` markers in code mode). A delivered message re-activates the recipient, so agents can
ask each other questions and answer them, bounded by per-node ``max_runs`` and the team ``max_steps``.

Multimodal inputs: input and ``files`` data nodes carry attachments (uploaded files). Every item that reaches an
agent keeps its attachments; an agent only sees the modalities it accepts (``modalities``). The scheduler is
media-agnostic: :class:`MediaHooks` (injected) looks attachments up, prepares them once per run (e.g. copies
them into the VM and extracts PDF text / video frames / audio transcripts) and describes them in the prompt.

The scheduler (:class:`TeamRun`) is pure Python: model calls, data lookups and media handling are injected, so
it is unit tested without a VM or database. ``multi_agent_service`` wires it to the VM CLIs and PostgreSQL.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

NODE_KINDS = ("input", "data", "agent", "output")
EDGE_WHEN = ("always", "approve", "revise", "handoff", "spawn")
INPUT_KINDS = ("request", "data", "agent", "feedback", "message")
MODALITIES = ("text", "image", "video", "audio", "pdf", "file")
AGENT_MODES = ("think", "code", "mock")
DATA_SOURCES = ("spec_scope", "ux_layout", "repo_files", "cells", "collab_history", "text", "files")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
CLIS = ("", "claude", "codex", "agy")
VERDICTS = ("approve", "revise", "none")

MAX_NODES, MAX_EDGES = 40, 120
MAX_MESSAGES_PER_TURN, MAX_MESSAGE_CHARS = 5, 4000
MAX_INPUT_CHARS = 16000
MAX_TASKS_PER_TURN = 8
MAX_ATTACHMENTS = 16

_ID_RX = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_MODEL_RX = re.compile(r"[A-Za-z0-9._:/\[\]-]{1,80}")
_VIEW_RX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_PATH_RX = re.compile(r"[A-Za-z0-9_./-]{1,200}")
_KEY_RX = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TEXT_MIMES = ("application/json", "application/xml", "application/x-yaml", "application/yaml", "application/javascript",
               "application/x-sh", "application/sql", "application/toml", "application/x-ndjson")
_TEXT_EXT = (".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".log", ".py",
             ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
             ".sh", ".sql", ".html", ".css", ".tex", ".ipynb", ".srt", ".vtt")


def modality_of(mime: str, name: str = "") -> str:
    """Media modality of an attachment, used for routing (``modalities``) and per-CLI delivery."""
    mime, low = str(mime or "").lower().split(";")[0].strip(), str(name or "").lower()
    if mime == "application/pdf" or low.endswith(".pdf"):
        return "pdf"
    for prefix in ("image", "video", "audio"):
        if mime.startswith(prefix + "/"):
            return prefix
    if mime.startswith("text/") or mime in _TEXT_MIMES or low.endswith(_TEXT_EXT):
        return "text"
    return "file"


class TeamGraphError(ValueError):
    pass


# ---------------------------------------------------------------- validation
def _s(v, limit: int, default: str = "") -> str:
    return str(v if v is not None else default).strip()[:limit]


def _int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _norm_agent(n: dict, out: dict) -> None:
    out["role"] = _s(n.get("role"), 300)
    out["system_prompt"] = _s(n.get("system_prompt"), 8000)
    out["prompt_template"] = _s(n.get("prompt_template"), 8000)
    accepts = n.get("accepts", list(INPUT_KINDS))
    if not isinstance(accepts, list) or any(a not in INPUT_KINDS for a in accepts):
        raise TeamGraphError(f"agent {out['id']}: accepts must be a subset of {list(INPUT_KINDS)}")
    out["accepts"] = [k for k in INPUT_KINDS if k in accepts]
    out["input_mode"] = n.get("input_mode", "all")
    if out["input_mode"] not in ("all", "any"):
        raise TeamGraphError(f"agent {out['id']}: input_mode must be all|any")
    out["mode"] = n.get("mode", "think")
    if out["mode"] not in AGENT_MODES:
        raise TeamGraphError(f"agent {out['id']}: mode must be one of {list(AGENT_MODES)}")
    if out["mode"] == "mock":
        responses = n.get("mock_responses", [{"content": f"{out['label']} 모의 응답"}])
        if (not isinstance(responses, list) or not 1 <= len(responses) <= 10
                or any(not isinstance(r, dict) or not isinstance(r.get("content"), str) for r in responses)
                or len(json.dumps(responses, ensure_ascii=False)) > 16000):
            raise TeamGraphError(f"agent {out['id']}: mock_responses must contain 1–10 response objects "
                                 "with string content (at most 16000 characters total)")
        out["mock_responses"] = deepcopy(responses)
    out["cli"] = n.get("cli") or ""
    if out["cli"] not in CLIS:
        raise TeamGraphError(f"agent {out['id']}: cli must be one of {[c for c in CLIS if c]} or empty")
    out["model"] = _s(n.get("model"), 80)
    if out["model"] and not _MODEL_RX.fullmatch(out["model"]):
        raise TeamGraphError(f"agent {out['id']}: invalid model name")
    out["effort"] = n.get("effort") or ""
    if out["effort"] not in EFFORTS:
        raise TeamGraphError(f"agent {out['id']}: effort must be one of {[e for e in EFFORTS if e]}")
    out["max_runs"] = _int(n.get("max_runs", 3), 1, 10, 3)
    out["timeout"] = _int(n.get("timeout", 1800), 60, 7200, 1800)
    cm = n.get("can_message", "*")
    if cm != "*":
        if not isinstance(cm, list) or not all(isinstance(x, str) for x in cm):
            raise TeamGraphError(f"agent {out['id']}: can_message must be '*' or a list of agent ids")
        cm = [x.strip() for x in cm if x.strip()][:MAX_NODES]
    out["can_message"] = cm
    out["blackboard"] = bool(n.get("blackboard", False))
    mods = n.get("modalities", list(MODALITIES))
    if not isinstance(mods, list) or any(m not in MODALITIES for m in mods):
        raise TeamGraphError(f"agent {out['id']}: modalities must be a subset of {list(MODALITIES)}")
    out["modalities"] = [m for m in MODALITIES if m in mods]
    key = _s(n.get("output_key"), 40)
    if key and not _KEY_RX.fullmatch(key):
        raise TeamGraphError(f"agent {out['id']}: output_key must be letters/digits/_ (start with a letter)")
    out["output_key"] = key
    out["guardrail"] = bool(n.get("guardrail", False))


def _norm_attachments(raw, where: str) -> list[str]:
    ids = raw if isinstance(raw, list) else []
    if len(ids) > MAX_ATTACHMENTS:
        raise TeamGraphError(f"{where}: at most {MAX_ATTACHMENTS} attachments")
    clean = []
    for a in ids:
        a = str(a.get("id") if isinstance(a, dict) else a).strip().lower()
        if not _UUID_RX.fullmatch(a):
            raise TeamGraphError(f"{where}: invalid attachment id {a!r}")
        if a not in clean:
            clean.append(a)
    return clean


def _norm_data(n: dict, out: dict) -> None:
    src = n.get("source", "text")
    if src not in DATA_SOURCES:
        raise TeamGraphError(f"data {out['id']}: source must be one of {list(DATA_SOURCES)}")
    raw = n.get("params") if isinstance(n.get("params"), dict) else {}
    p: dict = {}
    if src == "spec_scope":
        p["domain"] = _s(raw.get("domain"), 64)
    elif src == "ux_layout":
        view = _s(raw.get("view_key"), 64, "home") or "home"
        if not _VIEW_RX.fullmatch(view):
            raise TeamGraphError(f"data {out['id']}: invalid view_key")
        p["view_key"] = view
    elif src == "repo_files":
        paths = raw.get("paths") or []
        if isinstance(paths, str):
            paths = [x for x in re.split(r"[\s,]+", paths) if x]
        if not isinstance(paths, list):
            raise TeamGraphError(f"data {out['id']}: paths must be a list")
        clean = []
        for path in paths[:8]:
            path = str(path).strip().lstrip("/")
            if not _PATH_RX.fullmatch(path) or ".." in path.split("/"):
                raise TeamGraphError(f"data {out['id']}: invalid path {path!r}")
            clean.append(path)
        p["paths"] = clean
        repo = _s(raw.get("repo_key"), 64)
        if repo and (not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", repo) or repo.startswith(".")):
            raise TeamGraphError(f"data {out['id']}: invalid repo_key")
        p["repo_key"] = repo
    elif src == "collab_history":
        p["limit"] = _int(raw.get("limit", 20), 1, 100, 20)
    elif src == "text":
        p["text"] = _s(raw.get("text"), 12000)
    elif src == "files":
        p["attachments"] = _norm_attachments(raw.get("attachments"), f"data {out['id']}")
        p["note"] = _s(raw.get("note"), 2000)
    out["source"], out["params"] = src, p


def validate_team_graph(graph: dict) -> dict:
    """Validate and normalize a canvas team graph; returns a clean copy (only valid graphs are saved/run)."""
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise TeamGraphError("team graph must have nodes[] and edges[]")
    if len(graph["nodes"]) > MAX_NODES:
        raise TeamGraphError(f"at most {MAX_NODES} nodes")
    if len(graph["edges"]) > MAX_EDGES:
        raise TeamGraphError(f"at most {MAX_EDGES} edges")
    nodes, ids = [], set()
    for n in graph["nodes"]:
        if not isinstance(n, dict):
            raise TeamGraphError("node must be an object")
        nid = str(n.get("id") or "")
        if not _ID_RX.fullmatch(nid):
            raise TeamGraphError(f"node id {nid!r}: letters/digits/_/- (start with a letter, max 40)")
        if nid in ids:
            raise TeamGraphError(f"duplicate node id {nid}")
        kind = n.get("kind")
        if kind not in NODE_KINDS:
            raise TeamGraphError(f"node {nid}: kind must be one of {list(NODE_KINDS)}")
        ids.add(nid)
        out = {"id": nid, "kind": kind, "label": _s(n.get("label"), 80, nid) or nid,
               "x": _int(n.get("x", 40), -2000, 6000, 40), "y": _int(n.get("y", 40), -2000, 6000, 40)}
        if kind == "agent":
            _norm_agent(n, out)
        elif kind == "data":
            _norm_data(n, out)
        elif kind == "input":
            out["default_request"] = _s(n.get("default_request"), 8000)
            out["attachments"] = _norm_attachments(n.get("attachments"), f"input {nid}")
        nodes.append(out)
    if not any(n["kind"] == "agent" for n in nodes):
        raise TeamGraphError("a team needs at least one agent node")
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        if n["kind"] == "agent" and n["can_message"] != "*":
            bad = [x for x in n["can_message"] if by_id.get(x, {}).get("kind") != "agent" or x == n["id"]]
            if bad:
                raise TeamGraphError(f"agent {n['id']}: can_message references non-agents {bad}")
    edges, seen = [], set()
    for i, e in enumerate(graph["edges"]):
        if not isinstance(e, dict):
            raise TeamGraphError("edge must be an object")
        a, b = e.get("from"), e.get("to")
        if a not in by_id or b not in by_id:
            raise TeamGraphError(f"edge {a}->{b} references an unknown node")
        if a == b:
            raise TeamGraphError(f"edge {a}->{b}: self loops are not allowed (use max_runs + revise edges)")
        if by_id[b]["kind"] in ("input", "data"):
            raise TeamGraphError(f"edge {a}->{b}: {by_id[b]['kind']} nodes cannot receive edges")
        if by_id[a]["kind"] == "output":
            raise TeamGraphError(f"edge {a}->{b}: output nodes cannot send edges")
        when = e.get("when", "always")
        if when not in EDGE_WHEN:
            raise TeamGraphError(f"edge {a}->{b}: when must be one of {list(EDGE_WHEN)}")
        if when != "always" and by_id[a]["kind"] != "agent":
            raise TeamGraphError(f"edge {a}->{b}: only agent edges can be conditional")
        if when in ("handoff", "spawn") and by_id[b]["kind"] != "agent":
            raise TeamGraphError(f"edge {a}->{b}: {when} edges must target an agent")
        key = (a, b, when)
        if key in seen:
            raise TeamGraphError(f"duplicate edge {a}->{b} ({when})")
        seen.add(key)
        eid = str(e.get("id") or f"e{i + 1}")
        edges.append({"id": eid[:40], "from": a, "to": b, "when": when, "label": _s(e.get("label"), 80)})
    for n in nodes:
        if n["kind"] == "agent" and not any(e["to"] == n["id"] for e in edges):
            raise TeamGraphError(f"agent {n['id']} has no incoming edge (connect an input, data or agent node)")
    workers = {e["to"] for e in edges if e["when"] == "spawn"}
    for w in workers:
        if any(e["from"] == w for e in edges):
            raise TeamGraphError(f"agent {w}: a spawn worker returns its result to the orchestrator automatically, "
                                 "so it cannot have outgoing edges")
        bad = [e["from"] for e in edges if e["to"] == w and e["when"] != "spawn" and by_id[e["from"]]["kind"] == "agent"]
        if bad:
            raise TeamGraphError(f"agent {w}: a spawn worker only receives spawn, input and data edges (got {bad})")
    st = graph.get("settings") if isinstance(graph.get("settings"), dict) else {}
    settings = {"max_steps": _int(st.get("max_steps", 24), 1, 100, 24),
                "parallelism": _int(st.get("parallelism", 3), 1, 6, 3),
                "repo_key": _s(st.get("repo_key"), 64)}
    if settings["repo_key"] and (not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", settings["repo_key"])
                                 or settings["repo_key"].startswith(".")):
        raise TeamGraphError("settings.repo_key is invalid")
    return {"nodes": nodes, "edges": edges, "settings": settings}


# ---------------------------------------------------------------- agent I/O
@dataclass
class AgentOutput:
    content: str
    verdict: str = "none"
    messages: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    handoff: str = ""                                   # OpenAI-style handoff / ADK transfer target
    tasks: list[dict] = field(default_factory=list)     # orchestrator -> worker sub-tasks [{agent, task}]
    tripwire: bool = False                              # guardrail tripped: halt the team run
    tripwire_reason: str = ""


THINK_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "your full answer / work product for the team"},
        "verdict": {"type": "string", "enum": list(VERDICTS),
                    "description": "approve or revise when you review a teammate's work, otherwise none"},
        "messages": {"type": "array", "maxItems": MAX_MESSAGES_PER_TURN,
                     "items": {"type": "object",
                               "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
                               "required": ["to", "content"], "additionalProperties": False}},
    },
    "required": ["content", "verdict", "messages"],
    "additionalProperties": False,
}


def handoff_targets(graph: dict, node: dict) -> list[str]:
    return [e["to"] for e in graph["edges"] if e["from"] == node["id"] and e["when"] == "handoff"]


def spawn_targets(graph: dict, node: dict) -> list[str]:
    return [e["to"] for e in graph["edges"] if e["from"] == node["id"] and e["when"] == "spawn"]


def worker_ids(graph: dict) -> set[str]:
    return {e["to"] for e in graph["edges"] if e["when"] == "spawn"}


def node_schema(graph: dict, node: dict, worker: bool = False) -> dict:
    """Structured-output schema for one think-mode turn: only the fields this node can actually use, all required
    (strict structured-output providers reject optional properties)."""
    hand, workers = ([], []) if worker else (handoff_targets(graph, node), spawn_targets(graph, node))
    if not hand and not workers and not node.get("guardrail"):
        return THINK_SCHEMA
    props, required = dict(THINK_SCHEMA["properties"]), list(THINK_SCHEMA["required"])
    if hand:
        props["handoff"] = {"type": "string", "enum": ["", *hand],
                            "description": "agent id to hand the conversation to, or empty to finish yourself"}
        required.append("handoff")
    if workers:
        props["tasks"] = {"type": "array", "maxItems": MAX_TASKS_PER_TURN,
                          "description": "sub-tasks for workers (run in parallel); empty when giving the final answer",
                          "items": {"type": "object",
                                    "properties": {"agent": {"type": "string", "enum": workers},
                                                   "task": {"type": "string"}},
                                    "required": ["agent", "task"], "additionalProperties": False}}
        required.append("tasks")
    if node.get("guardrail"):
        props["tripwire"] = {"type": "boolean", "description": "true halts the whole team run (policy violation)"}
        props["tripwire_reason"] = {"type": "string"}
        required += ["tripwire", "tripwire_reason"]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_VERDICT_RX = re.compile(r"<<VERDICT:(APPROVE|REVISE)>>", re.I)
_TO_RX = re.compile(r"<<TO:([A-Za-z][A-Za-z0-9_-]{0,39})>>(.*?)<</TO>>", re.S)
_HANDOFF_RX = re.compile(r"<<HANDOFF:([A-Za-z][A-Za-z0-9_-]{0,39})>>")
_TASK_RX = re.compile(r"<<TASK:([A-Za-z][A-Za-z0-9_-]{0,39})>>(.*?)<</TASK>>", re.S)
_TRIP_RX = re.compile(r"<<TRIPWIRE(?::([^>]*))?>>")


def _tasks_of(raw) -> list[dict]:
    out = []
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, dict) and str(t.get("task") or "").strip():
            out.append({"agent": str(t.get("agent") or "").strip(), "task": str(t["task"]).strip()})
    return out


def coerce_output(raw) -> AgentOutput:
    """Accept a structured dict (think mode), marker text (code mode) or an AgentOutput."""
    if isinstance(raw, AgentOutput):
        return raw
    if isinstance(raw, dict):
        verdict = str(raw.get("verdict") or "none").lower()
        msgs = raw.get("messages") if isinstance(raw.get("messages"), list) else []
        content = raw.get("content")
        if not isinstance(content, str):
            content = json.dumps(content if content is not None else raw, ensure_ascii=False)
        return AgentOutput(content, verdict if verdict in VERDICTS else "none",
                           [m for m in msgs if isinstance(m, dict)],
                           handoff=str(raw.get("handoff") or "").strip(), tasks=_tasks_of(raw.get("tasks")),
                           tripwire=raw.get("tripwire") is True,
                           tripwire_reason=str(raw.get("tripwire_reason") or "").strip()[:2000])
    text = str(raw or "")
    verdict = "none"
    for m in _VERDICT_RX.finditer(text):
        verdict = m.group(1).lower()
    handoff = ""
    for m in _HANDOFF_RX.finditer(text):
        handoff = m.group(1)
    trip = _TRIP_RX.search(text)
    messages = [{"to": m.group(1), "content": m.group(2).strip()} for m in _TO_RX.finditer(text)]
    tasks = [{"agent": m.group(1), "task": m.group(2).strip()} for m in _TASK_RX.finditer(text) if m.group(2).strip()]
    content = text
    for rx in (_TO_RX, _TASK_RX, _VERDICT_RX, _HANDOFF_RX, _TRIP_RX):
        content = rx.sub("", content)
    return AgentOutput(content.strip(), verdict, messages, handoff=handoff, tasks=tasks, tripwire=bool(trip),
                       tripwire_reason=((trip.group(1) or "").strip() if trip else ""))


def _trim(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 40] + f"\n…(잘림, 총 {len(text)}자)"


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


DEFAULT_TEMPLATE = "{{auto}}"
_PLACEHOLDER_RX = re.compile(r"\{\{\s*(request|data|agents|feedback|messages|blackboard|inputs|auto|subtasks|attachments"
                             r"|state|input:[A-Za-z0-9_-]+|state:[A-Za-z0-9_]+)\s*\}\}")
_STATE_RX = re.compile(r"\{\{\s*state(?::([A-Za-z0-9_]+))?\s*\}\}")


def _state_text(state: dict, key: str | None = None) -> str:
    if key is not None:
        return _trim(state[key], MAX_INPUT_CHARS) if key in state else "(없음)"
    return "\n".join(f"- `{k}`: {_trim(v, 1500)}" for k, v in state.items())


def _team_roster(graph: dict, me: dict) -> str:
    workers = worker_ids(graph)
    rows = []
    for n in graph["nodes"]:
        if n["kind"] != "agent":
            continue
        tag = (" (나)" if n["id"] == me["id"] else "") + (" (워커)" if n["id"] in workers else "")
        rows.append(f"- `{n['id']}` {n['label']}{tag}: {n.get('role') or '역할 미지정'}")
    return "\n".join(rows)


def _allowed_targets(graph: dict, me: dict) -> list[str]:
    workers = worker_ids(graph)  # workers only run on assigned tasks, so a message would never be read
    agents = [n["id"] for n in graph["nodes"] if n["kind"] == "agent" and n["id"] != me["id"] and n["id"] not in workers]
    return agents if me.get("can_message", "*") == "*" else [a for a in agents if a in me["can_message"]]


def build_prompt(graph: dict, node: dict, inbox: dict, request: str, blackboard: list[dict], run_no: int,
                 state: dict | None = None, attachments_note: str = "", subtask: dict | None = None) -> str:
    """Compose: role + default system prompt + team roster + collaboration protocol + rendered template."""
    state = state or {}
    labels = {n["id"]: n["label"] for n in graph["nodes"]}
    groups: dict[str, list[str]] = {k: [] for k in (*INPUT_KINDS, "subtask")}
    per_source: dict[str, str] = {}
    for src, item in inbox.items():
        body = _trim(item["content"], MAX_INPUT_CHARS)
        per_source[item.get("node", src)] = body
        if item["kind"] == "request":
            continue
        if item["kind"] == "subtask":
            groups["subtask"].append(body)
            continue
        head = f"### {labels.get(item.get('node'), item.get('node'))} (`{item.get('node')}`)"
        groups[item["kind"]].append(f"{head}\n{body}")
    sections = {
        "request": request or "(요청 없음)",
        "team_request": (subtask or {}).get("request") or "",
        "attachments": attachments_note,
        "data": "\n\n".join(groups["data"]),
        "agents": "\n\n".join(groups["agent"]),
        "subtasks": "\n\n".join(groups["subtask"]),
        "feedback": "\n\n".join(groups["feedback"]),
        "messages": "\n\n".join(groups["message"]),
        "state": _state_text(state),
        "blackboard": "\n".join(f"- [{labels.get(b['node'], b['node'])}] {_trim(b['content'], 600)}"
                                for b in blackboard[-12:]),
    }
    titles = [("request", "배정된 하위 작업" if subtask else "사용자 요청")]
    if subtask:
        titles.append(("team_request", "팀 전체 요청 (참고)"))
    titles += [("attachments", "첨부 파일 (멀티모달 입력)"), ("data", "입력 데이터"), ("agents", "동료 에이전트 결과"),
               ("subtasks", "하위 작업 결과 (워커들이 돌려준 결과 — 종합하거나 필요하면 추가 작업을 배정하세요)"),
               ("feedback", "리뷰 피드백 (반영해서 다시 작업하세요)"), ("messages", "동료에게 받은 메시지")]
    if node.get("blackboard"):
        titles += [("state", "팀 공유 상태"), ("blackboard", "팀 공유 보드 (최근 활동)")]
    auto = "\n\n".join(f"## {t}\n{sections[k]}" for k, t in titles if sections[k])
    sections["inputs"] = auto

    def sub(m):
        key = m.group(1)
        if key == "auto":
            return auto
        if key.startswith("input:"):
            return per_source.get(key[6:], "(없음)")
        if key.startswith("state:"):
            return _state_text(state, key[6:])
        return sections.get(key) or "(없음)"

    body = _PLACEHOLDER_RX.sub(sub, node.get("prompt_template") or DEFAULT_TEMPLATE)
    targets = [] if subtask else _allowed_targets(graph, node)
    hand = [] if subtask else handoff_targets(graph, node)
    workers = [] if subtask else spawn_targets(graph, node)
    reviewer = not subtask and any(e["from"] == node["id"] and e["when"] in ("approve", "revise") for e in graph["edges"])
    if node.get("mode") == "code":
        protocol = ["- 저장소에서 직접 작업한 뒤, 마지막 응답에 팀에 전달할 결과 요약을 쓰세요."]
        if targets:
            protocol.append(f"- 동료에게 질문/요청: `<<TO:에이전트id>>내용<</TO>>` (가능한 대상: {', '.join(targets)})")
        if reviewer:
            protocol.append("- 검토 역할이면 `<<VERDICT:APPROVE>>` 또는 `<<VERDICT:REVISE>>`를 반드시 포함하세요.")
        if hand:
            protocol.append(f"- 다른 에이전트에게 넘기려면(handoff) `<<HANDOFF:대상id>>` (가능: {', '.join(hand)}). "
                            "넘기면 그 대상만 이어서 작업합니다.")
        if workers:
            protocol.append(f"- 하위 작업 배정: `<<TASK:워커id>>작업 설명<</TASK>>` (최대 {MAX_TASKS_PER_TURN}개, 병렬 실행, "
                            f"가능: {', '.join(workers)}). 배정하면 이번 응답은 중간 단계이고 결과는 다음 활성화 때 돌아옵니다. "
                            "최종 답에는 TASK를 넣지 마세요.")
        if node.get("guardrail"):
            protocol.append("- 당신은 가드레일입니다. 입력/결과가 정책·안전 기준을 위반하면 `<<TRIPWIRE:이유>>`를 쓰세요. "
                            "팀 실행이 즉시 중단됩니다.")
    else:
        fields = ["content(결과물 전체)", "verdict", "messages[{to, content}]"]
        fields += (["handoff"] if hand else []) + (["tasks[{agent, task}]"] if workers else []) + \
                  (["tripwire", "tripwire_reason"] if node.get("guardrail") else [])
        protocol = [f"- JSON 하나로 답하세요: {', '.join(fields)}."]
        protocol.append(f"- messages로 동료에게 질문·요청·답변을 보낼 수 있습니다 (가능한 대상: {', '.join(targets)})."
                        if targets else "- messages는 빈 배열로 두세요.")
        protocol.append("- verdict: 동료의 작업을 검토했다면 approve 또는 revise, 아니면 none." if reviewer
                        else "- 검토 역할이 아니면 verdict는 none.")
        if hand:
            protocol.append(f"- handoff: 다른 에이전트에게 대화를 넘기려면 대상 id ({', '.join(hand)}), 직접 끝내려면 빈 문자열. "
                            "넘기면 그 대상만 이어서 작업합니다.")
        if workers:
            protocol.append(f"- tasks: 하위 작업을 워커에게 배정 (최대 {MAX_TASKS_PER_TURN}개, 병렬 실행, 가능: {', '.join(workers)}). "
                            "tasks가 있으면 이번 답은 중간 단계로 다음 노드에 전달되지 않고, 결과는 다음 활성화 때 "
                            "'하위 작업 결과'로 돌아옵니다. 최종 답을 낼 때는 tasks를 빈 배열로 두세요.")
        if node.get("guardrail"):
            protocol.append("- tripwire: 당신은 가드레일입니다. 요청이나 결과가 정책·안전 기준을 위반하면 true와 "
                            "tripwire_reason을 쓰세요. 팀 실행이 즉시 중단(BLOCKED)됩니다. 문제가 없으면 false.")
    if attachments_note:
        protocol.append("- 첨부 파일은 경로가 주어지면 직접 열어 확인하고, 내용에 근거해 답하세요.")
    if subtask:
        head = [f"# 당신은 멀티 에이전트 팀의 워커 `{node['id']}` ({node['label']}) 입니다. "
                f"오케스트레이터 `{subtask.get('parent')}`가 하위 작업을 배정했습니다. 이 작업에만 집중해 결과를 돌려주세요."]
    else:
        head = [f"# 당신은 멀티 에이전트 팀의 `{node['id']}` ({node['label']}) 입니다."]
    if node.get("role"):
        head.append(f"역할: {node['role']}")
    if node.get("system_prompt"):
        sys_prompt = _STATE_RX.sub(lambda m: _state_text(state, m.group(1)) if m.group(1) else _state_text(state),
                                   node["system_prompt"])
        head.append(f"\n## 기본 지침\n{sys_prompt}")
    head.append(f"\n## 팀 구성\n{_team_roster(graph, node)}")
    head.append("\n## 협업 규칙\n" + "\n".join(protocol))
    if run_no > 1 and not subtask:
        head.append(f"\n(이번이 {run_no}번째 활성화입니다. 새로 받은 입력에 집중하세요.)")
    return "\n".join(head) + "\n\n---\n\n" + body


# ---------------------------------------------------------------- media
class MediaHooks:
    """Attachment handling injected into :class:`TeamRun`. The default only knows ids (no storage)."""

    def lookup(self, ids: list[str]) -> list[dict]:
        """Attachment ids -> metadata dicts ``{id, name, mime, modality, size, excerpt?}``."""
        return [{"id": i, "name": i, "mime": "", "modality": "file", "size": 0} for i in ids]

    def prepare(self, metas: list[dict]) -> None:
        """Called once per run with every attachment that reached an agent (e.g. copy into the VM, extract)."""
        return None

    def describe(self, metas: list[dict]) -> str:
        """Prompt section listing the attachments an agent receives."""
        rows = []
        for m in metas:
            rows.append(f"- `{m.get('name')}` ({m.get('modality')}, {m.get('mime') or '?'}, {_size(m.get('size'))})")
            if m.get("excerpt"):
                rows.append(f"  내용:\n```\n{_trim(m['excerpt'], 6000)}\n```")
        return "\n".join(rows)


def _dedupe_media(lists) -> list[dict]:
    seen, out = set(), []
    for metas in lists:
        for m in metas or ():
            if isinstance(m, dict) and m.get("id") and m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
    return out


# ---------------------------------------------------------------- scheduler
Invoke = Callable[[dict, str, dict], object]          # (node, prompt, ctx) -> AgentOutput | dict | str
ResolveData = Callable[[dict], object]                # (data node) -> text | {"text", "attachments"}
OnEvent = Callable[[dict], None]


def back_edges(graph: dict) -> set[tuple[str, str]]:
    """Edges that close a cycle, found by DFS from the input/data nodes.

    ``revise`` edges are feedback by definition and explored last, so in a plan -> code -> review -> (revise)
    -> code loop the forward edges stay "required" and only the feedback edge is a back edge.
    """
    order = {"always": 0, "approve": 1, "handoff": 1, "revise": 2, "spawn": 3}
    succ: dict[str, list[dict]] = {}
    for e in sorted(graph["edges"], key=lambda e: order[e["when"]]):
        succ.setdefault(e["from"], []).append(e)
    roots = [n["id"] for n in graph["nodes"] if n["kind"] in ("input", "data")]
    roots += [n["id"] for n in graph["nodes"] if n["id"] not in roots]
    state: dict[str, int] = {}          # 1 = on stack, 2 = finished
    back: set[tuple[str, str]] = set()
    for root in roots:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(succ.get(root, ())))]
        while stack:
            nid, it = stack[-1]
            e = next(it, None)
            if e is None:
                state[nid] = 2
                stack.pop()
                continue
            t = e["to"]
            if state.get(t) == 1:
                back.add((e["from"], t))
            elif t not in state:
                state[t] = 1
                stack.append((t, iter(succ.get(t, ()))))
    return back


class TeamRun:
    """Event-driven multi-agent scheduler.

    Every node keeps an inbox ``{source: item}`` of the latest input from each source plus the set of
    sources that delivered something *fresh* since its last run. An agent is ready when it has fresh input
    and (``input_mode=all``) every required upstream has delivered at least once. Required upstreams are the
    ``always`` edges that do not close a cycle, so review loops never deadlock. Ready agents run in parallel
    (``parallelism``); results fan out along matching edges, handoffs and direct messages. Orchestrators'
    ``tasks`` are queued as worker jobs sharing the same pool; when a batch completes its results return to the
    orchestrator as one ``subtask`` input. A guardrail agent's tripwire halts the run (``BLOCKED``).
    """

    def __init__(self, graph: dict, invoke: Invoke | None, resolve_data: ResolveData, request: str = "",
                 on_event: OnEvent | None = None, cancel: threading.Event | None = None,
                 attachments: list[dict] | None = None, media: MediaHooks | None = None):
        self.g = validate_team_graph(graph)
        self.nodes = {n["id"]: n for n in self.g["nodes"]}
        self.invoke, self.resolve_data = invoke, resolve_data
        self.on_event = on_event or (lambda ev: None)
        self.cancel = cancel or threading.Event()
        self.media = media or MediaHooks()
        self.run_attachments = list(attachments or [])
        inputs = [n for n in self.g["nodes"] if n["kind"] == "input"]
        self.request = (request or "").strip() or next((n["default_request"] for n in inputs if n["default_request"]), "")
        self.inbox: dict[str, dict[str, dict]] = {nid: {} for nid in self.nodes}
        self.fresh: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        self.runs: dict[str, int] = {nid: 0 for nid in self.nodes}
        self.status: dict[str, str] = {nid: "idle" for nid in self.nodes}
        self.last: dict[str, AgentOutput] = {}
        self.results: dict[str, list[dict]] = {n["id"]: [] for n in self.g["nodes"] if n["kind"] == "output"}
        self.blackboard: list[dict] = []
        self.state: dict[str, str] = {}
        self.seen_media: dict[str, list[dict]] = {}
        self.workers = worker_ids(self.g)
        self.tasks: deque = deque()
        self.batches: dict[int, dict] = {}
        self.batch_seq = 0
        self.jobs: dict[str, int] = {nid: 0 for nid in self.nodes}
        self.blocked: dict | None = None
        self.steps = 0
        self.seq = 0
        self._ev_lock = threading.Lock()
        back = back_edges(self.g)
        self.required: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        kind_of = {"input": "request", "data": "data", "agent": "agent"}
        for e in self.g["edges"]:
            tgt = self.nodes[e["to"]]
            if tgt["kind"] != "agent" or e["when"] != "always" or (e["from"], e["to"]) in back:
                continue
            if kind_of.get(self.nodes[e["from"]]["kind"]) in tgt["accepts"]:  # never wait for a filtered input
                self.required[e["to"]].add(e["from"])

    # -- events
    def emit(self, type_: str, node: str | None = None, content: str = "", target: str | None = None, **meta):
        with self._ev_lock:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), "type": type_, "node": node, "target": target,
                  "content": _trim(content, 20000), "meta": meta}
        try:
            self.on_event(ev)
        except Exception:
            pass
        return ev

    def snapshot(self) -> dict:
        return {nid: {"status": self.status[nid], "runs": self.runs[nid],
                      "verdict": self.last[nid].verdict if nid in self.last else None}
                for nid in self.nodes}

    # -- media
    def _media_for(self, nid: str, inbox: dict, extra: list[dict] | None = None) -> list[dict]:
        mods = self.nodes[nid].get("modalities")
        allowed = set(MODALITIES if mods is None else mods)  # [] means "no media", not "everything"
        metas = _dedupe_media([*(item.get("attachments") for item in inbox.values()), extra or []])
        return [m for m in metas if (m.get("modality") or "file") in allowed]

    def _describe(self, metas: list[dict]) -> str:
        if not metas:
            return ""
        try:
            return self.media.describe(metas)
        except Exception:
            return MediaHooks.describe(self.media, metas)

    def _lookup(self, ids: list[str], where: str) -> list[dict]:
        if not ids:
            return []
        try:
            return self.media.lookup(ids)
        except Exception as exc:
            self.emit("media_failed", where, f"첨부 파일 조회 실패: {exc}")
            return []

    # -- delivery
    def _deliver(self, target: str, source_key: str, item: dict) -> None:
        node = self.nodes[target]
        if node["kind"] == "output":
            self.results[target].append(item)
            self.status[target] = "done"
            self.emit("output", item.get("node"), item["content"], target=target)
            return
        if node["kind"] != "agent":
            return
        if item["kind"] not in node["accepts"] and item["kind"] != "subtask":  # results an orchestrator asked for
            self.emit("dropped", item.get("node"), f"{target}는 '{item['kind']}' 입력을 받지 않도록 설정됨", target=target)
            return
        self.inbox[target][source_key] = item
        self.fresh[target].add(source_key)
        self.emit("delivery", item.get("node"), "", target=target, kind=item["kind"],
                  attachments=len(item.get("attachments") or []))

    def _fan_out(self, src: str, out: AgentOutput, edges: bool = True) -> None:
        verdict = out.verdict
        carried = self.seen_media.get(src, [])  # media travels with the conversation (handoff keeps the images)
        hand = out.handoff if out.handoff in handoff_targets(self.g, self.nodes[src]) else ""
        if out.handoff and not hand:
            self.emit("handoff_rejected", src, out.content[:400], target=out.handoff, reason="no handoff edge to target")
        for e in self.g["edges"] if edges else ():
            if e["from"] != src or e["when"] == "spawn":
                continue
            if hand:  # a handoff transfers control: only the chosen handoff edge fires
                fire = e["when"] == "handoff" and e["to"] == hand
            else:
                fire = (e["when"] == "always" or (e["when"] == "approve" and verdict in ("approve", "none"))
                        or (e["when"] == "revise" and verdict == "revise"))
            if not fire:
                continue
            if e["when"] == "handoff":
                self.emit("handoff", src, out.content, target=e["to"])
            kind = "feedback" if e["when"] == "revise" else "agent"
            self._deliver(e["to"], src, {"node": src, "kind": kind, "content": out.content, "verdict": verdict,
                                         "attachments": carried})
        allowed = set(_allowed_targets(self.g, self.nodes[src]))
        for m in out.messages[:MAX_MESSAGES_PER_TURN]:
            to, text = str(m.get("to") or "").strip(), _trim(str(m.get("content") or ""), MAX_MESSAGE_CHARS)
            if not text:
                continue
            if to not in allowed:
                self.emit("message_rejected", src, text, target=to or None, reason="unknown or not allowed recipient")
                continue
            self.emit("message", src, text, target=to)
            self._deliver(to, f"msg:{src}", {"node": src, "kind": "message", "content": text})

    def _dispatch_tasks(self, src: str, out: AgentOutput) -> bool:
        allowed = spawn_targets(self.g, self.nodes[src])
        valid = []
        for i, t in enumerate(out.tasks):
            if i >= MAX_TASKS_PER_TURN:
                self.emit("task_rejected", src, t["task"], target=t["agent"] or None,
                          reason=f"at most {MAX_TASKS_PER_TURN} tasks per turn")
            elif t["agent"] not in allowed:
                self.emit("task_rejected", src, t["task"], target=t["agent"] or None, reason="no spawn edge to target")
            else:
                valid.append(t)
        if not valid:
            return False
        self.batch_seq += 1
        bid = self.batch_seq
        self.batches[bid] = {"parent": src, "tasks": valid, "items": [None] * len(valid), "pending": len(valid)}
        for i, t in enumerate(valid):
            self.tasks.append((bid, i))
            self.emit("task_assigned", src, t["task"], target=t["agent"], batch=bid, index=i)
        self.status[src] = "waiting"
        return True

    def _task_done(self, bid: int, idx: int, text: str, deliver: bool) -> None:
        b = self.batches[bid]
        b["items"][idx] = text
        b["pending"] -= 1
        if b["pending"] or not deliver:
            return
        parent = b["parent"]
        body = "\n\n".join(f"### 하위 작업 {i + 1} → `{t['agent']}`\n**작업:** {t['task']}\n\n{b['items'][i]}"
                           for i, t in enumerate(b["tasks"]))
        self.status[parent] = "done"
        self.emit("subtasks_returned", parent, "", batch=bid, count=len(b["tasks"]))
        self._deliver(parent, "subtasks", {"node": b["tasks"][0]["agent"], "kind": "subtask", "content": body})

    def _ready(self, nid: str) -> bool:
        n = self.nodes[nid]
        if n["kind"] != "agent" or nid in self.workers or self.status[nid] in ("running", "waiting") \
                or not self.fresh[nid]:
            return False
        if n["input_mode"] == "all":
            have = {k for k in self.inbox[nid] if not k.startswith("msg:")}
            if not self.required[nid] <= have:
                return False
        return True

    # -- main loop
    def _start_inputs(self) -> None:
        for n in self.g["nodes"]:
            if n["kind"] == "input":
                self.status[n["id"]] = "done"
                atts = _dedupe_media([self.run_attachments, self._lookup(n.get("attachments") or [], n["id"])])
                for e in self.g["edges"]:
                    if e["from"] == n["id"]:
                        self._deliver(e["to"], n["id"], {"node": n["id"], "kind": "request", "content": self.request,
                                                         "attachments": atts})
            elif n["kind"] == "data":
                self.status[n["id"]] = "running"
                atts: list[dict] = []
                try:
                    if n["source"] == "files":
                        atts = self._lookup(n["params"].get("attachments") or [], n["id"])
                        note = n["params"].get("note") or ""
                        res = (note + "\n\n" if note else "") + f"첨부 파일 {len(atts)}개 (아래 '첨부 파일' 섹션 참고)"
                    else:
                        res = self.resolve_data(n)
                    if isinstance(res, dict):
                        atts = _dedupe_media([atts, res.get("attachments") or []])
                        res = res.get("text") or ""
                    text = _trim(res, MAX_INPUT_CHARS)
                    self.status[n["id"]] = "done"
                    self.emit("data_resolved", n["id"], text, source=n["source"], attachments=len(atts))
                except Exception as exc:  # a missing source must not silently look like empty data
                    text = f"(데이터 소스 {n['source']} 조회 실패: {exc})"
                    self.status[n["id"]] = "failed"
                    self.emit("data_failed", n["id"], str(exc), source=n["source"])
                for e in self.g["edges"]:
                    if e["from"] == n["id"]:
                        self._deliver(e["to"], n["id"], {"node": n["id"], "kind": "data", "content": text,
                                                         "attachments": atts})
        media = _dedupe_media(item.get("attachments") for box in self.inbox.values() for item in box.values())
        if media:
            mods = sorted({m.get("modality") or "file" for m in media})
            self.emit("media_preparing", None, "", count=len(media), modalities=mods)
            try:
                self.media.prepare(media)
                self.emit("media_ready", None, self._describe(media), count=len(media), modalities=mods)
            except Exception as exc:  # agents still get names/excerpts; the failure is visible in the timeline
                self.emit("media_failed", None, str(exc)[:4000], count=len(media))

    def _invoke_node(self, node: dict, prompt: str, ctx: dict):
        if node["mode"] == "mock":
            responses = node["mock_responses"]
            return responses[min(ctx["run_no"], len(responses)) - 1]
        if self.invoke is None:
            raise RuntimeError("live agent invoker is unavailable")
        return self.invoke(node, prompt, ctx)

    def _submit_node(self, pool, running: dict, nid: str) -> None:
        node = self.nodes[nid]
        self.steps += 1
        self.runs[nid] += 1
        inbox = dict(self.inbox[nid])
        fresh = sorted(self.fresh[nid])
        self.fresh[nid].clear()
        for k in [k for k in self.inbox[nid] if k.startswith("msg:")]:
            del self.inbox[nid][k]     # a message is consumed once; data/agent inputs persist
        for k, item in list(self.inbox[nid].items()):
            if item["kind"] in ("feedback", "subtask"):
                del self.inbox[nid][k]  # feedback / sub-task results apply to the run they triggered
        metas = self._media_for(nid, inbox)
        self.seen_media[nid] = metas
        prompt = build_prompt(self.g, node, inbox, self.request, self.blackboard, self.runs[nid], self.state,
                              self._describe(metas))
        self.status[nid] = "running"
        self.emit("node_started", nid, "", run_no=self.runs[nid], fresh=fresh, mode=node["mode"],
                  attachments=len(metas))
        ctx = {"run_no": self.runs[nid], "request": self.request, "settings": self.g["settings"],
               "schema": node_schema(self.g, node), "attachments": metas, "subtask": None}
        running[pool.submit(self._invoke_node, node, prompt, ctx)] = ("node", nid, time.time(), None, None)

    def _submit_task(self, pool, running: dict, bid: int, idx: int) -> None:
        b = self.batches[bid]
        wid, task, parent = b["tasks"][idx]["agent"], b["tasks"][idx]["task"], b["parent"]
        worker = self.nodes[wid]
        self.steps += 1
        self.runs[wid] += 1
        self.jobs[wid] += 1
        inbox = dict(self.inbox[wid])  # input/data context wired to the worker
        metas = self._media_for(wid, inbox, self.seen_media.get(parent))
        sub = {"parent": parent, "request": self.request, "batch": bid, "index": idx}
        prompt = build_prompt(self.g, worker, inbox, task, self.blackboard, self.runs[wid], self.state,
                              self._describe(metas), subtask=sub)
        self.status[wid] = "running"
        self.emit("node_started", wid, task, run_no=self.runs[wid], mode=worker["mode"], subtask=True,
                  parent=parent, batch=bid, index=idx, attachments=len(metas))
        ctx = {"run_no": self.runs[wid], "request": self.request, "settings": self.g["settings"],
               "schema": node_schema(self.g, worker, worker=True), "attachments": metas, "subtask": sub}
        running[pool.submit(self._invoke_node, worker, prompt, ctx)] = ("task", wid, time.time(), bid, idx)

    def run(self) -> dict:
        st = self.g["settings"]
        self.emit("run_started", None, self.request, agents=[n["id"] for n in self.g["nodes"] if n["kind"] == "agent"],
                  attachments=len(self.run_attachments))
        self._start_inputs()
        failures: list[str] = []
        running: dict = {}
        outcome = None
        halted = ("CANCELLED", "BLOCKED")
        with ThreadPoolExecutor(max_workers=st["parallelism"], thread_name_prefix="agdd-team") as pool:
            while True:
                if self.cancel.is_set() and outcome != "BLOCKED":
                    outcome = "CANCELLED"
                if outcome in halted:
                    self.tasks.clear()
                for nid in [k for k in self.nodes if self._ready(k)] if outcome is None else []:
                    if len(running) >= st["parallelism"]:
                        break
                    node = self.nodes[nid]
                    if self.runs[nid] >= node["max_runs"]:
                        self.fresh[nid].clear()
                        self.status[nid] = "done" if nid in self.last else "skipped"
                        self.emit("node_skipped", nid, f"max_runs({node['max_runs']}) 도달 — 추가 입력은 무시됩니다")
                        continue
                    if self.steps >= st["max_steps"]:
                        outcome = "STEP_LIMIT"
                        break
                    self._submit_node(pool, running, nid)
                while outcome is None and self.tasks and len(running) < st["parallelism"]:
                    if self.steps >= st["max_steps"]:
                        outcome = "STEP_LIMIT"
                        break
                    self._submit_task(pool, running, *self.tasks.popleft())
                if not running:
                    break
                done, _ = wait(list(running), timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    kind, nid, t0, bid, idx = running.pop(fut)
                    ms = int((time.time() - t0) * 1000)
                    node = self.nodes[nid]
                    if kind == "task":
                        self.jobs[nid] -= 1
                        try:
                            out = coerce_output(fut.result())
                            text = out.content
                            self.blackboard.append({"node": nid, "content": out.content})
                            self.emit("node_finished", nid, out.content, verdict=out.verdict, ms=ms, subtask=True,
                                      batch=bid, index=idx, **{k: v for k, v in out.meta.items() if k != "content"})
                            failed = False
                        except Exception as exc:
                            failures.append(nid)
                            text = f"(하위 작업 실패: {str(exc)[:1500]})"
                            self.emit("node_failed", nid, str(exc)[:4000], ms=ms, subtask=True, batch=bid, index=idx)
                            failed = True
                        if not self.jobs[nid]:
                            self.status[nid] = "failed" if failed else "done"
                        self._task_done(bid, idx, text, outcome not in halted)
                        continue
                    try:
                        out = coerce_output(fut.result())
                    except Exception as exc:
                        self.status[nid] = "failed"
                        failures.append(nid)
                        self.emit("node_failed", nid, str(exc)[:4000], ms=ms)
                        continue
                    self.last[nid] = out
                    self.status[nid] = "done"
                    self.blackboard.append({"node": nid, "content": out.content})
                    self.emit("node_finished", nid, out.content, verdict=out.verdict, ms=ms,
                              messages=len(out.messages), tasks=len(out.tasks), handoff=out.handoff or None,
                              **{k: v for k, v in out.meta.items() if k != "content"})
                    if node.get("guardrail") and out.tripwire:
                        self.blocked = {"node": nid, "reason": out.tripwire_reason or out.content[:2000]}
                        self.emit("guardrail_tripped", nid, self.blocked["reason"])
                        outcome = "BLOCKED"
                        continue
                    if outcome in halted:
                        continue
                    spawned = bool(out.tasks) and outcome is None and self._dispatch_tasks(nid, out)
                    if node.get("output_key") and not spawned:
                        self.state[node["output_key"]] = out.content
                        self.emit("state_set", nid, out.content, key=node["output_key"])
                    # still deliver (e.g. to outputs) after the step limit; an orchestrator waiting on its
                    # workers only delivers messages — its edges fire with the final answer
                    self._fan_out(nid, out, edges=not spawned)
                if outcome in halted and not running:
                    break
        if outcome is None:
            if any(self.results.values()):
                outcome = "PARTIAL" if failures else "SUCCEEDED"
            elif self.results:  # output nodes exist but nothing reached them
                outcome = "FAILED"
            else:
                outcome = "FAILED" if failures and not self.last else ("PARTIAL" if failures else "SUCCEEDED")
        for nid, s in self.status.items():
            if s == "idle" and self.nodes[nid]["kind"] == "agent":
                self.status[nid] = "skipped"
            elif s == "waiting":
                self.status[nid] = "done"
        result = self.result_payload()
        self.emit("run_finished", None, self.blocked["reason"] if self.blocked else "", status=outcome,
                  steps=self.steps, failures=failures)
        return {"status": outcome, "steps": self.steps, "failures": failures, "result": result,
                "node_state": self.snapshot()}

    def result_payload(self) -> dict:
        outputs = {k: [i["content"] for i in v] for k, v in self.results.items()}
        if not self.results:  # no output node: the sink agents' last answers are the result
            sinks = [nid for nid, n in self.nodes.items() if n["kind"] == "agent" and nid not in self.workers
                     and not any(e["from"] == nid for e in self.g["edges"]) and nid in self.last]
            outputs = {nid: [self.last[nid].content] for nid in sinks}
        out = {"outputs": outputs, "final": {nid: o.content for nid, o in self.last.items()}, "state": dict(self.state)}
        if self.blocked:
            out["blocked"] = self.blocked
        return out


# ---------------------------------------------------------------- presets
def _review_loop() -> dict:
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "요청", "x": 40, "y": 160},
            {"id": "spec", "kind": "data", "label": "명세 범위", "source": "spec_scope", "x": 40, "y": 340},
            {"id": "planner", "kind": "agent", "label": "기획", "x": 320, "y": 200,
             "role": "요청과 명세를 구현 계획으로 분해", "accepts": ["request", "data", "message"],
             "system_prompt": "변경할 파일, 단계, 수용 기준을 번호 목록으로 작성하세요. 구현하지는 마세요."},
            {"id": "coder", "kind": "agent", "label": "구현", "x": 600, "y": 200, "mode": "code",
             "role": "계획대로 저장소에 코드를 작성", "max_runs": 3,
             "system_prompt": "계획을 따르고 테스트를 실행하세요. 계획이 모호하면 planner에게 질문하세요."},
            {"id": "reviewer", "kind": "agent", "label": "리뷰", "x": 880, "y": 200,
             "role": "구현을 계획·명세와 대조해 승인 또는 수정 요청", "accepts": ["agent", "data", "message"],
             "system_prompt": "버그·누락된 수용 기준을 찾으세요. 문제가 있으면 revise와 구체적 수정 사항을, 없으면 approve."},
            {"id": "result", "kind": "output", "label": "결과", "x": 1160, "y": 220},
        ],
        "edges": [
            {"from": "request", "to": "planner"}, {"from": "spec", "to": "planner"},
            {"from": "spec", "to": "reviewer"}, {"from": "planner", "to": "coder"},
            {"from": "coder", "to": "reviewer"},
            {"from": "reviewer", "to": "coder", "when": "revise", "label": "수정 요청"},
            {"from": "reviewer", "to": "result", "when": "approve", "label": "승인"},
        ],
        "settings": {"max_steps": 12, "parallelism": 2},
    }


def _parallel() -> dict:
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "요청", "x": 40, "y": 200},
            {"id": "spec", "kind": "data", "label": "명세 범위", "source": "spec_scope", "x": 40, "y": 380},
            {"id": "analyst_ux", "kind": "agent", "label": "UX 분석가", "x": 340, "y": 60,
             "role": "사용자 경험 관점 분석", "system_prompt": "사용자 흐름·접근성·화면 구성 관점에서만 분석하세요."},
            {"id": "analyst_arch", "kind": "agent", "label": "아키텍처 분석가", "x": 340, "y": 240,
             "role": "구조·성능 관점 분석", "system_prompt": "모듈 경계, 데이터 흐름, 성능 위험을 분석하세요."},
            {"id": "analyst_risk", "kind": "agent", "label": "리스크 분석가", "x": 340, "y": 420,
             "role": "보안·운영 리스크 분석", "system_prompt": "보안, 데이터 손실, 운영 리스크를 찾아내세요."},
            {"id": "synth", "kind": "agent", "label": "종합", "x": 680, "y": 240,
             "role": "분석 결과를 하나의 실행 계획으로 종합",
             "system_prompt": "세 분석가의 결과를 충돌 없이 합쳐 우선순위가 있는 계획을 만드세요. 모순이 있으면 해당 분석가에게 질문하세요."},
            {"id": "result", "kind": "output", "label": "결과", "x": 980, "y": 260},
        ],
        "edges": [
            {"from": "request", "to": "analyst_ux"}, {"from": "request", "to": "analyst_arch"},
            {"from": "request", "to": "analyst_risk"}, {"from": "spec", "to": "analyst_arch"},
            {"from": "spec", "to": "analyst_risk"}, {"from": "analyst_ux", "to": "synth"},
            {"from": "analyst_arch", "to": "synth"}, {"from": "analyst_risk", "to": "synth"},
            {"from": "synth", "to": "result"},
        ],
        "settings": {"max_steps": 16, "parallelism": 3},
    }


def _debate() -> dict:
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "논제", "x": 40, "y": 200},
            {"id": "pro", "kind": "agent", "label": "찬성", "x": 320, "y": 80, "input_mode": "any",
             "role": "제안을 옹호", "system_prompt": "제안의 장점을 근거와 함께 주장하고, 반대 측 메시지에 반박하세요."},
            {"id": "con", "kind": "agent", "label": "반대", "x": 320, "y": 320, "input_mode": "any",
             "role": "제안을 비판", "system_prompt": "제안의 약점과 대안을 제시하고, 찬성 측 메시지에 반박하세요."},
            {"id": "judge", "kind": "agent", "label": "심판", "x": 640, "y": 200,
             "role": "양측 주장을 평가해 결론", "system_prompt": "양측 논거를 비교해 결론과 근거를 쓰세요."},
            {"id": "result", "kind": "output", "label": "결론", "x": 920, "y": 220},
        ],
        "edges": [
            {"from": "request", "to": "pro"}, {"from": "request", "to": "con"},
            {"from": "pro", "to": "judge"}, {"from": "con", "to": "judge"}, {"from": "judge", "to": "result"},
        ],
        "settings": {"max_steps": 12, "parallelism": 2},
    }


def _orchestrator_workers() -> dict:
    # Anthropic multi-agent research system: a lead agent plans, spawns parallel sub-agents with focused tasks,
    # synthesizes their findings (iterating if needed), then a citation agent grounds the final report.
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "질문", "x": 40, "y": 220},
            {"id": "lead", "kind": "agent", "label": "리드 에이전트", "x": 330, "y": 200, "max_runs": 4,
             "role": "질문을 하위 작업으로 분해해 워커에게 배정하고 결과를 종합",
             "system_prompt": "먼저 접근 전략을 세우고 서로 겹치지 않는 하위 작업 2~5개를 tasks로 배정하세요. 각 task에는 목표, "
                              "범위, 결과 형식을 명확히 쓰세요. 하위 작업 결과가 돌아오면 빈틈을 평가해 필요하면 추가 배정하고, "
                              "충분하면 tasks를 비우고 최종 보고서를 작성하세요."},
            {"id": "worker", "kind": "agent", "label": "서브 에이전트", "x": 330, "y": 440,
             "role": "배정된 하위 작업 하나를 깊이 조사",
             "system_prompt": "배정된 작업만 조사하세요. 근거(파일·출처·데이터)를 함께 제시하고 확실하지 않은 부분은 표시하세요."},
            {"id": "citer", "kind": "agent", "label": "인용 검증", "x": 640, "y": 200,
             "role": "최종 보고서의 주장마다 근거를 연결하고 근거 없는 주장을 표시",
             "system_prompt": "보고서의 각 주장에 근거를 붙이고, 근거가 없는 주장은 [근거 필요]로 표시해 수정본을 내세요."},
            {"id": "result", "kind": "output", "label": "보고서", "x": 940, "y": 220},
        ],
        "edges": [
            {"from": "request", "to": "lead"},
            {"from": "lead", "to": "worker", "when": "spawn", "label": "하위 작업"},
            {"from": "lead", "to": "citer"}, {"from": "citer", "to": "result"},
        ],
        "settings": {"max_steps": 30, "parallelism": 4},
    }


def _handoff_router() -> dict:
    # OpenAI Agents SDK triage pattern (also Anthropic "routing", Google ADK LLM transfer).
    spec = [("tech", "기술 지원", "버그·오류·설정 문제를 재현 단계와 함께 해결"),
            ("billing", "결제·계정", "결제, 요금제, 계정 문제를 정책에 맞게 처리"),
            ("general", "일반 문의", "그 외 질문에 간결하게 답변")]
    nodes = [{"id": "request", "kind": "input", "label": "사용자 메시지", "x": 40, "y": 240},
             {"id": "triage", "kind": "agent", "label": "분류(트리아지)", "x": 320, "y": 220,
              "role": "요청을 이해하고 가장 알맞은 전문 에이전트에게 넘김",
              "system_prompt": "요청의 의도를 한 줄로 요약하고 handoff로 가장 알맞은 전문 에이전트를 고르세요. 간단한 인사 정도만 "
                               "직접 답하고, 넘길 때 content에는 전문 에이전트가 알아야 할 맥락을 쓰세요."},
             {"id": "result", "kind": "output", "label": "응답", "x": 940, "y": 240}]
    edges = [{"from": "request", "to": "triage"}, {"from": "triage", "to": "result", "label": "직접 답변"}]
    for i, (aid, label, role) in enumerate(spec):
        nodes.append({"id": aid, "kind": "agent", "label": label, "x": 640, "y": 60 + i * 180, "role": role,
                      "system_prompt": f"당신은 {label} 전문가입니다. {role}하세요."})
        # no request edge: a specialist only runs when triage hands off (the request text is in every prompt and
        # the user's attachments travel with the handoff)
        edges += [{"from": "triage", "to": aid, "when": "handoff", "label": "handoff"}, {"from": aid, "to": "result"}]
    return {"nodes": nodes, "edges": edges, "settings": {"max_steps": 8, "parallelism": 2}}


def _guardrails() -> dict:
    # OpenAI Agents SDK guardrails: an input guardrail runs before the agent and an output guardrail checks its
    # answer; either tripwire halts the run (BLOCKED).
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "요청", "x": 40, "y": 220},
            {"id": "input_guard", "kind": "agent", "label": "입력 가드레일", "x": 300, "y": 80, "guardrail": True,
             "role": "요청이 정책(개인정보·유해·범위 밖)을 위반하는지 판정",
             "system_prompt": "요청이 개인정보 유출, 유해 행위, 서비스 범위 밖에 해당하면 tripwire=true. 아니면 false와 한 줄 판단."},
            {"id": "assistant", "kind": "agent", "label": "어시스턴트", "x": 600, "y": 220,
             "role": "요청을 처리", "accepts": ["request", "agent", "data", "message"],
             "system_prompt": "요청을 정확하고 간결하게 처리하세요. 가드레일 판정은 참고만 하세요."},
            {"id": "output_guard", "kind": "agent", "label": "출력 가드레일", "x": 880, "y": 220, "guardrail": True,
             "role": "답변에 민감정보·사실 오류·정책 위반이 있는지 검사",
             "system_prompt": "답변에 민감정보 노출이나 정책 위반이 있으면 tripwire=true. 문제 없으면 false와 답변 원문을 content로."},
            {"id": "result", "kind": "output", "label": "응답", "x": 1160, "y": 240},
        ],
        "edges": [
            {"from": "request", "to": "input_guard"}, {"from": "request", "to": "assistant"},
            {"from": "input_guard", "to": "assistant", "label": "통과"},
            {"from": "assistant", "to": "output_guard"}, {"from": "output_guard", "to": "result"},
        ],
        "settings": {"max_steps": 6, "parallelism": 2},
    }


def _sequential_state() -> dict:
    # Google ADK SequentialAgent: each step writes session state via output_key; later steps read {{state:key}}.
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "요청", "x": 40, "y": 200},
            {"id": "writer", "kind": "agent", "label": "초안 작성", "x": 300, "y": 200, "output_key": "draft",
             "role": "요청에 대한 첫 초안 작성", "system_prompt": "요청을 충족하는 초안을 작성하세요."},
            {"id": "critic", "kind": "agent", "label": "비평", "x": 580, "y": 200, "output_key": "critique",
             "role": "초안의 문제점을 목록으로", "accepts": ["request", "agent"],
             "prompt_template": "## 요청\n{{request}}\n\n## 초안 (state:draft)\n{{state:draft}}",
             "system_prompt": "초안의 논리·정확성·누락을 구체적 항목으로 지적하세요."},
            {"id": "refiner", "kind": "agent", "label": "개선", "x": 860, "y": 200, "output_key": "final",
             "role": "비평을 반영한 최종본", "accepts": ["request", "agent"],
             "prompt_template": "## 요청\n{{request}}\n\n## 초안\n{{state:draft}}\n\n## 비평\n{{state:critique}}",
             "system_prompt": "비평을 모두 반영해 최종본을 쓰세요."},
            {"id": "result", "kind": "output", "label": "최종본", "x": 1140, "y": 220},
        ],
        "edges": [
            {"from": "request", "to": "writer"}, {"from": "writer", "to": "critic"},
            {"from": "critic", "to": "refiner"}, {"from": "refiner", "to": "result"},
        ],
        "settings": {"max_steps": 6, "parallelism": 1},
    }


def _evaluator_optimizer() -> dict:
    # Anthropic evaluator-optimizer / Google ADK LoopAgent: generate -> evaluate -> (revise) loop until approved.
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "과제", "x": 40, "y": 200},
            {"id": "generator", "kind": "agent", "label": "생성", "x": 320, "y": 200, "max_runs": 4,
             "output_key": "candidate", "role": "결과물 생성, 피드백을 받으면 개선",
             "system_prompt": "과제를 해결하는 결과물을 만드세요. 리뷰 피드백이 있으면 모든 지적을 반영하세요."},
            {"id": "evaluator", "kind": "agent", "label": "평가", "x": 620, "y": 200, "max_runs": 4,
             "role": "기준에 따라 평가해 승인 또는 수정 요청", "accepts": ["request", "agent"],
             "system_prompt": "정확성·완결성·명확성 기준으로 평가하세요. 기준 미달이면 revise와 구체적 개선점, 충족하면 approve."},
            {"id": "result", "kind": "output", "label": "결과", "x": 920, "y": 220},
        ],
        "edges": [
            {"from": "request", "to": "generator"}, {"from": "request", "to": "evaluator"},
            {"from": "generator", "to": "evaluator"},
            {"from": "evaluator", "to": "generator", "when": "revise", "label": "개선 요청"},
            {"from": "evaluator", "to": "result", "when": "approve", "label": "승인"},
        ],
        "settings": {"max_steps": 10, "parallelism": 1},
    }


def _mock_review() -> dict:
    graph = _evaluator_optimizer()
    responses = {
        "generator": [{"content": "초안"}, {"content": "피드백을 반영한 초안"}],
        "evaluator": [{"content": "검증 단계를 추가하세요.", "verdict": "revise"},
                      {"content": "모의 검토 승인", "verdict": "approve"}],
    }
    for node in graph["nodes"]:
        if node["kind"] == "agent":
            node.update(mode="mock", mock_responses=responses[node["id"]])
    return graph


def _voting() -> dict:
    # Anthropic parallelization (voting) / Google ADK ParallelAgent + gather: same task, independent attempts.
    nodes = [{"id": "request", "kind": "input", "label": "과제", "x": 40, "y": 220}]
    edges = []
    for i in range(3):
        vid = f"voter_{i + 1}"
        nodes.append({"id": vid, "kind": "agent", "label": f"독립 시도 {i + 1}", "x": 330, "y": 40 + i * 180,
                      "role": "과제를 독립적으로 해결", "can_message": [],
                      "system_prompt": "다른 시도를 참고하지 말고 독립적으로 해결한 뒤, 답과 확신도(0~1)를 쓰세요."})
        edges += [{"from": "request", "to": vid}, {"from": vid, "to": "aggregator"}]
    nodes += [{"id": "aggregator", "kind": "agent", "label": "집계(다수결)", "x": 660, "y": 220,
               "role": "독립 시도들을 비교해 합의안 도출", "can_message": [],
               "system_prompt": "시도들의 답을 비교해 다수결 또는 근거가 가장 강한 답을 고르고, 불일치 지점을 설명하세요."},
              {"id": "result", "kind": "output", "label": "결과", "x": 960, "y": 240}]
    edges.append({"from": "aggregator", "to": "result"})
    return {"nodes": nodes, "edges": edges, "settings": {"max_steps": 8, "parallelism": 3}}


def _multimodal_review() -> dict:
    # Multimodal fan-out: specialists each receive only the modalities they handle, then a synthesizer merges.
    return {
        "nodes": [
            {"id": "request", "kind": "input", "label": "요청 + 첨부", "x": 40, "y": 240,
             "default_request": "첨부한 자료(이미지·영상·음성·PDF)를 분석해 핵심 내용과 개선점을 정리해 주세요."},
            {"id": "vision", "kind": "agent", "label": "시각 분석", "x": 340, "y": 40, "modalities": ["image", "video"],
             "role": "이미지·영상 프레임 분석", "system_prompt": "화면 구성, 텍스트, 오류, 사용성 문제를 시각 근거와 함께 설명하세요."},
            {"id": "audio", "kind": "agent", "label": "음성 분석", "x": 340, "y": 230, "modalities": ["audio", "video"],
             "role": "음성·영상 음성 트랙 분석", "system_prompt": "전사본(또는 원본)에서 발언 요지, 결정 사항, 할 일을 정리하세요."},
            {"id": "docs", "kind": "agent", "label": "문서 분석", "x": 340, "y": 420, "modalities": ["pdf", "text", "file"],
             "role": "PDF·텍스트 문서 분석", "system_prompt": "문서의 구조와 핵심 주장, 수치, 요구사항을 정리하세요."},
            {"id": "synth", "kind": "agent", "label": "종합", "x": 680, "y": 230, "modalities": [],
             "role": "모달리티별 분석을 하나로 종합",
             "system_prompt": "각 분석을 교차 검증해 하나의 보고서로 정리하고, 모달리티 사이 모순은 해당 분석가에게 질문하세요."},
            {"id": "result", "kind": "output", "label": "보고서", "x": 980, "y": 250},
        ],
        "edges": [
            {"from": "request", "to": "vision"}, {"from": "request", "to": "audio"}, {"from": "request", "to": "docs"},
            {"from": "vision", "to": "synth"}, {"from": "audio", "to": "synth"}, {"from": "docs", "to": "synth"},
            {"from": "synth", "to": "result"},
        ],
        "settings": {"max_steps": 12, "parallelism": 3},
    }


_PRESETS = {
    "mock_review": (_mock_review, "모의 에이전트 리뷰 테스트", "IRADE",
                    "모델 호출 없이 초안 → 수정 요청 → 승인 흐름을 검증"),
    "review_loop": (_review_loop, "기획→구현→리뷰 루프", "IRADE", "plan → code → review(revise 루프)"),
    "orchestrator_workers": (_orchestrator_workers, "오케스트레이터-워커 (리서치)", "Anthropic",
                             "리드 에이전트가 하위 작업을 동적으로 배정(spawn)하고 병렬 결과를 종합"),
    "evaluator_optimizer": (_evaluator_optimizer, "평가자-최적화 루프", "Anthropic · Google ADK LoopAgent",
                            "생성 → 평가 → 개선 반복, 승인 시 종료"),
    "voting": (_voting, "병렬 투표 (다수결)", "Anthropic · Google ADK ParallelAgent",
               "같은 과제를 독립적으로 여러 번 풀고 집계"),
    "parallel": (_parallel, "병렬 분석→종합 (섹셔닝)", "Anthropic · Google ADK ParallelAgent",
                 "관점별 전문가가 동시에 분석하고 종합"),
    "handoff_router": (_handoff_router, "트리아지 + 핸드오프", "OpenAI Agents SDK · Google ADK transfer",
                       "분류 에이전트가 전문 에이전트에게 대화를 넘김(handoff)"),
    "guardrails": (_guardrails, "입력·출력 가드레일", "OpenAI Agents SDK",
                   "가드레일이 정책 위반을 감지하면 tripwire로 실행 중단(BLOCKED)"),
    "sequential_state": (_sequential_state, "순차 파이프라인 + 공유 상태", "Google ADK SequentialAgent",
                         "output_key로 상태에 쓰고 {{state:key}}로 읽는 단계별 파이프라인"),
    "multimodal_review": (_multimodal_review, "멀티모달 분석 팀", "IRADE",
                          "이미지·영상·음성·PDF를 모달리티별 전문가가 나눠 분석 후 종합"),
    "debate": (_debate, "찬반 토론→심판", "IRADE", "두 에이전트가 메시지로 반박하고 심판이 결론"),
}
PRESETS = tuple(_PRESETS)
PRESET_INFO = {k: {"title": v[1], "vendor": v[2], "desc": v[3]} for k, v in _PRESETS.items()}


def preset(name: str) -> dict:
    """Starter teams offered by the canvas toolbar (defaults to the review loop)."""
    return _PRESETS.get(name, _PRESETS["review_loop"])[0]()
