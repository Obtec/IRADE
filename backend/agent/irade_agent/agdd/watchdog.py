"""Stateless Loop Watchdog + test isolation (DOC.md §5.4).

The watchdog never sees conversation history: input is only ``git diff`` + the
last two error summaries. Deterministic heuristics run first (0 tokens); the
cheap model is consulted only when heuristics are inconclusive.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Callable

CheapModel = Callable[[str, dict], dict]
PROTECTED_TEST_GLOBS = ("tests/**", "test/**", "**/*.spec.ts", "**/*.test.ts", "**/test_*.py", "**/*_test.py",
                        "**/*_test.go", "**/src/test/**")

VERDICT_SCHEMA = {"type": "object", "properties": {
    "verdict": {"enum": ["CONTINUE", "PING_PONG", "CHEAT", "DEADLOCK"]},
    "reason": {"type": "string"},
    "escalate_to": {"enum": ["NONE", "ADVISOR", "HITL", "RESEARCH"]}},
    "required": ["verdict", "reason", "escalate_to"]}
PROMPT = """당신은 이전 대화를 모르는 무상태 감시자입니다. git diff와 직전 에러 요약 2건만 보고 판정하세요.
PING_PONG: 같은 라인을 되돌리며 반복 수정. CHEAT: mock/skip/assert 약화/예외 삼키기로 테스트 우회.
DEADLOCK: 동일 원인 에러가 반복되어 진전 없음. 그 외 CONTINUE.
"""

_MOCK = re.compile(r"^\+.*(\bmock\b|MagicMock|patch\(|jest\.mock|@pytest\.mark\.skip|\.skip\(|xit\(|"
                   r"except\s*(Exception)?\s*:\s*pass|assert\s+True|# type: ignore|@Disabled)", re.I | re.M)


def is_protected(path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path, "*/" + g.lstrip("*/")) for g in PROTECTED_TEST_GLOBS)


def touched_files(diff: str) -> list[str]:
    return [l[6:] for l in diff.splitlines() if l.startswith("+++ b/")]


def changed_lines(diff: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    cur = None
    for l in diff.splitlines():
        if l.startswith("+++ b/"):
            cur = l[6:]
        elif cur and l[:1] in "+-" and not l.startswith(("+++", "---")):
            out.setdefault(cur, set()).add(l[1:].strip())
    return out


@dataclass
class Verdict:
    verdict: str
    reason: str
    escalate_to: str = "NONE"


@dataclass
class Watchdog:
    llm: CheapModel | None = None
    fail_threshold: int = 2
    deadlock_threshold: int = 3
    history: list[dict] = field(default_factory=list)  # [{"ok", "diff", "error"}] — not sent to the model

    def record(self, ok: bool, diff: str, error: str = "") -> Verdict:
        self.history.append({"ok": ok, "diff": diff, "error": error[:2000]})
        return self.evaluate()

    def evaluate(self) -> Verdict:
        last = self.history[-1]
        if any(is_protected(f) for f in touched_files(last["diff"])):
            return Verdict("CHEAT", "protected test file modified (read-only mount violated)", "HITL")
        fails = 0
        for h in reversed(self.history):
            if h["ok"]:
                break
            fails += 1
        sudden_pass = last["ok"] and len(self.history) >= 2 and not self.history[-2]["ok"] and _MOCK.search(last["diff"])
        if sudden_pass:
            return Verdict("CHEAT", "passed right after failure by adding mock/skip/weakened assertion", "HITL")
        if fails < self.fail_threshold:
            return Verdict("CONTINUE", "below threshold")
        # deterministic ping-pong: a line added in one diff is removed in another (or vice versa)
        recent = [changed_lines(h["diff"]) for h in self.history[-3:]]
        if len(recent) >= 2:
            for f, lines in recent[-1].items():
                for prev in recent[:-1]:
                    if len(lines & prev.get(f, set())) >= 1:
                        return Verdict("PING_PONG", f"same lines in {f} edited back and forth", "ADVISOR")
        errs = [h["error"] for h in self.history[-fails:]]
        if fails >= self.deadlock_threshold and len({_sig(e) for e in errs}) == 1:
            return Verdict("DEADLOCK", f"same error signature x{fails}: {_sig(errs[-1])}", "RESEARCH")
        if self.llm:
            errors = [h["error"][-1500:] for h in self.history if not h["ok"]][-2:]
            r = self.llm(PROMPT + json.dumps({"git_diff": last["diff"][-6000:], "errors": errors},
                                             ensure_ascii=False), VERDICT_SCHEMA)
            if r.get("verdict") in ("PING_PONG", "CHEAT", "DEADLOCK"):
                return Verdict(r["verdict"], str(r.get("reason", ""))[:400], r.get("escalate_to", "ADVISOR"))
        return Verdict("CONTINUE", f"{fails} consecutive failures, no loop pattern")


_NUM = re.compile(r"0x[0-9a-f]+|\d+|'[^']*'|\"[^\"]*\"", re.I)


def _sig(error: str) -> str:
    lines = [l.strip() for l in error.strip().splitlines() if l.strip()]
    key = next((l for l in reversed(lines) if re.search(r"(error|exception|fail)", l, re.I)), lines[-1] if lines else "")
    return _NUM.sub("#", key)[:200]


def readonly_mounts(repo_root: str, files: list[str]) -> list[str]:
    """docker run -v arguments mounting protected test files read-only inside the Dev sandbox."""
    return [f"{repo_root}/{f}:/workspace/{f}:ro" for f in files if is_protected(f)]
