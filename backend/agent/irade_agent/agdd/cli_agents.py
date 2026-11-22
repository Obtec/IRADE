"""Coding-agent CLIs inside the workspace VM: Claude Code (`claude`), Codex (`codex`), Antigravity (`agy`).

Flags were verified against the installed CLIs (claude 2.1.329, codex-cli 0.171.0) and the Antigravity
headless docs. Every AGDD model step can run through one of these CLIs:
  - structured(prompt, schema)  -> JSON object (pre-flight, ambiguity, routing, semantic lifting, HITL cards …)
  - agent(task, cwd)            -> autonomous coding run in the repo (DEV node), resumable sessions
"""
from __future__ import annotations

import json
import re
import shlex
import uuid
from dataclasses import dataclass, field

from .vm import VmShell, sh_quote

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The Agora guest image ships without curl/git and with empty apt lists, so `apt update` must run before
# install (otherwise "Unable to locate package curl"); apt goes through the guest's unprivileged package bridge.
PREREQ = ("{ command -v curl >/dev/null && command -v git >/dev/null; } || "
          "{ apt update -q && apt install -y -q curl git ca-certificates; } && ")


@dataclass
class AgentSettings:
    cli: str = "claude"                  # claude | codex | agy
    model: str = ""                      # empty = CLI default
    effort: str = "medium"
    session: str = "per-run"             # new | per-run | <explicit session id>
    permission: str = "sandbox"          # sandbox: full autonomy inside the VM | ask-edits: edits only, no shell
    cheap_model: str = ""                # model for structured/cheap steps (empty = same as model)
    cheap_effort: str = "low"
    instructions: str = ""               # custom workspace agent tuning instructions
    default_repo: str = "workspace"      # default repo under /workspace/repos/
    ux_auto_canvas: bool = True          # pause & open 2D UX canvas on UX_VISUAL_REQUEST

    @classmethod
    def from_row(cls, row: dict | None) -> "AgentSettings":
        s = cls()
        for k, v in (row or {}).items():
            if hasattr(s, k) and v is not None:
                setattr(s, k, v)
        s.validate()
        return s

    def validate(self):
        if self.cli not in PROFILES:
            raise ValueError(f"cli must be one of {sorted(PROFILES)}")
        if self.effort not in EFFORTS or self.cheap_effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        for m in (self.model, self.cheap_model):
            if m and not re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,80}", m):
                raise ValueError("invalid model name")
        if self.session not in ("new", "per-run") and not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", self.session):
            raise ValueError("session must be new, per-run or a session id")
        if self.permission not in ("sandbox", "ask-edits"):
            raise ValueError("permission must be sandbox or ask-edits")
        self.instructions = str(self.instructions or "").strip()[:4000]
        self.default_repo = str(self.default_repo or "workspace").strip() or "workspace"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.default_repo) or self.default_repo.startswith("."):
            raise ValueError("invalid default_repo")
        self.ux_auto_canvas = bool(self.ux_auto_canvas)


MAX_CLI_IMAGES = 12


@dataclass
class MediaFile:
    """A file in the VM handed to a CLI turn. ``kind`` is the delivery type (image | pdf | text | video | audio |
    file); ``native`` marks originals a multimodal-native CLI should receive as-is (derived files such as
    extracted frames or transcripts are not native)."""
    path: str
    kind: str
    native: bool = True


class CliError(RuntimeError):
    """The CLI call itself failed (not logged in, quota, crash): never treat as an empty/clean answer."""


@dataclass
class AgentResult:
    ok: bool
    text: str
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    raw_tail: str = ""
    rate_limits: dict | None = None
    model: str = ""


class CliProfile:
    name = ""
    binary = ""
    install = ""
    login_hint = ""
    models: tuple = ()
    cheap_default = ""  # DOC §6.2 model routing: classify/check/lift/summarize steps use a low-cost model

    def version_cmd(self):
        return f"{self.binary} --version"

    def structured_cmd(self, prompt_file, schema_file, model, effort, files=()):
        raise NotImplementedError

    def agent_cmd(self, prompt_file, cwd, model, effort, session, permission, mcp=True, files=()):
        raise NotImplementedError

    def prompt_suffix(self, files) -> str:
        """Text appended to the prompt so the CLI attaches ``files`` (MediaFile list) natively, if it can."""
        return ""

    def parse(self, output: str) -> AgentResult:
        raise NotImplementedError

    def sessions_cmd(self) -> str:
        raise NotImplementedError

    def usage_cmd(self) -> str:
        raise NotImplementedError

    def interactive_cmd(self, cwd: str, model: str, effort: str, session: str | None, permission: str) -> str:
        raise NotImplementedError


