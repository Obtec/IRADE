"""Multi-agent teams orchestrated from a visual canvas graph.

A *team graph* is what the console's multi-agent canvas edits::

    {"nodes": [...], "edges": [...], "settings": {...}}

Node kinds
  input   the user request entering the team (``default_request`` is used when a run gives none)
  data    a data source resolved once at run start (spec scope, 2D UX layout, repo files, cells, text …)
  agent   a CLI/LLM agent: role, default system prompt, prompt template, which input kinds it accepts,
          wait-for-all or react-to-any inputs, think (JSON answer) or code (autonomous run in the repo)
  output  a sink that collects the team's final results

Edges carry data from ``from`` to ``to``. ``when`` makes them conditional on the source agent's verdict:
  always   fires on every completion
  approve  fires when the agent approves (or gives no verdict)
  revise   fires when the agent asks for a revision; this is the review/feedback loop edge

Agents also collaborate directly: an agent may address messages to teammates (``messages`` in think mode,
``<<TO:id>>…<</TO>>`` markers in code mode). A delivered message re-activates the recipient, so agents can
ask each other questions and answer them, bounded by per-node ``max_runs`` and the team ``max_steps``.

The scheduler (:class:`TeamRun`) is pure Python: model calls and data lookups are injected, so it is unit
tested without a VM or database. ``multi_agent_service`` wires it to the workspace VM CLIs and PostgreSQL.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

NODE_KINDS = ("input", "data", "agent", "output")
EDGE_WHEN = ("always", "approve", "revise")
INPUT_KINDS = ("request", "data", "agent", "feedback", "message")
AGENT_MODES = ("think", "code")
DATA_SOURCES = ("spec_scope", "ux_layout", "repo_files", "cells", "collab_history", "text")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
CLIS = ("", "claude", "codex", "agy")
VERDICTS = ("approve", "revise", "none")

MAX_NODES, MAX_EDGES = 40, 120
MAX_MESSAGES_PER_TURN, MAX_MESSAGE_CHARS = 5, 4000
MAX_INPUT_CHARS = 16000

_ID_RX = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_MODEL_RX = re.compile(r"[A-Za-z0-9._:/\[\]-]{1,80}")
_VIEW_RX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_PATH_RX = re.compile(r"[A-Za-z0-9_./-]{1,200}")


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
        key = (a, b, when)
        if key in seen:
            raise TeamGraphError(f"duplicate edge {a}->{b} ({when})")
        seen.add(key)
        eid = str(e.get("id") or f"e{i + 1}")
        edges.append({"id": eid[:40], "from": a, "to": b, "when": when, "label": _s(e.get("label"), 80)})
    for n in nodes:
        if n["kind"] == "agent" and not any(e["to"] == n["id"] for e in edges):
            raise TeamGraphError(f"agent {n['id']} has no incoming edge (connect an input, data or agent node)")
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

_VERDICT_RX = re.compile(r"<<VERDICT:(APPROVE|REVISE)>>", re.I)
_TO_RX = re.compile(r"<<TO:([A-Za-z][A-Za-z0-9_-]{0,39})>>(.*?)<</TO>>", re.S)


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
                           [m for m in msgs if isinstance(m, dict)])
    text = str(raw or "")
    verdict = "none"
    for m in _VERDICT_RX.finditer(text):
        verdict = m.group(1).lower()
    messages = [{"to": m.group(1), "content": m.group(2).strip()} for m in _TO_RX.finditer(text)]
    content = _TO_RX.sub("", _VERDICT_RX.sub("", text)).strip()
    return AgentOutput(content, verdict, messages)


def _trim(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 40] + f"\n…(잘림, 총 {len(text)}자)"


DEFAULT_TEMPLATE = "{{auto}}"
_PLACEHOLDER_RX = re.compile(r"\{\{\s*(request|data|agents|feedback|messages|blackboard|inputs|auto|input:[A-Za-z0-9_-]+)\s*\}\}")


def _team_roster(graph: dict, me: dict) -> str:
    rows = []
    for n in graph["nodes"]:
        if n["kind"] != "agent":
            continue
        tag = " (나)" if n["id"] == me["id"] else ""
        rows.append(f"- `{n['id']}` {n['label']}{tag}: {n.get('role') or '역할 미지정'}")
    return "\n".join(rows)


def _allowed_targets(graph: dict, me: dict) -> list[str]:
    agents = [n["id"] for n in graph["nodes"] if n["kind"] == "agent" and n["id"] != me["id"]]
    return agents if me.get("can_message", "*") == "*" else [a for a in agents if a in me["can_message"]]


def build_prompt(graph: dict, node: dict, inbox: dict, request: str, blackboard: list[dict], run_no: int) -> str:
    """Compose: role + default system prompt + team roster + collaboration protocol + rendered template."""
    labels = {n["id"]: n["label"] for n in graph["nodes"]}
    groups: dict[str, list[str]] = {k: [] for k in INPUT_KINDS}
    per_source: dict[str, str] = {}
    for src, item in inbox.items():
        body = _trim(item["content"], MAX_INPUT_CHARS)
        per_source[item.get("node", src)] = body
        if item["kind"] == "request":
            continue
        head = f"### {labels.get(item.get('node'), item.get('node'))} (`{item.get('node')}`)"
        groups[item["kind"]].append(f"{head}\n{body}")
    sections = {
        "request": request or "(요청 없음)",
        "data": "\n\n".join(groups["data"]),
        "agents": "\n\n".join(groups["agent"]),
        "feedback": "\n\n".join(groups["feedback"]),
        "messages": "\n\n".join(groups["message"]),
        "blackboard": "\n".join(f"- [{labels.get(b['node'], b['node'])}] {_trim(b['content'], 600)}"
                                for b in blackboard[-12:]),
    }
    titles = [("request", "사용자 요청"), ("data", "입력 데이터"), ("agents", "동료 에이전트 결과"),
              ("feedback", "리뷰 피드백 (반영해서 다시 작업하세요)"), ("messages", "동료에게 받은 메시지")]
    if node.get("blackboard"):
        titles.append(("blackboard", "팀 공유 보드 (최근 활동)"))
    auto = "\n\n".join(f"## {t}\n{sections[k]}" for k, t in titles if sections[k])
    sections["inputs"] = auto

    def sub(m):
        key = m.group(1)
        if key == "auto":
            return auto
        if key.startswith("input:"):
            return per_source.get(key[6:], "(없음)")
        return sections.get(key) or "(없음)"

    body = _PLACEHOLDER_RX.sub(sub, node.get("prompt_template") or DEFAULT_TEMPLATE)
    targets = _allowed_targets(graph, node)
    reviewer = any(e["from"] == node["id"] and e["when"] != "always" for e in graph["edges"])
    if node.get("mode") == "code":
        protocol = [
            "- 저장소에서 직접 작업한 뒤, 마지막 응답에 팀에 전달할 결과 요약을 쓰세요.",
            f"- 동료에게 질문/요청: `<<TO:에이전트id>>내용<</TO>>` (가능한 대상: {', '.join(targets) or '없음'})",
        ]
        if reviewer:
            protocol.append("- 검토 역할이면 `<<VERDICT:APPROVE>>` 또는 `<<VERDICT:REVISE>>`를 반드시 포함하세요.")
    else:
        protocol = [
            "- JSON 하나로 답하세요: content(결과물 전체), verdict, messages[{to, content}].",
            f"- messages로 동료에게 질문·요청·답변을 보낼 수 있습니다 (가능한 대상: {', '.join(targets) or '없음'}).",
            "- verdict: 동료의 작업을 검토했다면 approve 또는 revise, 아니면 none." if reviewer
            else "- 검토 역할이 아니면 verdict는 none.",
        ]
    head = [f"# 당신은 멀티 에이전트 팀의 `{node['id']}` ({node['label']}) 입니다."]
    if node.get("role"):
        head.append(f"역할: {node['role']}")
    if node.get("system_prompt"):
        head.append(f"\n## 기본 지침\n{node['system_prompt']}")
    head.append(f"\n## 팀 구성\n{_team_roster(graph, node)}")
    head.append("\n## 협업 규칙\n" + "\n".join(protocol))
    if run_no > 1:
        head.append(f"\n(이번이 {run_no}번째 활성화입니다. 새로 받은 입력에 집중하세요.)")
    return "\n".join(head) + "\n\n---\n\n" + body


# ---------------------------------------------------------------- scheduler
Invoke = Callable[[dict, str, dict], object]          # (node, prompt, ctx) -> AgentOutput | dict | str
ResolveData = Callable[[dict], str]                   # (data node) -> text
OnEvent = Callable[[dict], None]


def back_edges(graph: dict) -> set[tuple[str, str]]:
    """Edges that close a cycle, found by DFS from the input/data nodes.

    ``revise`` edges are feedback by definition and explored last, so in a plan -> code -> review -> (revise)
    -> code loop the forward edges stay "required" and only the feedback edge is a back edge.
    """
    order = {"always": 0, "approve": 1, "revise": 2}
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
    (``parallelism``); results fan out along matching edges and direct messages.
    """

    def __init__(self, graph: dict, invoke: Invoke, resolve_data: ResolveData, request: str = "",
                 on_event: OnEvent | None = None, cancel: threading.Event | None = None):
        self.g = validate_team_graph(graph)
        self.nodes = {n["id"]: n for n in self.g["nodes"]}
        self.invoke, self.resolve_data = invoke, resolve_data
        self.on_event = on_event or (lambda ev: None)
        self.cancel = cancel or threading.Event()
        inputs = [n for n in self.g["nodes"] if n["kind"] == "input"]
        self.request = (request or "").strip() or next((n["default_request"] for n in inputs if n["default_request"]), "")
        self.inbox: dict[str, dict[str, dict]] = {nid: {} for nid in self.nodes}
        self.fresh: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        self.runs: dict[str, int] = {nid: 0 for nid in self.nodes}
        self.status: dict[str, str] = {nid: "idle" for nid in self.nodes}
        self.last: dict[str, AgentOutput] = {}
        self.results: dict[str, list[dict]] = {n["id"]: [] for n in self.g["nodes"] if n["kind"] == "output"}
        self.blackboard: list[dict] = []
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
        if item["kind"] not in node["accepts"]:
            self.emit("dropped", item.get("node"), f"{target}는 '{item['kind']}' 입력을 받지 않도록 설정됨", target=target)
            return
        self.inbox[target][source_key] = item
        self.fresh[target].add(source_key)
        self.emit("delivery", item.get("node"), "", target=target, kind=item["kind"])

    def _fan_out(self, src: str, out: AgentOutput) -> None:
        verdict = out.verdict
        for e in self.g["edges"]:
            if e["from"] != src:
                continue
            fire = (e["when"] == "always" or (e["when"] == "approve" and verdict in ("approve", "none"))
                    or (e["when"] == "revise" and verdict == "revise"))
            if not fire:
                continue
            kind = "feedback" if e["when"] == "revise" else "agent"
            self._deliver(e["to"], src, {"node": src, "kind": kind, "content": out.content, "verdict": verdict})
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

    def _ready(self, nid: str) -> bool:
        n = self.nodes[nid]
        if n["kind"] != "agent" or self.status[nid] == "running" or not self.fresh[nid]:
            return False
        if n["input_mode"] == "all":
            have = {k for k in self.inbox[nid] if not k.startswith("msg:")}
            if not self.required[nid] <= have:
                return False
        return True

    # -- main loop
    def run(self) -> dict:
        st = self.g["settings"]
        self.emit("run_started", None, self.request, agents=[n["id"] for n in self.g["nodes"] if n["kind"] == "agent"])
        for n in self.g["nodes"]:
            if n["kind"] == "input":
                self.status[n["id"]] = "done"
                for e in self.g["edges"]:
                    if e["from"] == n["id"]:
                        self._deliver(e["to"], n["id"], {"node": n["id"], "kind": "request", "content": self.request})
            elif n["kind"] == "data":
                self.status[n["id"]] = "running"
                try:
                    text = _trim(self.resolve_data(n), MAX_INPUT_CHARS)
                    self.status[n["id"]] = "done"
                    self.emit("data_resolved", n["id"], text, source=n["source"])
                except Exception as exc:  # a missing source must not silently look like empty data
                    text = f"(데이터 소스 {n['source']} 조회 실패: {exc})"
                    self.status[n["id"]] = "failed"
                    self.emit("data_failed", n["id"], str(exc), source=n["source"])
                for e in self.g["edges"]:
                    if e["from"] == n["id"]:
                        self._deliver(e["to"], n["id"], {"node": n["id"], "kind": "data", "content": text})

        failures: list[str] = []
        running: dict = {}
        outcome = None
        with ThreadPoolExecutor(max_workers=st["parallelism"], thread_name_prefix="agdd-team") as pool:
            while True:
                if self.cancel.is_set():
                    outcome = "CANCELLED"
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
                    self.steps += 1
                    self.runs[nid] += 1
                    inbox = dict(self.inbox[nid])
                    fresh = sorted(self.fresh[nid])
                    self.fresh[nid].clear()
                    for k in [k for k in self.inbox[nid] if k.startswith("msg:")]:
                        del self.inbox[nid][k]     # a message is consumed once; data/agent inputs persist
                    for k, item in list(self.inbox[nid].items()):
                        if item["kind"] == "feedback":
                            del self.inbox[nid][k]  # feedback applies to the run it triggered
                    prompt = build_prompt(self.g, node, inbox, self.request, self.blackboard, self.runs[nid])
                    self.status[nid] = "running"
                    self.emit("node_started", nid, "", run_no=self.runs[nid], fresh=fresh, mode=node["mode"])
                    ctx = {"run_no": self.runs[nid], "request": self.request, "settings": st}
                    running[pool.submit(self.invoke, node, prompt, ctx)] = (nid, time.time())
                if not running:
                    break
                done, _ = wait(list(running), timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    nid, t0 = running.pop(fut)
                    ms = int((time.time() - t0) * 1000)
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
                              messages=len(out.messages), **{k: v for k, v in out.meta.items() if k != "content"})
                    if outcome != "CANCELLED":  # still deliver (e.g. to outputs) after the step limit
                        self._fan_out(nid, out)
                if outcome == "CANCELLED" and not running:
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
        result = self.result_payload()
        self.emit("run_finished", None, "", status=outcome, steps=self.steps, failures=failures)
        return {"status": outcome, "steps": self.steps, "failures": failures, "result": result,
                "node_state": self.snapshot()}

    def result_payload(self) -> dict:
        outputs = {k: [i["content"] for i in v] for k, v in self.results.items()}
        if not self.results:  # no output node: the sink agents' last answers are the result
            sinks = [nid for nid, n in self.nodes.items() if n["kind"] == "agent"
                     and not any(e["from"] == nid for e in self.g["edges"]) and nid in self.last]
            outputs = {nid: [self.last[nid].content] for nid in sinks}
        return {"outputs": outputs, "final": {nid: o.content for nid, o in self.last.items()}}


# ---------------------------------------------------------------- presets
def preset(name: str) -> dict:
    """Starter teams offered by the canvas toolbar."""
    if name == "parallel":
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
    if name == "debate":
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
    # default: plan -> implement -> review loop
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


PRESETS = ("review_loop", "parallel", "debate")
