"""DEV node coding agent: tool-calling loop over the worker-local SQLite (DOC.md §5.2, §6.1, §6.2).

Token rules enforced by the tools themselves:
- no whole-file reads: symbol index + 150-line partial reads, re-reads return only the diff
- every tool result goes through the output parser + 2,500-token hard cap; raw text is offloaded to FTS5
- code changes only as unified diffs; protected test files are rejected
- explicit session_state instead of re-sending the conversation after a node reset
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import gateway, watchdog
from .worker_db import WorkerDB

MAX_STEPS = 24

SYSTEM = """당신은 AGDD Dev 노드 코딩 에이전트입니다. 주어진 ACCEPT_NOW 명세 범위만 구현합니다.
규칙:
- 파일 전체를 읽지 마세요. search_symbols / grep으로 위치를 찾고 read_symbol 또는 read_file(구간)로 필요한 부분만 읽으세요.
- 코드 수정은 apply_patch(git unified diff)로만 합니다. 테스트 파일(tests/**, *.spec.ts 등)은 읽기 전용입니다.
- 파일은 클래스·도메인 책임 단위 150~350줄을 유지하세요.
- 수정 후 run_tests로 확인하고, 끝나면 finish(summary)를 호출하세요. 범위 밖 작업이 필요하면 finish에 이유를 적으세요.
- 이전 도구 출력이 [CLEARED]로 바뀌었다면 search_outputs로 다시 찾으세요."""


def _tool(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


TOOLS = [
    _tool("search_symbols", "함수/클래스/메서드 이름으로 심볼 색인 검색 (부분 일치)", {"query": {"type": "string"}}, ["query"]),
    _tool("read_symbol", "심볼 하나의 코드 범위만 읽기 (예: add, calc.add, Cls.method)",
          {"qualified_name": {"type": "string"}}, ["qualified_name"]),
    _tool("read_file", "파일 일부 읽기(최대 150줄). 이미 읽은 파일은 변경 diff만 반환",
          {"path": {"type": "string"}, "start": {"type": "integer"}}, ["path"]),
    _tool("list_files", "저장소 파일 목록 (glob)", {"pattern": {"type": "string"}}),
    _tool("grep", "저장소에서 정규식 검색 (최대 50줄)", {"pattern": {"type": "string"}, "path": {"type": "string"}},
          ["pattern"]),
    _tool("apply_patch", "git unified diff 적용 (새 파일은 --- /dev/null)", {"patch": {"type": "string"}}, ["patch"]),
    _tool("run_tests", "테스트 실행. 성공 1줄, 실패 시 핵심 에러만", {"args": {"type": "array", "items": {"type": "string"}}}),
    _tool("search_outputs", "이전 도구 출력 원문 전문 검색", {"query": {"type": "string"}}, ["query"]),
    _tool("note", "다음 노드/재시도에도 유지할 사실 기록", {"key": {"type": "string"}, "value": {"type": "string"}},
          ["key", "value"]),
    _tool("finish", "작업 종료", {"summary": {"type": "string"}, "done": {"type": "boolean"}}, ["summary", "done"]),
]


@dataclass
class DevResult:
    ok: bool
    summary: str
    patches: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    steps: int = 0


class DevTools:
    def __init__(self, repo: Path, wdb: WorkerDB, test_cmd: list[str]):
        self.repo, self.wdb, self.test_cmd = repo, wdb, test_cmd
        self.patches: list[str] = []
        self.changed: list[str] = []
        wdb.root = repo
        # A DEV attempt starts with a fresh model context, so earlier read watermarks must not hide code from it.
        with wdb.db:
            wdb.db.execute("DELETE FROM file_read_watermarks")
        wdb.index_tree()  # cheap for changed files only: unchanged ast_hash rows are simply rewritten

    def _sh(self, args, timeout=600):
        p = subprocess.run(args, cwd=self.repo, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    def _safe(self, path: str) -> Path:
        p = (self.repo / path).resolve()
        if not str(p).startswith(str(self.repo.resolve())):
            raise ValueError("path escapes repository")
        return p

    def _resolve(self, name: str) -> list:
        """Accepts 'Cls.method', 'module.func', 'path/mod.py:func' or a bare name."""
        q = name.replace("/", ".").replace(":", ".").removesuffix("()")
        rows = self.wdb.db.execute(
            "SELECT qualified_name, symbol_kind, file_path, start_line, end_line, signature FROM code_symbols "
            "WHERE qualified_name = ? OR symbol_name = ? "
            "OR replace(replace(file_path, '/', '.'), '.py', '') || '.' || qualified_name = ? "
            "OR replace(file_path, '/', '.') || '.' || qualified_name = ? ORDER BY file_path", (q, q, q, q)).fetchall()
        if not rows:
            tail = q.rsplit(".", 1)[-1]
            rows = self.wdb.db.execute(
                "SELECT qualified_name, symbol_kind, file_path, start_line, end_line, signature FROM code_symbols "
                "WHERE qualified_name LIKE ? OR file_path LIKE ? ORDER BY length(qualified_name) LIMIT 30",
                (f"%{tail}%", f"%{q.split('.')[0]}%")).fetchall()
        return rows

    def search_symbols(self, query):
        rows = self._resolve(query)
        return "\n".join(f"{r[0]} ({r[1]}) {r[2]}:{r[3]}-{r[4]}  {r[5]}" for r in rows) or "no symbols"

    def read_symbol(self, qualified_name):
        rows = self._resolve(qualified_name)
        if not rows:
            return f"no symbol {qualified_name!r}; use search_symbols or grep"
        if len(rows) > 1 and rows[0][0] != qualified_name:
            exact = [r for r in rows if r[0] == qualified_name or r[0].endswith("." + qualified_name.split(".")[-1])]
            rows = exact or rows
        r = rows[0]
        lines = (self.repo / r[2]).read_text(encoding="utf-8").splitlines()
        end = min(r[4], r[3] + 149)
        return f"# {r[2]}:{r[3]}-{end}\n" + "\n".join(lines[r[3] - 1:end])

    def read_file(self, path, start=1):
        self._safe(path)
        return self.wdb.read_file(path, max(1, int(start)))

    def list_files(self, pattern="**/*"):
        import fnmatch
        rc, out = self._sh(["git", "ls-files", "--cached", "--others", "--exclude-standard"])
        pats = [pattern, pattern.replace("**/", "")] if pattern else ["*"]
        lines = [l for l in out.splitlines() if any(fnmatch.fnmatch(l, p) for p in pats) or pattern in ("**/*", "*", ".")]
        return "\n".join(lines[:200]) + (f"\n[... {len(lines) - 200} more]" if len(lines) > 200 else "")

    def grep(self, pattern, path=None):
        args = ["git", "grep", "-n", "-I", "--untracked", "-E", pattern]
        if path:
            self._safe(path)
            args += ["--", path]
        rc, out = self._sh(args)
        lines = out.splitlines()
        return "\n".join(l[:240] for l in lines[:50]) + (f"\n[... {len(lines) - 50} more]" if len(lines) > 50 else "") \
            or "no matches"

    def apply_patch(self, patch):
        files = watchdog.touched_files(patch)
        if not files:
            return "rejected: not a unified diff (need '+++ b/<path>' headers)"
        bad = [f for f in files if watchdog.is_protected(f)]
        if bad:
            return f"rejected: read-only test files {bad}"
        if not patch.endswith("\n"):
            patch += "\n"
        p = subprocess.run(["git", "apply", "--recount", "--whitespace=nowarn", "-"], cwd=self.repo, input=patch,
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return gateway.parse_tool_output("git apply", p.returncode, p.stdout + p.stderr)
        self.patches.append(patch)
        for f in files:
            if f not in self.changed:
                self.changed.append(f)
            if f.endswith(".py") and (self.repo / f).exists():
                try:
                    self.wdb.index_file(f)
                except SyntaxError as e:
                    return f"applied, but {f} has a SyntaxError: {e}"
        return f"applied to {files}"

    def run_tests(self, args=None):
        cmd = list(self.test_cmd) + [a for a in (args or []) if not a.startswith("-o")]
        rc, out = self._sh(cmd, timeout=900)
        return gateway.parse_tool_output(" ".join(cmd), rc, out, lambda c, t: self.wdb.offload("tests", c, t))

    def search_outputs(self, query):
        return "\n---\n".join(self.wdb.search_outputs(query, limit=3)) or "no matches"

    def note(self, key, value):
        self.wdb.set_state(key, value)
        return "noted"

    def call(self, name, args: dict) -> str:
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name == "call":
            return f"unknown tool {name}"
        try:
            out = fn(**args)
        except Exception as e:
            out = f"error: {e!r}"[:600]
        return gateway.hard_cap(str(out))


def run_dev_agent(chat: Callable[[list[dict], list[dict]], dict], tools: DevTools, task: dict,
                  rules: str = "", max_steps: int = MAX_STEPS, log: Callable[[str], None] = print) -> DevResult:
    """``chat(messages, tools) -> assistant message dict`` (OpenAI format, via the LLM Gateway)."""
    messages = [
        {"role": "system", "content": SYSTEM + ("\n\n" + rules if rules else "")},
        {"role": "user", "content": "세션 상태(확정 사실):\n" + (tools.wdb.state_prompt() or "(없음)")},
        {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
    ]
    for step in range(1, max_steps + 1):
        msg = chat(messages, TOOLS)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                        else {"role": "assistant", "content": msg.get("content") or ""})
        if not calls:
            messages.append({"role": "user", "content": "도구를 호출해 작업을 진행하거나 finish를 호출하세요."})
            continue
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "finish":
                ok = bool(args.get("done")) and bool(tools.patches)
                return DevResult(ok, str(args.get("summary", ""))[:2000], tools.patches, tools.changed, step)
            out = tools.call(name, args)
            log(f"tool {name} -> {out[:120]!r}")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    return DevResult(False, f"step limit {max_steps} reached", tools.patches, tools.changed, max_steps)