def _json_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _last_json(text: str):
    last = None
    for obj in _json_lines(text):
        last = obj
    if last is None:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                last = json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                pass
    return last


class Claude(CliProfile):
    name, binary = "claude", "claude"
    install = PREREQ + "curl -fsSL https://claude.ai/install.sh | bash"
    login_hint = "claude  (브라우저 로그인 URL이 출력되면 열어서 코드 붙여넣기)  또는  claude setup-token"
    models = ("opus", "sonnet", "haiku", "fable")
    cheap_default = "haiku"

    def _common(self, model, effort):
        # stable system prompt (per-machine cwd/env/git sections moved out) -> prompt-cache prefix reuse
        return ((f" --model {shlex.quote(model)}" if model else "") + f" --effort {effort}"
                " --exclude-dynamic-system-prompt-sections")

    @staticmethod
    def _dirs(files) -> str:
        dirs = sorted({f.path.rsplit("/", 1)[0] for f in files if "/" in f.path})
        return "".join(f" --add-dir {sh_quote(d)}" for d in dirs)

    def structured_cmd(self, prompt_file, schema_file, model, effort, files=()):
        # pure JSON transforms: no built-in tools and no MCP servers -> no tool schemas in the prompt. With media
        # attached only the read-only Read tool is enabled: it views images and PDFs natively.
        tools = (f" --tools Read --allowedTools Read{self._dirs(files)}" if files else " --tools \"\"")
        return (f"claude -p --output-format json --no-session-persistence --permission-mode dontAsk"
                f"{tools} --strict-mcp-config"
                f"{self._common(model, effort)} --json-schema \"$(cat {sh_quote(schema_file)})\" < {sh_quote(prompt_file)}")

    def agent_cmd(self, prompt_file, cwd, model, effort, session, permission, mcp=True, files=()):
        perm = "--permission-mode bypassPermissions" if permission == "sandbox" else "--permission-mode acceptEdits"
        sess = f" --resume {shlex.quote(session)}" if session else ""
        no_mcp = "" if mcp else " --strict-mcp-config"  # graph node allowed_mcp is empty -> load no MCP servers
        return (f"cd {sh_quote(cwd)} && claude -p --output-format stream-json --verbose {perm}"
                f"{self._common(model, effort)}{self._dirs(files)}{no_mcp}{sess} < {sh_quote(prompt_file)}")

    def parse(self, output):
        result, rate = None, None
        for obj in _json_lines(output):
            if obj.get("type") == "result":
                result = obj
            if "rate_limit" in str(obj.get("type", "")) or obj.get("rate_limit_info"):
                rate = obj.get("rate_limit_info") or obj
        if result is None:
            result = _last_json(output) or {}
        usage = result.get("usage") or {}
        text = result.get("result")
        if result.get("structured_output") is not None:
            text = json.dumps(result["structured_output"], ensure_ascii=False)
        return AgentResult(ok=not result.get("is_error", not result) and result.get("subtype", "success") == "success",
                           text=text if isinstance(text, str) else json.dumps(text or "", ensure_ascii=False),
                           session_id=result.get("session_id"),
                           usage={"prompt_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                                  + usage.get("cache_creation_input_tokens", 0),
                                  "cached_tokens": usage.get("cache_read_input_tokens", 0),
                                  "output_tokens": usage.get("output_tokens", 0), "thinking_tokens": 0},
                           cost_usd=result.get("total_cost_usd"), raw_tail=output[-2000:], rate_limits=rate)

    def sessions_cmd(self):
        return ("for f in $(ls -t $HOME/.claude/projects/*/*.jsonl 2>/dev/null | head -30); do "
                "echo \"$(basename $f .jsonl)\t$(stat -c %Y $f)\t$(dirname $f | xargs basename)\"; done")

    def usage_cmd(self):
        # Plan limits are not exposed headlessly; rate_limit_event frames captured during runs fill the gauge.
        return "claude auth status 2>&1; echo '---'; timeout 120 claude -p /usage < /dev/null 2>&1 | tail -40"

    def interactive_cmd(self, cwd: str, model: str, effort: str, session: str | None, permission: str) -> str:
        perm = "--permission-mode bypassPermissions" if permission == "sandbox" else "--permission-mode acceptEdits"
        sess = f" --resume {shlex.quote(session)}" if session and session not in ("new", "per-run") else ""
        return f"cd {shlex.quote(cwd)} && claude {perm}{self._common(model, effort)}{sess}"


