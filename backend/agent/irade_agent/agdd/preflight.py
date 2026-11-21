"""Pre-Flight Conflict Gate (DOC.md §3.4).

Stage 1: deterministic SQL checks (0 tokens).
Stage 2: SQL-narrowed batching of ACCEPT_NOW rows (5~15 per batch).
Stage 3: cheap-model semantic check per batch (injected ``llm`` callable).
Stage 4: gate — CRITICAL > 0 blocks; otherwise snapshot commit + executable scope.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .spec_engine import TABLES, SpecEngine

SEMANTIC_TYPES = ("REQ_VS_REQ", "REQ_VS_ARC", "ARC_VS_SCH", "IMPLICIT_SCOPE_CONFLICT")

# llm(prompt: str, json_schema: dict) -> parsed JSON dict. Must be called with thinking none/low.
CheapModel = Callable[[str, dict], dict]

SEMANTIC_SCHEMA = {
    "type": "object",
    "properties": {"conflicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "conflict_type": {"enum": list(SEMANTIC_TYPES)},
            "severity": {"enum": ["CRITICAL", "WARNING"]},
            "involved_ids": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
            "resolution_options": {"type": "array", "items": {"type": "string"}},
            "recommended": {"type": "integer"},
        },
        "required": ["conflict_type", "severity", "involved_ids", "explanation", "resolution_options"],
    }}},
    "required": ["conflicts"],
}

SEMANTIC_PROMPT = """아래 명세 행들(모두 ACCEPT_NOW, 일부 DEFER 참조 포함) 사이의 논리적 모순만 찾으세요.
검사 유형: REQ_VS_REQ, REQ_VS_ARC, ARC_VS_SCH, IMPLICIT_SCOPE_CONFLICT(완료 기준이 DEFER 컴포넌트를 전제).
추측 금지. 근거가 명확한 충돌만 2~3줄 explanation과 객관식 resolution_options로 JSON 출력. 없으면 빈 배열.
명세 행(JSON):
"""


class _Ids:
    def __init__(self):
        self.day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"CNF-{self.day}-{self.n:02d}"


def _conflict(ids, stage, sev, typ, involved, why, options, recommended=0):
    return {"conflict_id": ids(), "check_stage": stage, "severity": sev, "conflict_type": typ,
            "involved_ids": involved, "explanation": why,
            "resolution_options": {"options": options, "recommended": recommended}}


# ---------------- Stage 1 ----------------
def sql_checks(eng: SpecEngine, ids=None) -> list[dict]:
    ids = ids or _Ids()
    out = []
    db = eng.db
    for typ in ("ARC", "SCH"):
        table, pk, _, _ = TABLES[typ]
        rows = db.execute(f"""
            SELECT s.req_id, x.{pk} AS dep, x.adoption_status FROM spec_traceability t
            JOIN srs_requirements s ON t.source_type='SRS' AND t.source_id=s.req_id
            JOIN {table} x ON t.target_type='{typ}' AND t.target_id=x.{pk}
            WHERE s.adoption_status='ACCEPT_NOW' AND x.adoption_status IN ('DEFER','REJECT')""").fetchall()
        for r in rows:
            out.append(_conflict(ids, "SQL_DETERMINISTIC", "CRITICAL", "ADOPTION_MISMATCH", [r["req_id"], r["dep"]],
                                 f"ACCEPT_NOW 요구사항 {r['req_id']}가 {r['adoption_status']} 상태의 {r['dep']}에 의존합니다.",
                                 [f"{r['dep']}를 ACCEPT_NOW로 승격", f"{r['req_id']}를 DEFER로 보류"], 1))
    for r in db.execute("""
            SELECT req_id FROM srs_requirements s WHERE adoption_status='ACCEPT_NOW' AND NOT EXISTS (
              SELECT 1 FROM spec_traceability t WHERE
                (t.source_type='SRS' AND t.source_id=s.req_id AND t.target_type IN ('ARC','SCH')) OR
                (t.target_type='SRS' AND t.target_id=s.req_id AND t.source_type IN ('ARC','SCH')))"""):
        out.append(_conflict(ids, "SQL_DETERMINISTIC", "CRITICAL", "ORPHAN_SPEC", [r["req_id"]],
                             f"ACCEPT_NOW 요구사항 {r['req_id']}에 연결된 ARC/SCH가 없습니다.",
                             [f"{r['req_id']}에 구현 ARC/SCH 추적 추가", f"{r['req_id']}를 HOLD로 보류"], 0))
    for r in db.execute("""
            SELECT a.schema_id AS a, b.schema_id AS b, a.table_or_key_name AS name FROM schema_definitions a
            JOIN schema_definitions b ON a.storage_engine=b.storage_engine AND a.table_or_key_name=b.table_or_key_name
              AND a.schema_id < b.schema_id AND a.columns_and_ddl_json <> b.columns_and_ddl_json
            WHERE a.adoption_status='ACCEPT_NOW' AND b.adoption_status='ACCEPT_NOW'"""):
        out.append(_conflict(ids, "SQL_DETERMINISTIC", "CRITICAL", "DUPLICATE_SCHEMA", [r["a"], r["b"]],
                             f"{r['name']}이(가) 서로 다른 DDL로 {r['a']}, {r['b']}에 중복 정의되었습니다.",
                             [f"{r['a']} 유지, {r['b']} REJECT", f"{r['b']} 유지, {r['a']} REJECT"], 0))
    return out


# ---------------- Stage 2 ----------------
_WORD = re.compile(r"[0-9A-Za-z가-힣_]{3,}")


def build_batches(eng: SpecEngine, min_size: int = 5, max_size: int = 15) -> list[list[tuple[str, str]]]:
    """Cluster ACCEPT_NOW rows: same domain + 1-hop trace + top FTS keyword neighbours."""
    reqs = eng.rows("SRS")
    by_domain: dict[str, list[dict]] = {}
    for r in reqs:
        by_domain.setdefault(r["domain"], []).append(r)
    batches: list[list[tuple[str, str]]] = []
    for _, group in sorted(by_domain.items()):
        for i in range(0, len(group), max_size):
            chunk = group[i:i + max_size]
            members: list[tuple[str, str]] = [("SRS", r["req_id"]) for r in chunk]
            for r in chunk:  # 1-hop links (including DEFER targets for IMPLICIT_SCOPE checks)
                members += [(t, s) for t, s, _ in eng.linked("SRS", r["req_id"])]
            if len(set(members)) < min_size:  # FTS keyword neighbours
                words = sorted(set(_WORD.findall(" ".join(r["acceptance_criteria"] + " " + r["title"] for r in chunk))))
                if words:
                    q = " OR ".join(f'"{w}"' for w in words[:12])
                    members += eng.search(q, limit=max_size)
            seen, uniq = set(), []
            for m in members:
                if m not in seen:
                    seen.add(m)
                    uniq.append(m)
            batches.append(uniq[:max_size])
    return batches


# ---------------- Stage 3 ----------------
def semantic_checks(eng: SpecEngine, llm: CheapModel, ids=None, batches=None, workers: int = 4) -> list[dict]:
    """Batches are checked concurrently (the gateway enforces the provider rate limit)."""
    from concurrent.futures import ThreadPoolExecutor

    ids = ids or _Ids()
    out = []
    jobs = []
    for batch in batches if batches is not None else build_batches(eng):
        payload = [dict(eng.get(t, s), spec_type=t) for t, s in batch if eng.get(t, s)]
        if len(payload) >= 2:
            jobs.append((batch, SEMANTIC_PROMPT + json.dumps(payload, ensure_ascii=False)))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1))) as ex:
        results = list(ex.map(lambda j: llm(j[1], SEMANTIC_SCHEMA), jobs))
    for (batch, _), result in zip(jobs, results):
        valid = {s for _, s in batch}
        for c in result.get("conflicts", []):
            involved = [i for i in c.get("involved_ids", []) if i in valid]
            if c.get("conflict_type") not in SEMANTIC_TYPES or not involved:
                continue  # drop hallucinated / out-of-batch findings
            sev = c.get("severity") if c.get("severity") in ("CRITICAL", "WARNING") else "WARNING"
            out.append(_conflict(ids, "LLM_SEMANTIC", sev, c["conflict_type"], involved,
                                 str(c.get("explanation", ""))[:600], list(c.get("resolution_options", [])),
                                 int(c.get("recommended", 0))))
    return out


# ---------------- Stage 4 ----------------
@dataclass
class GateResult:
    passed: bool
    conflicts: list[dict] = field(default_factory=list)
    snapshot: dict | None = None
    scope: dict | None = None

    @property
    def critical(self):
        return [c for c in self.conflicts if c["severity"] == "CRITICAL"]


def run_gate(eng: SpecEngine, llm: CheapModel | None = None,
             commit_snapshot: Callable[[dict, bool], None] | None = None) -> GateResult:
    ids = _Ids()
    conflicts = sql_checks(eng, ids)
    # Skip paid semantic stage when structural CRITICALs already block execution.
    if llm is not None and not conflicts:
        conflicts += semantic_checks(eng, llm, ids)
    eng.record_conflicts(conflicts)
    passed = not any(c["severity"] == "CRITICAL" for c in conflicts)
    snap = eng.snapshot()
    if commit_snapshot:
        commit_snapshot(snap, passed)
    return GateResult(passed, conflicts, snap, eng.executable_scope() if passed else None)
