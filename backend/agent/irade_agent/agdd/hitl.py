"""Reversibility-based HITL compressor + digest (DOC.md §5.7)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

CheapModel = Callable[[str, dict], dict]
DIGEST_TIMES = (time(10, 0), time(16, 0))

_IRREVERSIBLE = [
    (re.compile(r"\bdrop\s+(table|column|index|schema|database)\b|\btruncate\b", re.I), "DESTRUCTIVE_DDL"),
    (re.compile(r"\balter\s+table\b.*\b(alter\s+column|type)\b", re.I | re.S), "TYPE_CHANGE_DDL"),
    (re.compile(r"\bcreate\s+(unique\s+)?index\b(?!.*concurrently)", re.I), "LOCKING_DDL"),
    (re.compile(r"\b(auth|oauth|login|password|token|session|credential)", re.I), "AUTH_CHANGE"),
    (re.compile(r"\b(payment|billing|checkout|invoice|refund)", re.I), "PAYMENT_CHANGE"),
]

CARD_SCHEMA = {"type": "object", "properties": {
    "impact": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    "options": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "pros": {"type": "string"}, "cons": {"type": "string"}}}},
    "recommended": {"type": "integer"}}, "required": ["impact", "options", "recommended"]}
CARD_PROMPT = """다음 승인 요청(들)을 결정 카드로 압축하세요: impact 3줄 이하, 선택지 A/B의 장단점, 권장 기본값 인덱스.
요청(JSON):
"""


def classify(reason_code: str, diff: str = "", breaks_api_contract: bool = False) -> tuple[str, str]:
    if reason_code == "CRITICAL_SPEC_CONFLICT":
        return "IRREVERSIBLE", reason_code
    if breaks_api_contract:
        return "IRREVERSIBLE", "API_CONTRACT_BREAK"
    added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    for rx, code in _IRREVERSIBLE:
        if rx.search(added) or (code in ("AUTH_CHANGE", "PAYMENT_CHANGE") and rx.search(removed)):
            return "IRREVERSIBLE", code
    return "REVERSIBLE", reason_code


def module_of(req: dict) -> str:
    files = req.get("files") or ["_"]
    return files[0].rsplit("/", 1)[0] if "/" in files[0] else "_"


def compress(requests: list[dict], llm: CheapModel) -> list[dict]:
    """Merge requests of the same module and reversibility into one card each."""
    groups: dict[tuple, list[dict]] = {}
    for r in requests:
        groups.setdefault((module_of(r), r["reversibility"]), []).append(r)
    cards = []
    for (module, rev), reqs in sorted(groups.items()):
        raw = llm(CARD_PROMPT + json.dumps(reqs, ensure_ascii=False)[:8000], CARD_SCHEMA)
        opts = list(raw.get("options") or [])[:4] or [{"label": "승인", "pros": "", "cons": ""},
                                                       {"label": "거절", "pros": "", "cons": ""}]
        rec = raw.get("recommended", 0)
        cards.append({"module": module, "reversibility": rev, "request_ids": [r.get("id") for r in reqs],
                      "impact": [str(x) for x in (raw.get("impact") or [])][:3], "options": opts,
                      "recommended": rec if isinstance(rec, int) and 0 <= rec < len(opts) else 0})
    return cards


@dataclass
class Dispatch:
    push_now: list[dict]
    digest: list[dict]
    digest_at: datetime | None


def next_digest(now: datetime) -> datetime:
    for t in DIGEST_TIMES:
        cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if cand > now:
            return cand
    t = DIGEST_TIMES[0]
    return (now + timedelta(days=1)).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def dispatch(cards: list[dict], now: datetime) -> Dispatch:
    """IRREVERSIBLE -> stop the run and push now. REVERSIBLE -> proceed with default, review in digest."""
    push = [c for c in cards if c["reversibility"] == "IRREVERSIBLE"]
    rest = [c for c in cards if c["reversibility"] == "REVERSIBLE"]
    return Dispatch(push, rest, next_digest(now) if rest else None)


def digest_batch_id(at: datetime) -> str:
    return at.strftime("DIGEST-%Y%m%d-%H%M")