class Codex(CliProfile):
    name, binary = "codex", "codex"
    install = (PREREQ + "mkdir -p $HOME/.local/bin && cd /tmp && "
               "curl -fsSL -o codex.tgz https://github.com/openai/codex/releases/latest/download/"
               "codex-x86_64-unknown-linux-musl.tar.gz && tar xzf codex.tgz && "
               "mv codex-x86_64-unknown-linux-musl $HOME/.local/bin/codex && chmod +x $HOME/.local/bin/codex && rm codex.tgz")
    login_hint = "codex login --device-auth  (표시된 URL과 코드로 로그인)"
    models = ("gpt-5.5", "gpt-5.5-codex", "gpt-5-mini")
    cheap_default = "gpt-5-mini"

    def _common(self, model, effort):
        eff = {"xhigh": "xhigh", "max": "xhigh"}.get(effort, effort)
        return (f" -m {shlex.quote(model)}" if model else "") + f" -c model_reasoning_effort={eff}"

    @staticmethod
    def _images(files) -> str:
        # --image=<file> (one value per flag) so the trailing "-" (prompt on stdin) is never taken as an image
        imgs = [f.path for f in files if f.kind == "image"][:MAX_CLI_IMAGES]
        return "".join(f" --image={sh_quote(p)}" for p in imgs)

    def structured_cmd(self, prompt_file, schema_file, model, effort, files=()):
        out = f"{prompt_file}.last"
        emit = ("python3 -c 'import json,sys;print(json.dumps({\"agdd_last_message\": open(sys.argv[1]).read()}))' "
                f"{sh_quote(out)} 2>/dev/null")
        return (f"codex exec --json --ephemeral --skip-git-repo-check -s read-only -c 'mcp_servers={{}}'{self._common(model, effort)} "
                f"{self._images(files)} --output-schema {sh_quote(schema_file)} -o {sh_quote(out)} - < {sh_quote(prompt_file)}; "
                f"rc=$?; {emit}; exit $rc")

    def agent_cmd(self, prompt_file, cwd, model, effort, session, permission, mcp=True, files=()):
        sandbox = "--dangerously-bypass-approvals-and-sandbox" if permission == "sandbox" else "-s workspace-write"
        if not mcp:
            sandbox += " -c 'mcp_servers={}'"
        if session:
            return (f"cd {sh_quote(cwd)} && codex exec resume {shlex.quote(session)} --json {sandbox}"
                    f"{self._common(model, effort)}{self._images(files)} - < {sh_quote(prompt_file)}")
        return (f"cd {sh_quote(cwd)} && codex exec --json {sandbox}{self._common(model, effort)}{self._images(files)} "
                f"- < {sh_quote(prompt_file)}")

    def parse(self, output):
        sid, text, usage, ok, rate = None, "", {}, False, None
        for obj in _json_lines(output):
            t = obj.get("type", "")
            if t == "thread.started":
                sid = obj.get("thread_id")
            elif t == "item.completed" and (obj.get("item") or {}).get("type") in ("agent_message", "assistant_message"):
                text = obj["item"].get("text", text)
            elif t == "turn.completed":
                ok = True
                u = obj.get("usage") or {}
                usage = {"prompt_tokens": u.get("input_tokens", 0), "cached_tokens": u.get("cached_input_tokens", 0),
                         "output_tokens": u.get("output_tokens", 0), "thinking_tokens": u.get("reasoning_output_tokens", 0)}
            elif t == "turn.failed" or t == "error":
                ok = False
                text = json.dumps(obj.get("error") or obj, ensure_ascii=False)
            if "agdd_last_message" in obj and obj["agdd_last_message"]:
                text = obj["agdd_last_message"]
            if obj.get("rate_limits"):
                rate = obj["rate_limits"]
        return AgentResult(ok=ok, text=text, session_id=sid, usage=usage, raw_tail=output[-2000:], rate_limits=rate)

    def sessions_cmd(self):
        return ("find $HOME/.codex/sessions -name '*.jsonl' -printf '%T@ %p\\n' 2>/dev/null | sort -nr | head -30 | "
                "while read t f; do id=$(basename \"$f\" .jsonl | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'); "
                "echo \"$id\t${t%.*}\t$(head -c 600 \"$f\" | grep -o '\"cwd\":\"[^\"]*' | head -1 | cut -d'\"' -f4)\"; done")

    def usage_cmd(self):
        # Rate-limit snapshots are recorded in the newest rollout's token_count events.
        return ("codex login status 2>&1; echo '---'; f=$(find $HOME/.codex/sessions -name '*.jsonl' -printf '%T@ %p\\n' "
                "2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-); [ -n \"$f\" ] && grep -h '\"rate_limits\"' \"$f\" | tail -1; true")

    def interactive_cmd(self, cwd: str, model: str, effort: str, session: str | None, permission: str) -> str:
        sandbox = "--dangerously-bypass-approvals-and-sandbox" if permission == "sandbox" else "-s workspace-write"
        if session and session not in ("new", "per-run"):
            return f"cd {shlex.quote(cwd)} && codex resume {shlex.quote(session)} {sandbox}{self._common(model, effort)}"
        return f"cd {shlex.quote(cwd)} && codex {sandbox}{self._common(model, effort)}"


