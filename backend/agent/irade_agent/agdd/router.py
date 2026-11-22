"""Entry router (Fast-Track) and AMBIGUITY_GATE (DOC.md §5.1)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

FAST_TRACK_MAX_LINES = 30
FAST_TRACK_MAX_FILES = 2
_SENSITIVE = re.compile(
    r"(\b(create|alter|drop|truncate)\s+(table|index|schema)\b|migration|ddl|"
    r"\bauth|login|oauth|token|password|credential|인증|권한|"
    r"payment|billing|checkout|결제)", re.I)

CheapModel = Callable[[str, dict], dict]

ESTIMATE_SCHEMA = {"type": "object", "properties": {
    "est_lines": {"type": "integer"}, "files": {"type": "array", "items": {"type": "string"}},
    "touches_ddl": {"type": "boolean"}, "touches_auth": {"type": "boolean"}, "touches_payment": {"type": "boolean"}},
    "required": ["est_lines", "files", "touches_ddl", "touches_auth", "touches_payment"]}


@dataclass
class RouteDecision:
    route_mode: str  # FAST_TRACK | FULL_AGDD
    reasons: list[str]


def decide_route(est_lines: int, files: list[str], touches_ddl=False, touches_auth=False,
                 touches_payment=False, text: str = "") -> RouteDecision:
    """All conditions must hold for FAST_TRACK; any miss -> FULL_AGDD. Keyword scan is a safety net."""
    reasons = []
    if est_lines > FAST_TRACK_MAX_LINES:
        reasons.append(f"est_lines {est_lines} > {FAST_TRACK_MAX_LINES}")
    if len(set(files)) > FAST_TRACK_MAX_FILES:
        reasons.append(f"files {len(set(files))} > {FAST_TRACK_MAX_FILES}")
    if touches_ddl or touches_auth or touches_payment:
        reasons.append("touches DDL/auth/payment")
    hay = text + " " + " ".join(files)
    if _SENSITIVE.search(hay):
        reasons.append("sensitive keyword detected")
    return RouteDecision("FULL_AGDD" if reasons else "FAST_TRACK", reasons)


ESTIMATE_PROMPT = """변경 요청을 분석해 예상 변경 규모를 추정하세요. 모든 필드를 반드시 채우세요.
예시 출력: {"est_lines": 12, "files": ["src/calc.py"], "touches_ddl": false, "touches_auth": false, "touches_payment": false}
변경 요청:
"""


def route_request(request: str, llm: CheapModel, attempts: int = 2) -> RouteDecision:
    """Cheap-model size estimate; an incomplete estimate is retried once, then falls back to FULL_AGDD."""
    for _ in range(attempts):
        est = llm(ESTIMATE_PROMPT + request, ESTIMATE_SCHEMA)
        if isinstance(est.get("est_lines"), int) and isinstance(est.get("files"), list) and est["files"]:
            return decide_route(est["est_lines"], [str(f) for f in est["files"]], bool(est.get("touches_ddl")),
                                bool(est.get("touches_auth")), bool(est.get("touches_payment")), request)
    return RouteDecision("FULL_AGDD", ["size estimate unavailable"])


# ---------------- AMBIGUITY_GATE ----------------
CHECKLIST = ("PERMISSION", "FAILURE_FALLBACK", "EMPTY_BOUNDARY", "CONCURRENCY")
AMBIGUITY_SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": {
    "type": "object", "properties": {
        "spec_id": {"type": "string"}, "category": {"enum": list(CHECKLIST)},
        "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
        "recommended": {"type": "integer"}},
    "required": ["spec_id", "category", "question", "options", "recommended"]}}}, "required": ["questions"]}
AMBIGUITY_PROMPT = """적대적 모호성 린터입니다. 아래 요구사항에서 다음 4개 체크리스트 중 명시되지 않은 항목을 찾으세요:
PERMISSION(권한 조건), FAILURE_FALLBACK(실패 시 폴백), EMPTY_BOUNDARY(빈 값·경계값), CONCURRENCY(동시성 처리).
임의로 추측해 채우지 말고, 미결정 항목마다 권장 기본값 인덱스를 포함한 객관식 질문으로만 출력하세요.
요구사항(JSON):
"""


def ambiguity_gate(requirements: list[dict], llm: CheapModel) -> list[dict]:
    if not requirements:
        return []
    result = llm(AMBIGUITY_PROMPT + json.dumps(requirements, ensure_ascii=False), AMBIGUITY_SCHEMA)
    if not isinstance(result, dict):
        return []
    valid = {r.get("req_id") for r in requirements}
    out = []
    for q in (result.get("questions") or []):
        if not isinstance(q, dict):
            continue
        opts = [str(o) for o in (q.get("options") or [])]
        if q.get("category") not in CHECKLIST or len(opts) < 2 or q.get("spec_id") not in valid:
            continue
        rec = q.get("recommended", 0)
        q["options"] = opts
        q["recommended"] = rec if isinstance(rec, int) and not isinstance(rec, bool) and 0 <= rec < len(opts) else 0
        out.append(q)
    return out
