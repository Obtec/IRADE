"""API-key model agents for the workspace VM (replaces the former subscription-CLI agents).

Every AGDD model step goes through the LLM Gateway (OpenAI-compatible, server-side upstream API key,
``AGDD_UPSTREAM_API_KEY``), so budget gating, per-workspace rate limiting, cost accounting and prompt-cache
normalisation apply to all calls. The VM stays the execution sandbox: the coding agent's tools (read, grep,
edit, run) execute inside the workspace VM through a ``VmShell``.

  - structured(prompt, schema) -> JSON object (pre-flight, ambiguity, routing, semantic lifting, HITL cards …)
  - agent(task, cwd)           -> tool-calling coding loop in the repo (DEV node); resumable sessions are stored
                                  as transcripts in the VM (``~/.agdd/sessions/<id>.json``)
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from . import gateway
from .vm import VmShell, sh_quote

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Gateway model aliases (llm_gateway.ALIASES); any explicit upstream model name is also accepted.
MODEL_ALIASES = ("cheap", "mid", "advisor")
PERMISSIONS = ("sandbox", "ask-edits")
MAX_STEPS = 40
MAX_IMAGES = 12
IMAGE_BYTES = 5 * 1024 * 1024
TEXT_ATTACH_CHARS = 20_000
SESSION_MAX_TOKENS = 120_000
_IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_LEGACY_KEYS = ("cli",)  # settings rows saved before the API migration


@dataclass
class AgentSettings:
    model: str = "mid"                   # gateway alias (cheap | mid | advisor) or an upstream model name
    effort: str = "medium"
    session: str = "per-run"             # new | per-run | <explicit session id>
    permission: str = "sandbox"          # sandbox: edits + shell commands in the VM | ask-edits: file edits only
    cheap_model: str = "cheap"           # model for structured/cheap steps
    cheap_effort: str = "low"
    instructions: str = ""               # custom workspace agent tuning instructions
    default_repo: str = "workspace"      # default repo under /workspace/repos/
    ux_auto_canvas: bool = True          # pause & open 2D UX canvas on UX_VISUAL_REQUEST

    @classmethod
    def from_row(cls, row: dict | None) -> "AgentSettings":
        s = cls()
        for k, v in (row or {}).items():
            if k in _LEGACY_KEYS:
                continue
            if hasattr(s, k) and v is not None:
                setattr(s, k, v)
        s.validate()
        return s

    def validate(self):
        self.model = str(self.model or "").strip() or "mid"
        self.cheap_model = str(self.cheap_model or "").strip() or "cheap"
        if self.effort not in EFFORTS or self.cheap_effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        for m in (self.model, self.cheap_model):
            if not re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,80}", m):
                raise ValueError("invalid model name")
        if self.session not in ("new", "per-run") and not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", self.session):
            raise ValueError("session must be new, per-run or a session id")
        if self.permission not in PERMISSIONS:
            raise ValueError("permission must be sandbox or ask-edits")
        self.instructions = str(self.instructions or "").strip()[:4000]
        self.default_repo = str(self.default_repo or "workspace").strip() or "workspace"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.default_repo) or self.default_repo.startswith("."):
            raise ValueError("invalid default_repo")
        self.ux_auto_canvas = bool(self.ux_auto_canvas)


@dataclass
class MediaFile:
    """A file in the VM handed to a model turn. ``kind`` is the delivery type (image | pdf | text | video | audio |
    file); ``native`` marks originals (derived files such as extracted frames or transcripts are not native)."""
    path: str
    kind: str
    native: bool = True


class AgentError(RuntimeError):
    """The model API call itself failed (gateway down, upstream error): never treat as an empty/clean answer."""


@dataclass
class AgentResult:
    ok: bool
    text: str
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    raw_tail: str = ""
    model: str = ""
    steps: int = 0


# ---------------------------------------------------------------- gateway transport
def gateway_effort(effort: str) -> str:
    """OpenAI-compatible reasoning_effort accepts low | medium | high."""
    return {"xhigh": "high", "max": "high"}.get(effort, effort)


def chat_completion(body: dict, headers: dict) -> dict:
    """One chat.completions call through the LLM Gateway. Tests replace this function."""
    from openai import APIStatusError, OpenAI

    from .infra import env
    client = OpenAI(base_url=env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1"),
                    api_key=env("LLM_GATEWAY_TOKEN", "internal"), timeout=float(env("AGDD_MODEL_TIMEOUT", "300")))
    try:
        return client.chat.completions.create(**body, extra_headers=headers).model_dump()
    except APIStatusError as e:
        if e.status_code == 402:
            from .budget import BudgetHalt
            raise BudgetHalt(f"budget_halt: {e.message}") from e
        raise AgentError(f"model API error {e.status_code}: {str(e.message)[:300]}") from e
    except Exception as e:  # connection / timeout
        raise AgentError(f"model API unavailable: {e!r:.300}") from e


def _usage(resp: dict) -> dict:
    u = resp.get("usage") or {}
    reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    return {"prompt_tokens": u.get("prompt_tokens", 0) or 0,
            "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
            "output_tokens": (u.get("completion_tokens", 0) or 0) - reasoning, "thinking_tokens": reasoning}


def _add_usage(total: dict, u: dict) -> dict:
    for k, v in u.items():
        total[k] = total.get(k, 0) + v
    return total


def _cost(model: str, usage: dict) -> float:
    from . import llm_gateway
    return round(llm_gateway.cost_of(llm_gateway.resolve_model(model), usage), 6)


# ---------------------------------------------------------------- coding-agent tools (executed in the VM)
def _tool(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


READ_TOOLS = [
    _tool("list_files", "저장소 파일 목록 (git ls-files 또는 find, 최대 400개). pattern은 경로 부분 문자열/glob",
          {"pattern": {"type": "string"}}),
    _tool("read_file", "파일 일부 읽기 (줄 번호 포함, 최대 250줄)",
          {"path": {"type": "string"}, "start": {"type": "integer"}, "lines": {"type": "integer"}}, ["path"]),
    _tool("grep", "저장소에서 정규식 검색 (최대 80줄)", {"pattern": {"type": "string"}, "path": {"type": "string"}},
          ["pattern"]),
]
EDIT_TOOLS = [
    _tool("edit_file", "파일의 old_text(정확히 1회 등장)를 new_text로 바꿉니다", {
        "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"]),
    _tool("write_file", "새 파일 생성 또는 작은 파일 전체 교체", {"path": {"type": "string"}, "content": {"type": "string"}},
          ["path", "content"]),
]
SHELL_TOOLS = [
    _tool("run_command", "저장소 디렉터리에서 bash 명령 실행 (테스트, 빌드, agdd-spec 등). 성공은 1줄, 실패는 핵심 에러만",
          {"command": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"]),
]
FINISH_TOOL = _tool("finish", "작업 종료. summary에 최종 답변을 적습니다 (요청에 출력 형식·요약 규칙이 있으면 그대로 따르세요)",
                    {"summary": {"type": "string"}}, ["summary"])

AGENT_SYSTEM = """당신은 워크스페이스 VM 안의 저장소에서 작업하는 코딩 에이전트입니다. 작업 디렉터리: {cwd}
- 도구로만 저장소를 읽고 수정합니다. 파일 전체를 읽지 말고 grep / read_file 구간 읽기로 필요한 부분만 보세요.
- 수정은 edit_file(부분 치환)을 우선 사용하고, write_file은 새 파일이나 작은 파일에만 쓰세요.
{shell_rule}
- 작업이 끝나면 반드시 finish(summary)를 호출하세요."""
SHELL_RULE = "- run_command로 테스트와 프로젝트 명령을 실행할 수 있습니다 (VM이 격리 경계입니다)."
NO_SHELL_RULE = "- 이 워크스페이스는 파일 편집만 허용됩니다(셸 명령 실행 불가). 테스트 실행이 필요하면 요약에 적으세요."


def _safe_rel(path: str) -> str:
    p = str(path or "").strip().lstrip("/")
    if not p or ".." in p.split("/") or not re.fullmatch(r"[A-Za-z0-9._@+/ -]{1,300}", p):
        raise ValueError(f"invalid path: {path!r}")
    return p


class ApiAgent:
    """Model agent backed by the LLM Gateway; repository tools execute inside the workspace VM."""

    def __init__(self, shell: VmShell, settings: AgentSettings, workspace_id=None, run_id=None, node: str = ""):
        self.shell, self.s = shell, settings
        self.ws, self.run_id, self.node = workspace_id, run_id, node

    # -- plumbing
    def _headers(self, task: str) -> dict:
        return {"x-agdd-task": task, "x-agdd-workspace": str(self.ws or ""), "x-agdd-run": str(self.run_id or ""),
                "x-agdd-node": str(self.node or "")}

    def _log(self, text: str) -> None:
        if getattr(self.shell, "mirror", False) and hasattr(self.shell, "echo"):
            try:
                self.shell.echo(text)
            except Exception:
                pass

    def _media_parts(self, files) -> list[dict]:
        """Images go in as image parts, text-like files inline; other media are named so tools can inspect them."""
        parts, names, n_img = [], [], 0
        for f in files or ():
            ext = f.path.rsplit(".", 1)[-1].lower() if "." in f.path else ""
            try:
                if f.kind == "image" and ext in _IMAGE_MIME and n_img < MAX_IMAGES:
                    data = self.shell.read_file(f.path, max_bytes=IMAGE_BYTES + 1)
                    if len(data) <= IMAGE_BYTES:
                        n_img += 1
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"data:{_IMAGE_MIME[ext]};base64,{base64.b64encode(data).decode()}"}})
                        continue
                if f.kind == "text":
                    text = self.shell.read_file(f.path, max_bytes=TEXT_ATTACH_CHARS * 4).decode(errors="replace")
                    parts.append({"type": "text", "text": f"첨부 `{f.path}`:\n```\n{text[:TEXT_ATTACH_CHARS]}\n```"})
                    continue
            except (FileNotFoundError, RuntimeError):
                pass
            names.append(f"`{f.path}` ({f.kind})")
        if names:
            parts.append({"type": "text", "text": "VM에 있는 첨부 파일: " + ", ".join(names)})
        return parts

    @staticmethod
    def _user(prompt: str, parts: list[dict]):
        return [{"type": "text", "text": prompt}, *parts] if parts else prompt

    # -- structured JSON steps
    def structured(self, prompt: str, schema: dict, timeout: float = 900, files=(), task: str = "classify"
                   ) -> tuple[dict, AgentResult]:
        from .infra import parse_json
        from .semantic_cache import cache_lookup, cache_store
        ckey = (f"api:{self.ws or '-'}", self.s.cheap_model, self.s.cheap_effort)
        hit = cache_lookup(ckey, prompt, schema) if not files else None
        if hit is not None:  # identical question (e.g. pre-flight on unchanged specs): no model call
            return hit, AgentResult(ok=True, text="", model="cache")
        body = {"model": self.s.cheap_model, "reasoning_effort": gateway_effort(self.s.cheap_effort),
                "messages": [{"role": "user", "content": self._user(prompt, self._media_parts(files))}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}}
        resp = chat_completion(body, self._headers(task))
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        data = parse_json(text)
        usage = _usage(resp)
        res = AgentResult(ok=isinstance(data, dict) and bool(data or text.strip().startswith("{")), text=text,
                          usage=usage, cost_usd=_cost(self.s.cheap_model, usage), model=resp.get("model") or self.s.cheap_model)
        if not res.ok:
            raise AgentError(f"structured call returned no JSON: {text[:300]!r}")
        cache_store(ckey, prompt, schema, data)
        return data, res

    def cheap_llm(self, on_result=None, task: str = "classify"):
        """Adapter matching the ``llm(prompt, schema) -> dict`` interface used by preflight/router/hitl/…"""
        def call(prompt, schema):
            data, res = self.structured(prompt, schema, task=task)
            if on_result:
                on_result(res)
            return data
        return call

    # -- sessions (transcripts in the VM so retries can resume from any worker process)
    def _session_path(self, sid: str) -> str:
        return f"{self.shell.home}/.agdd/sessions/{sid}.json"

    def _load_session(self, sid: str | None) -> list[dict]:
        if not sid or not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", sid):
            return []
        try:
            msgs = json.loads(self.shell.read_file(self._session_path(sid), max_bytes=8 * 1024 * 1024))
            return msgs if isinstance(msgs, list) else []
        except (FileNotFoundError, ValueError, RuntimeError):
            return []

    def _save_session(self, sid: str, messages: list[dict]) -> None:
        msgs, _ = gateway.batch_clear(messages, threshold=SESSION_MAX_TOKENS)
        try:
            self.shell.put_file(self._session_path(sid), json.dumps(msgs, ensure_ascii=False))
        except Exception:
            pass

    # -- tools
    def _run_tool(self, name: str, args: dict, cwd: str) -> str:
        from . import watchdog
        if name == "list_files":
            pat = str(args.get("pattern") or "")
            cmd = (f"cd {sh_quote(cwd)} && (git ls-files 2>/dev/null || find . -type f -not -path './.git/*' | sed 's|^./||')"
                   + (f" | grep -F -- {sh_quote(pat.replace('*', ''))}" if pat else "") + " | head -400")
            return self.shell.run(cmd, timeout=60).output or "(파일 없음)"
        if name == "read_file":
            rel = _safe_rel(args.get("path"))
            start = max(1, int(args.get("start") or 1))
            n = max(1, min(250, int(args.get("lines") or 250)))
            r = self.shell.run(f"cd {sh_quote(cwd)} && test -f {sh_quote(rel)} && "
                               f"awk 'NR>={start} && NR<{start + n} {{printf \"%6d  %s\\n\", NR, $0}}' {sh_quote(rel)} && "
                               f"echo \"[총 $(wc -l < {sh_quote(rel)})줄]\"", timeout=60)
            return r.output if r.exit_code == 0 else f"파일을 읽을 수 없습니다: {rel}"
        if name == "grep":
            path = _safe_rel(args["path"]) if args.get("path") else "."
            r = self.shell.run(f"cd {sh_quote(cwd)} && grep -rnE --exclude-dir=.git --exclude-dir=node_modules "
                               f"-- {sh_quote(str(args.get('pattern') or ''))} {sh_quote(path)} | head -80", timeout=60)
            return r.output or "(일치 없음)"
        if name in ("edit_file", "write_file"):
            rel = _safe_rel(args.get("path"))
            if watchdog.is_protected(rel):
                return f"거부: 테스트 파일은 읽기 전용입니다 ({rel})"
            full = f"{cwd.rstrip('/')}/{rel}"
            if name == "write_file":
                content = str(args.get("content") or "")
            else:
                try:
                    cur = self.shell.read_file(full).decode()
                except FileNotFoundError:
                    return f"파일이 없습니다: {rel} (새 파일은 write_file)"
                old = str(args.get("old_text") or "")
                cnt = cur.count(old) if old else 0
                if cnt != 1:
                    return f"old_text가 {cnt}회 등장합니다. 정확히 1회 등장하도록 주변 줄을 더 포함하세요."
                content = cur.replace(old, str(args.get("new_text") or ""), 1)
            self.shell.put_file(full, content)
            self._log(f"✎ {name} {rel}")
            return f"OK {name} {rel} ({content.count(chr(10)) + 1}줄)"
        if name == "run_command":
            if self.s.permission != "sandbox":
                return "거부: 이 워크스페이스는 셸 명령 실행이 허용되지 않습니다 (permission=ask-edits)."
            command = str(args.get("command") or "").strip()
            if not command:
                return "빈 명령"
            timeout = max(10, min(1800, int(args.get("timeout") or 600)))
            self._log(f"$ {command[:300]}")
            r = self.shell.run(f"cd {sh_quote(cwd)} && {command}", timeout=timeout)
            return gateway.parse_tool_output(command[:200], r.exit_code, r.output)
        return f"알 수 없는 도구: {name}"

    # -- coding agent
    def agent(self, task: str, cwd: str, session: str | None, timeout: float = 3600, on_progress=None,
              mcp: bool = True, files=()) -> AgentResult:
        """Tool-calling loop. ``mcp`` is accepted for interface compatibility (no MCP servers via the gateway)."""
        model = self.s.model
        tools = READ_TOOLS + EDIT_TOOLS + (SHELL_TOOLS if self.s.permission == "sandbox" else []) + [FINISH_TOOL]
        sid = session if session and re.fullmatch(r"[A-Za-z0-9_-]{6,80}", session) else uuid.uuid4().hex
        system = AGENT_SYSTEM.format(cwd=cwd, shell_rule=SHELL_RULE if self.s.permission == "sandbox" else NO_SHELL_RULE)
        history = self._load_session(session)
        if not history:
            history = [{"role": "system", "content": system}]
        history.append({"role": "user", "content": self._user(task, self._media_parts(files))})
        usage, steps, summary, ok = {}, 0, "", False
        started = last_keep = time.monotonic()
        deadline = started + timeout
        self._log(f"▶ agent model={model} effort={self.s.effort} session={sid}")
        try:
            while steps < MAX_STEPS:
                now = time.monotonic()
                if now > deadline:
                    summary = f"timeout after {timeout}s"
                    break
                steps += 1
                if on_progress and now - last_keep > 5:  # liveness heartbeat for long runs; never fatal
                    last_keep = now
                    try:
                        on_progress()
                    except Exception:
                        pass
                body = {"model": model, "messages": history, "tools": tools,
                        "reasoning_effort": gateway_effort(self.s.effort)}
                resp = chat_completion(body, self._headers("implement"))
                _add_usage(usage, _usage(resp))
                msg = ((resp.get("choices") or [{}])[0].get("message") or {})
                calls = msg.get("tool_calls") or []
                entry = {"role": "assistant", "content": msg.get("content") or ""}
                if calls:
                    entry["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                        "name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}} for c in calls]
                history.append(entry)
                if msg.get("content"):
                    self._log(str(msg["content"])[:600])
                if not calls:  # plain answer without tools ends the turn
                    summary, ok = str(msg.get("content") or ""), True
                    break
                finished = False
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if name == "finish":
                        summary, ok, finished = str(args.get("summary") or ""), True, True
                        out = "OK"
                    else:
                        try:
                            out = self._run_tool(name, args if isinstance(args, dict) else {}, cwd)
                        except (ValueError, KeyError, TypeError) as e:
                            out = f"도구 인자 오류: {e}"
                    history.append({"role": "tool", "tool_call_id": c["id"], "content": gateway.hard_cap(str(out))})
                if finished:
                    break
            else:
                summary = summary or f"최대 단계({MAX_STEPS}) 도달 — 작업이 끝나지 않았습니다"
        except AgentError as e:
            summary, ok = str(e), False
        finally:
            self._save_session(sid, history)
        self._log(f"■ agent {'done' if ok else 'stopped'} · steps={steps}")
        return AgentResult(ok=ok, text=summary, session_id=sid, usage=usage, cost_usd=_cost(model, usage),
                           raw_tail=summary[-2000:], model=model, steps=steps)