class Antigravity(CliProfile):
    name, binary = "agy", "agy"
    install = PREREQ + "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    login_hint = "agy  (출력된 URL을 브라우저에서 열고 코드 붙여넣기)  또는 GEMINI_API_KEY 설정"
    models = ("gemini-4-neon", "gemini-4-argon", "gemini-3.8-flash", "gemini-4-hydrogen")
    cheap_default = "gemini-3.8-flash"

    def _common(self, model, effort):
        if not model:
            return ""
        if re.search(r"-(?:low|medium|high|xhigh)$", model):
            return f" --model {shlex.quote(model)}"
        if model in ("gemini-4-neon", "gemini-4-hydrogen"):
            eff = {"max": "xhigh"}.get(effort, effort)
        else:
            eff = {"max": "high", "xhigh": "high"}.get(effort, effort)
        return f" --model {shlex.quote(model)} --effort {eff}"

    def structured_cmd(self, prompt_file, schema_file, model, effort, files=()):
        return (f"agy --print=\"$(cat {sh_quote(prompt_file)})\" --output-format json"
                f" --disable-slash-commands --print-timeout 240s"
                f"{self._common(model, effort)} --json-schema {sh_quote(schema_file)}")

    def prompt_suffix(self, files) -> str:
        # Gemini-family CLIs inline @path references as multimodal parts (image, video, audio, PDF)
        originals = [f.path for f in files if f.native][:MAX_CLI_IMAGES]
        return ("\n\n첨부 파일: " + " ".join(f"@{p}" for p in originals)) if originals else ""

    def agent_cmd(self, prompt_file, cwd, model, effort, session, permission, mcp=True, files=()):
        perm = " --dangerously-skip-permissions" if permission == "sandbox" else ""
        sess = f" --conversation {shlex.quote(session)}" if session else ""
        return (f"cd {sh_quote(cwd)} && agy --print=\"$(cat {sh_quote(prompt_file)})\" --output-format json"
                f"{self._common(model, effort)}{perm}{sess}")

    def parse(self, output):
        # verified shape: {"conversation_id","status","response","error","usage":{input,output,thinking,cache_read}}
        obj = _last_json(output) or {}
        text = obj.get("structured_output") if obj.get("structured_output") is not None else \
            (obj.get("response") or obj.get("result") or "")
        u = obj.get("usage") or {}
        err = obj.get("error") or (obj.get("status") == "ERROR")
        if err and not text:
            text = str(obj.get("error") or "")
        return AgentResult(ok=bool(obj) and not err,
                           text=text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
                           session_id=obj.get("conversation_id") or None,
                           usage={"prompt_tokens": u.get("input_tokens", 0) + u.get("cache_read_tokens", 0),
                                  "cached_tokens": u.get("cache_read_tokens", 0),
                                  "output_tokens": u.get("output_tokens", 0),
                                  "thinking_tokens": u.get("thinking_tokens", 0)},
                           cost_usd=obj.get("total_cost_usd"), raw_tail=output[-2000:])

    def sessions_cmd(self):
        return "timeout 60 agy -p /conversations 2>&1 | tail -40 || true"

    def usage_cmd(self):
        # agy waits for an OAuth code when logged out; fail fast instead of blocking 60 s
        return ("timeout 90 agy -p /usage --print-timeout 60s < /dev/null 2>&1 | tail -40; echo '---'; "
                "timeout 90 agy -p /credits --print-timeout 60s < /dev/null 2>&1 | tail -20")

    def interactive_cmd(self, cwd: str, model: str, effort: str, session: str | None, permission: str) -> str:
        perm = " --dangerously-skip-permissions" if permission == "sandbox" else ""
        sess = f" --conversation {shlex.quote(session)}" if session and session not in ("new", "per-run") else ""
        return f"cd {shlex.quote(cwd)} && agy{self._common(model, effort)}{perm}{sess}"


PROFILES: dict[str, CliProfile] = {p.name: p for p in (Claude(), Codex(), Antigravity())}


