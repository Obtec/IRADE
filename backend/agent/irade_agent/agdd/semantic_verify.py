"""SEMANTIC_VERIFY: per-symbol incremental semantic lifting (DOC.md §5.3, §6.1 #4)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .worker_db import WorkerDB

FIELDS = ("preconditions", "state_transitions", "side_effects", "failure_contracts", "invariants")
CheapModel = Callable[[str, dict], dict]

LIFT_SCHEMA = {"type": "object",
               "properties": {f: {"type": "array", "items": {"type": "string"}} for f in FIELDS},
               "required": list(FIELDS)}
LIFT_PROMPT = """아래 코드 조각을 선언형 명세 JSON으로 변환하세요(추측 금지, 코드에 있는 것만).
필드: preconditions, state_transitions, side_effects, failure_contracts, invariants (각각 문자열 배열).
"""


@dataclass
class Violation:
    qualified_name: str
    location: str
    missing: list[str] = field(default_factory=list)      # contract items not present in code
    unexpected: list[str] = field(default_factory=list)   # side effects not declared in contract


@dataclass
class VerifyReport:
    lifted: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def ok(self):
        return not self.violations


def _norm(items) -> set[str]:
    return {" ".join(str(i).lower().split()) for i in items or []}


def compare(declarative: dict, contract: dict) -> tuple[list[str], list[str]]:
    missing = []
    for f in ("preconditions", "failure_contracts", "invariants", "state_transitions"):
        missing += [f"{f}: {x}" for x in sorted(_norm(contract.get(f)) - _norm(declarative.get(f)))]
    unexpected = [f"side_effects: {x}" for x in sorted(_norm(declarative.get("side_effects"))
                                                       - _norm(contract.get("side_effects")))]
    return missing, unexpected


def verify_changed(worker: WorkerDB, changed_files: list[str], contracts: dict[str, dict], llm: CheapModel,
                   on_verified: Callable[[str, str], None] | None = None) -> VerifyReport:
    """``contracts``: {symbol_fqn: declarative_spec_json} loaded from PG ``semantic_contracts``.
    ``on_verified(fqn, ast_hash)`` updates ``last_verified_ast_hash`` on match."""
    report = VerifyReport()
    for path in changed_files:
        worker.index_file(path)
        for row in worker.db.execute("SELECT * FROM code_symbols WHERE file_path=? AND symbol_kind!='class'", (path,)).fetchall():
            qn, h = row["qualified_name"], row["ast_hash"]
            cached = worker.cached_projection(qn, h)
            if cached is not None:  # unchanged ast_hash -> 0 LLM calls
                report.cached.append(qn)
                decl = cached["declarative"]
            else:
                snippet = worker.read_symbol(qn)
                decl = llm(LIFT_PROMPT + snippet, LIFT_SCHEMA)
                decl = {f: list(decl.get(f, [])) for f in FIELDS}
                report.lifted.append(qn)
                report.llm_calls += 1
            contract = contracts.get(qn)
            if contract is None:
                status = "UNCONTRACTED"
            else:
                missing, unexpected = compare(decl, contract)
                status = "DRIFT" if missing or unexpected else "MATCHED"
                if status == "DRIFT":
                    report.violations.append(Violation(qn, f"{path}:{row['start_line']}-{row['end_line']}",
                                                       missing, unexpected))
                elif on_verified:
                    on_verified(qn, h)
            worker.store_projection(qn, h, decl, status)
    return report


def dev_feedback(report: VerifyReport) -> str:
    """Only the symbol location + violated items go back to the Dev node."""
    return json.dumps([v.__dict__ for v in report.violations], ensure_ascii=False)
