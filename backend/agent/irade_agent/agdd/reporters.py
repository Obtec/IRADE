"""Standard reporters + non-functional budgets (DOC.md §5.5).

Only JUnit XML, SARIF and JSON (k6 summary, EXPLAIN (FORMAT JSON), bundle stats)
are accepted; no per-tool custom parsers.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class Finding:
    source: str
    severity: str  # FAIL | WARN
    message: str
    location: str = ""


@dataclass
class Budgets:
    p95_ms: float | None = None
    explain_cost: float | None = None
    bundle_bytes: int | None = None
    sarif_block_levels: tuple = ("error",)


def junit(xml_text: str) -> list[Finding]:
    root = ET.fromstring(xml_text)
    out = []
    for tc in root.iter("testcase"):
        for tag in ("failure", "error"):
            el = tc.find(tag)
            if el is not None:
                msg = (el.get("message") or el.text or "").strip().splitlines()
                out.append(Finding("junit", "FAIL", (msg[0] if msg else tag)[:300],
                                   f"{tc.get('classname', '')}.{tc.get('name', '')}"))
    return out


def sarif(text: str, budgets: Budgets = Budgets()) -> list[Finding]:
    data = json.loads(text)
    out = []
    for run in data.get("runs", []):
        for r in run.get("results", []):
            level = r.get("level", "warning")
            loc = r.get("locations", [{}])[0].get("physicalLocation", {})
            where = f"{loc.get('artifactLocation', {}).get('uri', '')}:{loc.get('region', {}).get('startLine', '')}"
            out.append(Finding("sarif", "FAIL" if level in budgets.sarif_block_levels else "WARN",
                               f"{r.get('ruleId', '')}: {r.get('message', {}).get('text', '')}"[:300], where))
    return out


def k6(text: str, budgets: Budgets) -> list[Finding]:
    data = json.loads(text)
    vals = data.get("metrics", {}).get("http_req_duration", {})
    vals = vals.get("values", vals)
    p95 = vals.get("p(95)")
    if budgets.p95_ms is not None and p95 is not None and p95 > budgets.p95_ms:
        return [Finding("k6", "FAIL", f"p95 {p95:.1f}ms > budget {budgets.p95_ms}ms")]
    return []


def _walk_plan(node):
    yield node
    for c in node.get("Plans", []):
        yield from _walk_plan(c)


def explain(text: str, budgets: Budgets, large_table_rows: int = 10_000) -> list[Finding]:
    """PostgreSQL EXPLAIN (FORMAT JSON): cost budget + full table scans on large relations + N+1 hint."""
    data = json.loads(text)
    plan = (data[0] if isinstance(data, list) else data)["Plan"]
    out = []
    if budgets.explain_cost is not None and plan.get("Total Cost", 0) > budgets.explain_cost:
        out.append(Finding("explain", "FAIL", f"total cost {plan['Total Cost']} > budget {budgets.explain_cost}"))
    for n in _walk_plan(plan):
        if n.get("Node Type") == "Seq Scan" and n.get("Plan Rows", 0) >= large_table_rows:
            out.append(Finding("explain", "FAIL", f"full table scan on {n.get('Relation Name')}"))
        if n.get("Node Type") == "Nested Loop" and n.get("Actual Loops", 1) > 100:
            out.append(Finding("explain", "WARN", "nested loop with many iterations (possible N+1)"))
    return out


def bundle(text: str, budgets: Budgets) -> list[Finding]:
    data = json.loads(text)
    size = data.get("total_bytes") or sum(a.get("size", 0) for a in data.get("assets", []))
    if budgets.bundle_bytes is not None and size > budgets.bundle_bytes:
        return [Finding("bundle", "FAIL", f"bundle {size}B > budget {budgets.bundle_bytes}B")]
    return []


@dataclass
class VerifyResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self):
        return not any(f.severity == "FAIL" for f in self.findings)

    def summary(self, max_lines: int = 30) -> str:
        if self.passed:
            return f"VERIFY OK ({len(self.findings)} warnings)"
        fails = [f for f in self.findings if f.severity == "FAIL"]
        return "\n".join([f"VERIFY FAIL ({len(fails)})"] +
                         [f"- [{f.source}] {f.location} {f.message}" for f in fails[:max_lines - 1]])


def collect(reports: dict[str, str], budgets: Budgets = Budgets()) -> VerifyResult:
    """reports: {"junit": xml, "sarif": json, "k6": json, "explain": json, "bundle": json}"""
    parsers = {"junit": lambda t: junit(t), "sarif": lambda t: sarif(t, budgets), "k6": lambda t: k6(t, budgets),
               "explain": lambda t: explain(t, budgets), "bundle": lambda t: bundle(t, budgets)}
    res = VerifyResult()
    for kind, text in reports.items():
        if kind not in parsers:
            raise ValueError(f"non-standard reporter rejected: {kind}")
        res.findings += parsers[kind](text)
    return res