class VmAgent:
    """Runs CLI agents inside the workspace VM through a VmShell."""

    def __init__(self, shell: VmShell, settings: AgentSettings, gate=None):
        self.shell, self.s = shell, settings
        self.p = PROFILES[settings.cli]
        # gate(kind) -> "ALLOW" | "FALLBACK" (>= 80 % budget: use the cheap model) ; raises BudgetHalt at 100 %
        self.gate = gate or (lambda kind: "ALLOW")

    def cheap_model(self) -> str:
        return self.s.cheap_model or self.p.cheap_default or self.s.model

    def _files(self, prompt: str, schema: dict | None = None):
        base = f"{self.shell.tmp}/{uuid.uuid4().hex}"
        self.shell.put_file(base + ".prompt", prompt)
        if schema is not None:
            self.shell.put_file(base + ".schema.json", json.dumps(schema, ensure_ascii=False))
        return base

    def installed(self) -> bool:
        return self.shell.run(f"command -v {self.p.binary} >/dev/null", timeout=30).exit_code == 0

    def structured(self, prompt: str, schema: dict, timeout: float = 900, files=()) -> tuple[dict, AgentResult]:
        from .semantic_cache import cache_lookup, cache_store
        ckey = (f"vm{self.shell.canvas_id}", self.p.name, self.cheap_model(), self.s.cheap_effort)
        hit = cache_lookup(ckey, prompt, schema)
        if hit is not None:  # identical question (e.g. pre-flight on unchanged specs): no model call
            return hit, AgentResult(ok=True, text="", model="cache")
        self.gate("structured")
        base = self._files(prompt + self.p.prompt_suffix(files) + "\n\n반드시 JSON 객체 하나만 출력하세요.", schema)
        from .infra import parse_json
        models = [self.cheap_model()]
        if not self.s.cheap_model and self.s.model != models[0]:
            models.append(self.s.model)  # implicit cheap default unavailable on this account -> main model
        try:
            i = 0
            while i < len(models):
                model = models[i]
                cmd = self.p.structured_cmd(base + ".prompt", base + ".schema.json", model, self.s.cheap_effort,
                                            files)
                r = self.shell.run(cmd, timeout=timeout)
                res = self.p.parse(r.output)
                data = parse_json(res.text) if (res.ok and res.text) else {}
                if res.ok and isinstance(data, dict) and (r.exit_code == 0 or data):
                    res.model = model
                    cache_store(ckey, prompt, schema, data)
                    return data, res
                low = (res.text + "\n" + r.output).lower()
                non_model_err = r.exit_code == 127 or any(
                    m in low for m in ("not logged in", "please run /login", "authentication failed", "command not found")
                )
                if (not non_model_err and self.p.name == "agy" and not self.s.cheap_model
                        and "invalid model selection" in low and "" not in models):
                    models.append("")
                if non_model_err or i + 1 == len(models):
                    detail = (res.text or "")[:300] or r.output[-300:]
                    raise CliError(f"{self.p.name} structured call failed (exit {r.exit_code}): {detail}")
                i += 1
        finally:
            self.shell.run(f"rm -f {base}.*", timeout=30)

    def agent(self, task: str, cwd: str, session: str | None, timeout: float = 3600, on_progress=None,
              mcp: bool = True, files=()) -> AgentResult:
        model = self.cheap_model() if self.gate("agent") == "FALLBACK" else self.s.model
        base = self._files(task + self.p.prompt_suffix(files))
        cmd = self.p.agent_cmd(base + ".prompt", cwd, model, self.s.effort, session, self.s.permission, mcp, files)
        r = self.shell.run(cmd, timeout=timeout, on_progress=on_progress)
        self.shell.run(f"rm -f {base}.*", timeout=30)
        res = self.p.parse(r.output)
        res.model = model
        if r.timed_out:
            res.ok, res.text = False, f"timeout after {timeout}s"
        elif r.exit_code != 0 and not res.text:
            res.ok, res.text = False, r.output[-3000:]
        return res

    def cheap_llm(self, on_result=None):
        """Adapter matching the ``llm(prompt, schema) -> dict`` interface used by preflight/router/hitl/…"""
        def call(prompt, schema):
            data, res = self.structured(prompt, schema)
            if on_result:
                on_result(res)
            return data
        return call


def parse_sessions(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and parts[1].isdigit():
            rows.append({"id": parts[0], "mtime": int(parts[1]), "where": parts[2] if len(parts) > 2 else ""})
    return rows


def parse_codex_rate_limits(output: str) -> dict | None:
    for obj in reversed(list(_json_lines(output))):
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        rl = payload.get("rate_limits") if isinstance(payload, dict) else None
        if rl:
            return rl
    return None
