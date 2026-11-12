"""Interactive Lean 4 InfoView parser and Split-Pane + Editor-Cursor-Sync HTML/JS widget."""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import json
from typing import Any
import uuid

NON_SUBSTANTIVE_TACTIC_KINDS: frozenset[str | None] = frozenset(
    {
        None,
        "",
        "null",
        "by",
        "cdot",
        "cdotTk",
        "Lean.Parser.Term.byTactic",
        "Lean.Parser.Tactic.tacticSeq",
        "Lean.Parser.Tactic.tacticSeq1Indented",
        "Lean.Parser.Tactic.tactic_<;>_",
        "Lean.Parser.Tactic.paren",
    }
)


@dataclass
class TacticStepInfo:
    """A substantive tactic step extracted from REPL's ``InfoTree``."""

    step_id: int
    name: str
    pp: str
    line: int
    col: int
    end_line: int
    end_col: int
    goals_before: list[str]
    goals_after: list[str]
    subgoals_opened: list[str] = field(default_factory=list)
    is_compound: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id,
            "name": self.name,
            "pp": self.pp,
            "line": self.line,
            "col": self.col,
            "endLine": self.end_line,
            "endCol": self.end_col,
            "goalsBefore": self.goals_before,
            "goalsAfter": self.goals_after,
            "subgoalsOpened": self.subgoals_opened,
            "isCompound": self.is_compound,
        }


@dataclass
class TermItemInfo:
    """A term elaboration node (expression + expected type) from REPL's ``InfoTree``."""

    line: int
    col: int
    end_line: int
    end_col: int
    pp: str
    expr: str
    expected_type: str | None
    is_binder: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "col": self.col,
            "endLine": self.end_line,
            "endCol": self.end_col,
            "pp": self.pp,
            "expr": self.expr,
            "expectedType": self.expected_type,
            "isBinder": self.is_binder,
        }


@dataclass
class InfoViewPayload:
    """Complete structured state needed to render the Lean 4 InfoView widget."""

    source_code: str
    ok: bool
    env: int | None
    proof_state: int | None
    tactic_steps: list[TacticStepInfo]
    term_items: list[TermItemInfo]
    sorries: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    notes: list[str]
    initial_line: int
    initial_col: int = 0

    @property
    def has_proof_activity(self) -> bool:
        return bool(self.tactic_steps or self.sorries)


def _short_tactic_name(raw_name: str | None, pp: str) -> str:
    if raw_name:
        short = raw_name.rsplit(".", 1)[-1]
        if short and not short.startswith("tactic"):
            return short
    first_word = pp.strip().split(None, 1)[0] if pp.strip() else "tactic"
    return first_word[:24]


def _augment_error_message(sev: str, data: str) -> str:
    """Append actionable proof hints for common Lean 4 tactic errors such as nonlinear/unexpanded ``omega`` goals."""
    text = str(data or "")
    if sev == "error" and "omega could not prove the goal" in text and "💡 [Hint / 해결 방법]" not in text:
        hint = (
            "\n\n💡 [Hint / 해결 방법] `omega`는 선형 정수/자연수 산술(Linear Arithmetic) 결정 절차이므로, "
            "비선형 곱셈 항(예: `(k + 1) * k`)이나 전개되지 않은 귀납 가설(`ih`)을 독립된 미지수(`a`, `b`)로 취급합니다.\n"
            "  • `Nat.mul_add`와 `Nat.add_mul`을 사용해 목표와 귀납 가설(`at *`)의 곱셈을 모두 전개한 뒤 `omega`를 호출하세요.\n"
            "  • 예시 (`sumUpTo_double`의 `succ k ih` 케이스):\n"
            "    simp [sumUpTo, Nat.mul_add, Nat.add_mul] at *\n"
            "    omega"
        )
        return text + hint
    return text


def extract_infoview_payload(
    source_code: str,
    *,
    ok: bool,
    env: int | None,
    proof_state: int | None,
    infotree: Any,
    tactics: list[dict[str, Any]] | None = None,
    sorries: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> InfoViewPayload:
    """Extract sorted, deduplicated tactic steps and term types from REPL ``infotree``."""
    raw_tactics: list[TacticStepInfo] = []
    term_map: dict[tuple[int, int, int, int], TermItemInfo] = {}

    def walk(tree_node: Any) -> list[TacticStepInfo]:
        """Walk tree and return substantive TacticStepInfo nodes in subtree."""
        if not isinstance(tree_node, dict):
            return []
        kind = tree_node.get("kind")
        node = tree_node.get("node")
        children = tree_node.get("children") or []

        child_tactics: list[TacticStepInfo] = []
        for ch in children:
            child_tactics.extend(walk(ch))

        if not isinstance(node, dict):
            return child_tactics

        stx = node.get("stx") or {}
        rng = stx.get("range") or {}
        if rng.get("synthetic", False):
            return child_tactics

        start = rng.get("start") or {}
        finish = rng.get("finish") or {}
        line = int(start.get("line", 0))
        col = int(start.get("column", 0))
        end_line = int(finish.get("line", line))
        end_col = int(finish.get("column", col))
        if line <= 0:
            return child_tactics

        if kind == "TacticInfo":
            raw_name = node.get("name")
            stx_kind = stx.get("kind")
            if (
                raw_name in NON_SUBSTANTIVE_TACTIC_KINDS
                or stx_kind in NON_SUBSTANTIVE_TACTIC_KINDS
            ):
                return child_tactics

            pp = str(stx.get("pp") or "").strip()
            if not pp or pp == "<failed to pretty print>":
                src_lines = source_code.splitlines()
                if 1 <= line <= len(src_lines):
                    pp = src_lines[line - 1].strip()

            goals_before = [str(g) for g in (node.get("goalsBefore") or [])]
            goals_after = [str(g) for g in (node.get("goalsAfter") or [])]

            # Check if this tactic spans multiple lines and has child tactics on later lines
            later_children = [
                ct for ct in child_tactics if ct.line > line or ct.col > col
            ]
            is_compound = bool(later_children and end_line > line)
            subgoals_opened: list[str] = []
            if is_compound:
                for ct in later_children:
                    for g in ct.goals_before:
                        if g not in subgoals_opened and g not in goals_before:
                            subgoals_opened.append(g)

            step = TacticStepInfo(
                step_id=0,
                name=_short_tactic_name(raw_name, pp),
                pp=pp,
                line=line,
                col=col,
                end_line=end_line,
                end_col=end_col,
                goals_before=goals_before,
                goals_after=goals_after,
                subgoals_opened=subgoals_opened,
                is_compound=is_compound,
            )
            raw_tactics.append(step)
            return [step]

        if kind == "TermInfo":
            # Keep single-line terms for crisp token-level inspection
            if end_line == line and end_col > col:
                expr = str(node.get("expr") or "").strip()
                expected = node.get("expectedType") or node.get("expectedType?")
                expected_str = str(expected).strip() if expected is not None else None
                pp = str(stx.get("pp") or "").strip()
                if expr:
                    key = (line, col, end_line, end_col)
                    existing = term_map.get(key)
                    if existing is None or (
                        existing.expected_type is None and expected_str is not None
                    ):
                        term_map[key] = TermItemInfo(
                            line=line,
                            col=col,
                            end_line=end_line,
                            end_col=end_col,
                            pp=pp or expr,
                            expr=expr,
                            expected_type=expected_str,
                            is_binder=bool(node.get("isBinder", False)),
                        )
        return child_tactics

    if isinstance(infotree, list):
        for root_tree in infotree:
            walk(root_tree)

    # Fallback: if infotree was not populated, use `tactics` list from CommandResponse
    if not raw_tactics and tactics:
        for idx, t in enumerate(tactics):
            pos = t.get("pos") or {}
            end_pos = t.get("endPos") or pos
            line = int(pos.get("line", 1))
            col = int(pos.get("column", 0))
            end_line = int(end_pos.get("line", line))
            end_col = int(end_pos.get("column", col))
            pp = str(t.get("tactic", "")).strip()
            goals_str = str(t.get("goals", "")).strip()
            raw_tactics.append(
                TacticStepInfo(
                    step_id=idx,
                    name=_short_tactic_name(None, pp),
                    pp=pp,
                    line=line,
                    col=col,
                    end_line=end_line,
                    end_col=end_col,
                    goals_before=[goals_str] if goals_str else [],
                    goals_after=[],
                )
            )

    # Sort tactics in source order and deduplicate identical spans
    raw_tactics.sort(
        key=lambda s: (s.line, s.col, -(s.end_line - s.line), -(s.end_col - s.col))
    )
    dedup_tactics: list[TacticStepInfo] = []
    seen_spans: set[tuple[int, int, int, int, str]] = set()
    for step in raw_tactics:
        sig = (step.line, step.col, step.end_line, step.end_col, step.pp)
        if sig in seen_spans:
            continue
        seen_spans.add(sig)
        step.step_id = len(dedup_tactics)
        dedup_tactics.append(step)

    sorted_terms = sorted(
        term_map.values(),
        key=lambda t: (t.line, t.col, (t.end_col - t.col)),
    )

    sorries_list = list(sorries or [])
    messages_list: list[dict[str, Any]] = []
    for raw_m in messages or []:
        if isinstance(raw_m, dict):
            m_copy = dict(raw_m)
            m_copy["data"] = _augment_error_message(
                str(m_copy.get("severity") or "info"),
                str(m_copy.get("data") or ""),
            )
            messages_list.append(m_copy)
    notes_list = list(notes or [])

    # Determine the most helpful initial line to focus in InfoView:
    # 1. First error line (if any)
    # 2. First open sorry line (if any)
    # 3. Last tactic step line (or first tactic step line)
    initial_line = 1
    initial_col = 0
    err_msg = next((m for m in messages_list if m.get("severity") == "error"), None)
    if err_msg and isinstance(err_msg.get("pos"), dict) and err_msg["pos"].get("line"):
        initial_line = int(err_msg["pos"]["line"])
        initial_col = int(err_msg["pos"].get("column", 0))
    elif sorries_list and isinstance(sorries_list[0].get("pos"), dict):
        initial_line = int(sorries_list[0]["pos"].get("line", 1))
        initial_col = int(sorries_list[0]["pos"].get("column", 0))
    elif dedup_tactics:
        first_step = dedup_tactics[0]
        initial_line = first_step.line
        initial_col = first_step.col

    return InfoViewPayload(
        source_code=source_code,
        ok=ok,
        env=env,
        proof_state=proof_state,
        tactic_steps=dedup_tactics,
        term_items=sorted_terms,
        sorries=sorries_list,
        messages=messages_list,
        notes=notes_list,
        initial_line=initial_line,
        initial_col=initial_col,
    )


_INFOVIEW_CSS = """
<style>
.l4iv-wrap {
  font-family: var(--jp-code-font-family, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace);
  font-size: 12.5px;
  line-height: 1.45;
  color: var(--jp-ui-font-color1, inherit);
  border: 1px solid rgba(127, 127, 127, 0.28);
  border-radius: 8px;
  overflow: hidden;
  margin: 6px 0;
  background: var(--jp-layout-color1, rgba(127, 127, 127, 0.03));
}
.l4iv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 12px;
  background: rgba(59, 130, 246, 0.08);
  border-bottom: 1px solid rgba(127, 127, 127, 0.22);
}
.l4iv-title {
  font-weight: 700;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.l4iv-sync-pill {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(127, 127, 127, 0.15);
  transition: all 0.15s ease;
}
.l4iv-sync-pill.l4iv-synced {
  background: rgba(16, 185, 129, 0.18);
  color: #10b981;
}
.l4iv-controls {
  display: flex;
  align-items: center;
  gap: 6px;
}
.l4iv-btn {
  font-family: inherit;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid rgba(127, 127, 127, 0.3);
  background: var(--jp-layout-color2, rgba(127, 127, 127, 0.1));
  color: inherit;
  cursor: pointer;
  user-select: none;
}
.l4iv-btn:hover {
  background: rgba(59, 130, 246, 0.16);
  border-color: #3b82f6;
}
.l4iv-btn.l4iv-active {
  background: #3b82f6;
  color: #ffffff;
  border-color: #2563eb;
}
.l4iv-grid {
  display: grid;
  grid-template-columns: minmax(260px, 46%) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  align-items: stretch;
  min-height: 160px;
}
@media (max-width: 720px) {
  .l4iv-grid {
    grid-template-columns: 1fr;
  }
}
.l4iv-code-pane {
  border-right: 1px solid rgba(127, 127, 127, 0.22);
  min-height: 0;
  min-width: 0;
  max-height: 440px;
  overflow-y: auto;
  overflow-x: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
  scrollbar-color: rgba(139, 92, 246, 0.45) rgba(127, 127, 127, 0.08);
  padding: 6px 0;
  background: rgba(127, 127, 127, 0.02);
}
.l4iv-line {
  display: flex;
  align-items: baseline;
  padding: 2px 10px 2px 6px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.1s ease;
}
.l4iv-line:hover {
  background: rgba(59, 130, 246, 0.08);
}
.l4iv-line.l4iv-line-selected {
  background: rgba(139, 92, 246, 0.14);
  border-left-color: #8b5cf6;
}
.l4iv-lineno {
  width: 26px;
  flex-shrink: 0;
  text-align: right;
  padding-right: 8px;
  opacity: 0.45;
  font-size: 11px;
  user-select: none;
}
.l4iv-line-text {
  flex: 1;
  white-space: pre-wrap;
  word-break: break-all;
}
.l4iv-line-badge {
  margin-left: 6px;
  font-size: 10.5px;
  font-weight: 600;
  padding: 0 5px;
  border-radius: 3px;
  flex-shrink: 0;
}
.l4iv-b-goals {
  background: rgba(139, 92, 246, 0.22);
  color: #a78bfa;
}
.l4iv-b-done {
  background: rgba(16, 185, 129, 0.22);
  color: #34d399;
}
.l4iv-b-sorry {
  background: rgba(245, 158, 11, 0.24);
  color: #fbbf24;
}
.l4iv-b-err {
  background: rgba(239, 68, 68, 0.28);
  color: #ff7b72;
  border: 1px solid rgba(248, 113, 113, 0.45);
}
.l4iv-info-pane {
  min-height: 0;
  min-width: 0;
  max-height: 440px;
  overflow-y: auto;
  overflow-x: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
  scrollbar-color: rgba(139, 92, 246, 0.45) rgba(127, 127, 127, 0.08);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.l4iv-section {
  flex-shrink: 0;
  min-width: 0;
  border: 1px solid rgba(127, 127, 127, 0.24);
  border-radius: 6px;
  padding: 8px 10px;
  background: rgba(127, 127, 127, 0.05);
}
.l4iv-sec-GoalBefore {
  border-left: 3px solid #818cf8;
}
.l4iv-sec-GoalAfter {
  border-left: 3px solid #a78bfa;
}
.l4iv-sec-Done {
  border-left: 3px solid #34d399;
  background: rgba(16, 185, 129, 0.1);
}
.l4iv-sec-Term {
  border-left: 3px solid #38bdf8;
  background: rgba(14, 165, 233, 0.09);
}
.l4iv-sec-MsgErr {
  border-left: 4px solid #f87171;
  border-color: rgba(248, 113, 113, 0.45);
  background: rgba(239, 68, 68, 0.16);
  color: var(--vsc-err-text, inherit);
}
.l4iv-sec-MsgErr .l4iv-sec-hdr {
  color: #ff7b72;
  opacity: 1;
}
.l4iv-sec-MsgWarn {
  border-left: 4px solid #fbbf24;
  background: rgba(245, 158, 11, 0.14);
}
.l4iv-sec-MsgWarn .l4iv-sec-hdr {
  color: #fbbf24;
  opacity: 1;
}
.l4iv-sec-MsgInfo {
  border-left: 3px solid #60a5fa;
  background: rgba(59, 130, 246, 0.1);
}
.l4iv-sec-hdr {
  font-size: 11px;
  font-weight: 700;
  opacity: 0.92;
  margin-bottom: 5px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.l4iv-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow-y: auto;
  overflow-x: auto;
  scrollbar-width: thin;
  scrollbar-color: rgba(139, 92, 246, 0.45) rgba(127, 127, 127, 0.08);
  font-family: inherit;
  font-size: 12.5px;
}
.l4iv-wrap.l4iv-expanded .l4iv-code-pane,
.l4iv-wrap.l4iv-expanded .l4iv-info-pane,
.l4iv-wrap.l4iv-expanded .l4iv-pre {
  max-height: none;
}
.l4iv-code-pane::-webkit-scrollbar,
.l4iv-info-pane::-webkit-scrollbar,
.l4iv-pre::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
.l4iv-code-pane::-webkit-scrollbar-track,
.l4iv-info-pane::-webkit-scrollbar-track,
.l4iv-pre::-webkit-scrollbar-track {
  background: rgba(127, 127, 127, 0.08);
  border-radius: 4px;
}
.l4iv-code-pane::-webkit-scrollbar-thumb,
.l4iv-info-pane::-webkit-scrollbar-thumb,
.l4iv-pre::-webkit-scrollbar-thumb {
  background: rgba(139, 92, 246, 0.45);
  border-radius: 4px;
}
.l4iv-code-pane::-webkit-scrollbar-thumb:hover,
.l4iv-info-pane::-webkit-scrollbar-thumb:hover,
.l4iv-pre::-webkit-scrollbar-thumb:hover {
  background: rgba(139, 92, 246, 0.7);
}
.l4iv-turnstile {
  color: #a78bfa;
  font-weight: 700;
}
.l4iv-case-tag {
  color: #60a5fa;
  font-weight: 600;
}
.l4iv-goal-sep {
  border: none;
  border-top: 1px dashed rgba(127, 127, 127, 0.28);
  margin: 7px 0;
}
</style>
""".strip()


def _format_goal_html(goal_text: str) -> str:
    """Format a Lean goal string with HTML syntax highlighting for turnstiles and case tags."""
    out_lines: list[str] = []
    for raw_line in html.escape(goal_text or "").split("\n"):
        trimmed = raw_line.lstrip()
        indent = raw_line[: len(raw_line) - len(trimmed)]
        if trimmed.startswith("⊢"):
            out_lines.append(
                f"{indent}<span class='l4iv-turnstile'>⊢</span><strong>{trimmed[1:]}</strong>"
            )
        elif trimmed.startswith("case "):
            out_lines.append(f"{indent}<span class='l4iv-case-tag'>{trimmed}</span>")
        else:
            out_lines.append(raw_line)
    return "\n".join(out_lines)


def _find_best_tactic_index_py(
    tactics: list[TacticStepInfo],
    line: int,
    col: int,
) -> int:
    """Select the active tactic index for ``(line, col)`` matching client-side logic."""
    if not tactics:
        return -1
    on_line = [i for i, t in enumerate(tactics) if t.line == line]
    if len(on_line) == 1:
        return on_line[0]
    if len(on_line) > 1:
        for idx in reversed(on_line):
            if col >= tactics[idx].col:
                return idx
        return on_line[0]

    best_idx = -1
    best_span = 10**9
    for i, t in enumerate(tactics):
        if t.line <= line <= t.end_line:
            span = (t.end_line - t.line) * 1000 + (t.end_col - t.col)
            if span <= best_span:
                best_span = span
                best_idx = i
    if best_idx != -1:
        return best_idx

    for i in range(len(tactics) - 1, -1, -1):
        if tactics[i].line <= line:
            return i
    return 0


def _find_best_term_py(
    terms: list[TermItemInfo],
    line: int,
    col: int,
) -> TermItemInfo | None:
    """Select the active term item for ``(line, col)`` matching client-side logic."""
    best: TermItemInfo | None = None
    best_len = 10**9
    for tm in terms:
        if tm.line == line and tm.col <= col <= tm.end_col:
            length = tm.end_col - tm.col
            if length <= best_len:
                best_len = length
                best = tm
    if best is not None:
        return best
    for tm in terms:
        if tm.line == line:
            return tm
    return None


def _render_initial_info_pane_html(
    payload: InfoViewPayload,
    active_step_idx: int,
) -> str:
    """Server-side render (SSR) the initial right-hand InfoView pane for No-JS environments."""
    active_line = max(1, payload.initial_line)
    active_col = max(0, payload.initial_col)
    parts: list[str] = []

    for note in payload.notes:
        parts.append(f"<div style='font-size:11.5px;opacity:0.8'>ℹ {html.escape(note)}</div>")

    # 1. Active Tactic State (default viewMode = "both")
    if 0 <= active_step_idx < len(payload.tactic_steps):
        step = payload.tactic_steps[active_step_idx]
        loc_badge = f"Ln {step.line}:{step.col}"
        gb = step.goals_before
        gb_body = (
            "<hr class='l4iv-goal-sep'/>".join(_format_goal_html(g) for g in gb)
            if gb
            else "No goals"
        )
        parts.append(
            "<div class='l4iv-section l4iv-sec-GoalBefore'>"
            f"<div class='l4iv-sec-hdr'><span>Goals Before <code>{html.escape(step.name)}</code> ({len(gb)})</span>"
            f"<span>{html.escape(loc_badge)}</span></div>"
            f"<pre class='l4iv-pre'>{gb_body}</pre></div>"
        )

        if step.is_compound and step.subgoals_opened:
            sg = step.subgoals_opened
            sg_body = "<hr class='l4iv-goal-sep'/>".join(_format_goal_html(g) for g in sg)
            parts.append(
                "<div class='l4iv-section l4iv-sec-GoalAfter'>"
                f"<div class='l4iv-sec-hdr'><span>Subgoals Opened by <code>{html.escape(step.name)}</code> ({len(sg)})</span>"
                "<span>compound block</span></div>"
                f"<pre class='l4iv-pre'>{sg_body}</pre></div>"
            )
        else:
            ga = step.goals_after
            if not ga:
                parts.append(
                    "<div class='l4iv-section l4iv-sec-Done'>"
                    f"<div class='l4iv-sec-hdr'><span>Goals After <code>{html.escape(step.pp)}</code></span><span>✓ Resolved</span></div>"
                    "<pre class='l4iv-pre'><strong>✓ Goals accomplished in this branch!</strong></pre></div>"
                )
            else:
                ga_body = "<hr class='l4iv-goal-sep'/>".join(_format_goal_html(g) for g in ga)
                parts.append(
                    "<div class='l4iv-section l4iv-sec-GoalAfter'>"
                    f"<div class='l4iv-sec-hdr'><span>Goals After <code>{html.escape(step.name)}</code> ({len(ga)})</span>"
                    f"<span>{html.escape(loc_badge)}</span></div>"
                    f"<pre class='l4iv-pre'>{ga_body}</pre></div>"
                )

    # 2. Open Sorries
    for s in payload.sorries:
        pos = s.get("pos") or {}
        ln = int(pos.get("line", 0)) if isinstance(pos, dict) else 0
        ps_val = s.get("proofState")
        ps = f"proofState #{ps_val}" if ps_val is not None else "sorry"
        highlight = " style='outline:1.5px solid #f59e0b'" if ln == active_line else ""
        ln_str = f"Ln {ln}" if ln else ""
        parts.append(
            f"<div class='l4iv-section l4iv-sec-MsgWarn'{highlight}>"
            f"<div class='l4iv-sec-hdr'><span>Open Goal ({html.escape(ps)})</span>"
            f"<span>{ln_str}</span></div>"
            f"<pre class='l4iv-pre'>{_format_goal_html(str(s.get('goal') or ''))}</pre></div>"
        )

    # 3. Term Info at Cursor
    term = _find_best_term_py(payload.term_items, active_line, active_col)
    if term is not None:
        ty_str = f" : {term.expected_type}" if term.expected_type else ""
        binder_badge = "binder" if term.is_binder else "term"
        parts.append(
            "<div class='l4iv-section l4iv-sec-Term'>"
            f"<div class='l4iv-sec-hdr'><span>Term at Cursor (Ln {term.line}:{term.col})</span>"
            f"<span>{binder_badge}</span></div>"
            f"<pre class='l4iv-pre'><code>{html.escape(term.expr)}{html.escape(ty_str)}</code></pre></div>"
        )

    # 4. Messages
    for m in payload.messages:
        sev = str(m.get("severity") or "info")
        cls = (
            "l4iv-sec-MsgErr"
            if sev == "error"
            else ("l4iv-sec-MsgWarn" if sev == "warning" else "l4iv-sec-MsgInfo")
        )
        pos = m.get("pos") if isinstance(m.get("pos"), dict) else {}
        ln = int(pos.get("line", 0)) if pos and pos.get("line") else 0
        col = int(pos.get("column", 0)) if pos else 0
        loc = f"Ln {ln}:{col}" if ln else ""
        active_border = " style='outline:1.5px solid currentColor'" if ln == active_line else ""
        parts.append(
            f"<div class='l4iv-section {cls}'{active_border}>"
            f"<div class='l4iv-sec-hdr'><span>{html.escape(sev.upper())}</span><span>{html.escape(loc)}</span></div>"
            f"<pre class='l4iv-pre'>{html.escape(str(m.get('data') or ''))}</pre></div>"
        )

    if not parts:
        parts.append(
            "<div style='opacity:0.65'>Click any line or move your cursor in the cell editor to inspect proof states and types.</div>"
        )

    return "".join(parts)


def render_infoview_html(payload: InfoViewPayload) -> str:
    """Render an interactive Split-Pane Lean 4 InfoView with Jupyter Editor Cursor Sync."""
    uid = f"l4iv_{uuid.uuid4().hex[:10]}"
    code_lines = payload.source_code.splitlines() or [""]
    init_step_idx = _find_best_tactic_index_py(
        payload.tactic_steps,
        payload.initial_line,
        payload.initial_col,
    )

    # Build per-line summary badges from tactic_steps, sorries, and messages
    line_badges: dict[int, tuple[str, str]] = {}
    for step in payload.tactic_steps:
        if step.is_compound and step.subgoals_opened:
            n_sub = len(step.subgoals_opened)
            line_badges[step.line] = (f"{n_sub} subgoal{'s' if n_sub != 1 else ''}", "l4iv-b-goals")
        elif not step.goals_after:
            line_badges[step.line] = ("✓", "l4iv-b-done")
        else:
            n_after = len(step.goals_after)
            line_badges[step.line] = (f"{n_after} goal{'s' if n_after != 1 else ''}", "l4iv-b-goals")

    for s in payload.sorries:
        pos = s.get("pos") or {}
        ln = int(pos.get("line", 0))
        if ln > 0:
            line_badges[ln] = ("sorry", "l4iv-b-sorry")

    for m in payload.messages:
        if m.get("severity") == "error":
            pos = m.get("pos") or {}
            ln = int(pos.get("line", 0))
            if ln > 0:
                line_badges[ln] = ("error", "l4iv-b-err")

    lines_html: list[str] = []
    for idx, text_line in enumerate(code_lines, start=1):
        badge_html = ""
        if idx in line_badges:
            b_txt, b_cls = line_badges[idx]
            badge_html = f"<span class='l4iv-line-badge {b_cls}'>{html.escape(b_txt)}</span>"
        escaped_line = html.escape(text_line) if text_line else " "
        selected_cls = " l4iv-line-selected" if idx == payload.initial_line else ""
        lines_html.append(
            f"<div class='l4iv-line{selected_cls}' data-line='{idx}'>"
            f"<span class='l4iv-lineno'>{idx}</span>"
            f"<span class='l4iv-line-text'>{escaped_line}</span>"
            f"{badge_html}</div>"
        )

    js_data = {
        "env": payload.env,
        "proofState": payload.proof_state,
        "ok": payload.ok,
        "initialLine": payload.initial_line,
        "initialCol": payload.initial_col,
        "tactics": [t.to_dict() for t in payload.tactic_steps],
        "terms": [tm.to_dict() for tm in payload.term_items],
        "sorries": payload.sorries,
        "messages": payload.messages,
        "notes": payload.notes,
    }
    json_blob = json.dumps(js_data, ensure_ascii=False).replace("</", "<\\/")

    env_badge = f"env #{payload.env}" if payload.env is not None else "root"
    n_steps = len(payload.tactic_steps)
    step_nav_html = ""
    if n_steps > 0:
        shown_idx = (init_step_idx + 1) if init_step_idx >= 0 else 1
        step_nav_html = (
            f"<button type='button' class='l4iv-btn' data-action='prev'>◀ Prev</button>"
            f"<span class='l4iv-step-indicator' style='font-size:11px;opacity:0.85'>"
            f"{shown_idx} / {n_steps}</span>"
            f"<button type='button' class='l4iv-btn' data-action='next'>Next ▶</button>"
        )

    initial_info_html = _render_initial_info_pane_html(payload, init_step_idx)

    script = f"""
<script>
(function() {{
  const root = document.getElementById("{uid}");
  if (!root) return;
  const DATA = {json_blob};
  let viewMode = "both"; // "after" | "before" | "both"
  let activeLine = DATA.initialLine || 1;
  let activeCol = DATA.initialCol || 0;
  let activeStepIdx = -1;

  const codePane = root.querySelector(".l4iv-code-pane");
  const codeLines = root.querySelectorAll(".l4iv-line");
  const infoPane = root.querySelector(".l4iv-info-pane");
  const syncPill = root.querySelector(".l4iv-sync-pill");
  const stepIndicator = root.querySelector(".l4iv-step-indicator");
  const modeBtns = root.querySelectorAll("[data-mode]");
  const expandBtn = root.querySelector("[data-action='expand']");

  function esc(str) {{
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }}

  function formatGoalHtml(goalText) {{
    return esc(goalText).split("\\n").map(function(line) {{
      const trimmed = line.trimStart();
      const indent = line.slice(0, line.length - trimmed.length);
      if (trimmed.startsWith("⊢")) {{
        return indent + "<span class='l4iv-turnstile'>⊢</span><strong>" + trimmed.slice(1) + "</strong>";
      }}
      if (trimmed.startsWith("case ")) {{
        return indent + "<span class='l4iv-case-tag'>" + trimmed + "</span>";
      }}
      return line;
    }}).join("\\n");
  }}

  function findBestTacticIndex(line, col) {{
    if (!DATA.tactics.length) return -1;
    // 1. Exact start-line match (prefer matching column, or last tactic on that line)
    const onLine = [];
    for (let i = 0; i < DATA.tactics.length; i++) {{
      const t = DATA.tactics[i];
      if (t.line === line) onLine.push(i);
    }}
    if (onLine.length === 1) return onLine[0];
    if (onLine.length > 1) {{
      for (let j = onLine.length - 1; j >= 0; j--) {{
        const idx = onLine[j];
        if (col >= DATA.tactics[idx].col) return idx;
      }}
      return onLine[0];
    }}
    // 2. Innermost enclosing multi-line tactic
    let bestIdx = -1;
    let bestSpan = 1e9;
    for (let i = 0; i < DATA.tactics.length; i++) {{
      const t = DATA.tactics[i];
      if (line >= t.line && line <= t.endLine) {{
        const span = (t.endLine - t.line) * 1000 + (t.endCol - t.col);
        if (span <= bestSpan) {{
          bestSpan = span;
          bestIdx = i;
        }}
      }}
    }}
    if (bestIdx !== -1) return bestIdx;
    // 3. Nearest preceding tactic
    for (let i = DATA.tactics.length - 1; i >= 0; i--) {{
      if (DATA.tactics[i].line <= line) return i;
    }}
    return 0;
  }}

  function findBestTerm(line, col) {{
    let best = null;
    let bestLen = 1e9;
    for (let i = 0; i < DATA.terms.length; i++) {{
      const tm = DATA.terms[i];
      if (tm.line === line && col >= tm.col && col <= tm.endCol) {{
        const len = tm.endCol - tm.col;
        if (len <= bestLen) {{
          bestLen = len;
          best = tm;
        }}
      }}
    }}
    if (best) return best;
    // Fallback: first term on that line with an expectedType
    for (let i = 0; i < DATA.terms.length; i++) {{
      const tm = DATA.terms[i];
      if (tm.line === line) return tm;
    }}
    return null;
  }}

  function renderInfoPane() {{
    let selectedLineEl = null;
    codeLines.forEach(function(el) {{
      const ln = parseInt(el.getAttribute("data-line"), 10);
      const isSel = ln === activeLine;
      el.classList.toggle("l4iv-line-selected", isSel);
      if (isSel) selectedLineEl = el;
    }});

    if (codePane && selectedLineEl) {{
      const paneTop = codePane.scrollTop;
      const paneBottom = paneTop + codePane.clientHeight;
      const elTop = selectedLineEl.offsetTop - codePane.offsetTop;
      const elBottom = elTop + selectedLineEl.offsetHeight;
      if (elTop < paneTop) {{
        codePane.scrollTop = Math.max(0, elTop - 12);
      }} else if (elBottom > paneBottom) {{
        codePane.scrollTop = elBottom - codePane.clientHeight + 12;
      }}
    }}

    if (stepIndicator && DATA.tactics.length > 0 && activeStepIdx >= 0) {{
      stepIndicator.textContent = (activeStepIdx + 1) + " / " + DATA.tactics.length;
    }}

    const parts = [];

    // Notes
    if (DATA.notes && DATA.notes.length) {{
      DATA.notes.forEach(function(n) {{
        parts.push("<div style='font-size:11.5px;opacity:0.8'>ℹ " + esc(n) + "</div>");
      }});
    }}

    // 1. Active Tactic State
    if (activeStepIdx >= 0 && activeStepIdx < DATA.tactics.length) {{
      const step = DATA.tactics[activeStepIdx];
      const locBadge = "Ln " + step.line + ":" + step.col;

      if (viewMode === "before" || viewMode === "both") {{
        const gb = step.goalsBefore || [];
        const gbBody = gb.length
          ? gb.map(formatGoalHtml).join("<hr class='l4iv-goal-sep'/>")
          : "No goals";
        parts.push(
          "<div class='l4iv-section l4iv-sec-GoalBefore'>" +
          "<div class='l4iv-sec-hdr'><span>Goals Before <code>" + esc(step.name) + "</code> (" + gb.length + ")</span>" +
          "<span>" + esc(locBadge) + "</span></div>" +
          "<pre class='l4iv-pre'>" + gbBody + "</pre></div>"
        );
      }}

      if (viewMode === "after" || viewMode === "both") {{
        if (step.isCompound && step.subgoalsOpened && step.subgoalsOpened.length > 0) {{
          const sg = step.subgoalsOpened;
          parts.push(
            "<div class='l4iv-section l4iv-sec-GoalAfter'>" +
            "<div class='l4iv-sec-hdr'><span>Subgoals Opened by <code>" + esc(step.name) + "</code> (" + sg.length + ")</span>" +
            "<span>compound block</span></div>" +
            "<pre class='l4iv-pre'>" + sg.map(formatGoalHtml).join("<hr class='l4iv-goal-sep'/>") + "</pre></div>"
          );
        }} else {{
          const ga = step.goalsAfter || [];
          if (ga.length === 0) {{
            parts.push(
              "<div class='l4iv-section l4iv-sec-Done'>" +
              "<div class='l4iv-sec-hdr'><span>Goals After <code>" + esc(step.pp) + "</code></span><span>✓ Resolved</span></div>" +
              "<pre class='l4iv-pre'><strong>✓ Goals accomplished in this branch!</strong></pre></div>"
            );
          }} else {{
            parts.push(
              "<div class='l4iv-section l4iv-sec-GoalAfter'>" +
              "<div class='l4iv-sec-hdr'><span>Goals After <code>" + esc(step.name) + "</code> (" + ga.length + ")</span>" +
              "<span>" + esc(locBadge) + "</span></div>" +
              "<pre class='l4iv-pre'>" + ga.map(formatGoalHtml).join("<hr class='l4iv-goal-sep'/>") + "</pre></div>"
            );
          }}
        }}
      }}
    }}

    // 2. Open Sorries (if on active line or no tactic steps)
    DATA.sorries.forEach(function(s) {{
      const ln = s.pos ? s.pos.line : 0;
      const ps = s.proofState !== undefined && s.proofState !== null ? "proofState #" + s.proofState : "sorry";
      const highlight = ln === activeLine ? " style='outline:1.5px solid #f59e0b'" : "";
      parts.push(
        "<div class='l4iv-section l4iv-sec-MsgWarn'" + highlight + ">" +
        "<div class='l4iv-sec-hdr'><span>Open Goal (" + esc(ps) + ")</span>" +
        "<span>" + (ln ? "Ln " + ln : "") + "</span></div>" +
        "<pre class='l4iv-pre'>" + formatGoalHtml(s.goal || "") + "</pre></div>"
      );
    }});

    // 3. Term Info at Cursor (`expr : expectedType`)
    const term = findBestTerm(activeLine, activeCol);
    if (term) {{
      const tyStr = term.expectedType ? " : " + term.expectedType : "";
      const binderBadge = term.isBinder ? "binder" : "term";
      parts.push(
        "<div class='l4iv-section l4iv-sec-Term'>" +
        "<div class='l4iv-sec-hdr'><span>Term at Cursor (Ln " + term.line + ":" + term.col + ")</span>" +
        "<span>" + binderBadge + "</span></div>" +
        "<pre class='l4iv-pre'><code>" + esc(term.expr) + esc(tyStr) + "</code></pre></div>"
      );
    }}

    // 4. Messages (Errors, Warnings, #check/#eval/#print Info)
    DATA.messages.forEach(function(m) {{
      const sev = m.severity || "info";
      const cls = sev === "error" ? "l4iv-sec-MsgErr" : (sev === "warning" ? "l4iv-sec-MsgWarn" : "l4iv-sec-MsgInfo");
      const ln = m.pos && m.pos.line ? m.pos.line : 0;
      const loc = ln ? "Ln " + ln + ":" + (m.pos.column || 0) : "";
      const activeBorder = ln === activeLine ? " style='outline:1.5px solid currentColor'" : "";
      parts.push(
        "<div class='l4iv-section " + cls + "'" + activeBorder + ">" +
        "<div class='l4iv-sec-hdr'><span>" + esc(sev.toUpperCase()) + "</span><span>" + esc(loc) + "</span></div>" +
        "<pre class='l4iv-pre'>" + esc(m.data || "") + "</pre></div>"
      );
    }});

    if (!parts.length) {{
      parts.push("<div style='opacity:0.65'>Click any line or move your cursor in the cell editor to inspect proof states and types.</div>");
    }}

    infoPane.innerHTML = parts.join("");
  }}

  function selectPosition(line, col, source) {{
    activeLine = Math.max(1, line);
    activeCol = Math.max(0, col || 0);
    activeStepIdx = findBestTacticIndex(activeLine, activeCol);
    if (source === "editor" && syncPill) {{
      syncPill.textContent = "⚡ Editor Ln " + activeLine + ":" + activeCol;
      syncPill.classList.add("l4iv-synced");
    }} else if (syncPill) {{
      syncPill.textContent = "Ln " + activeLine + ":" + activeCol;
    }}
    renderInfoPane();
  }}

  // Click on code lines in the left pane
  codeLines.forEach(function(el) {{
    el.addEventListener("click", function(ev) {{
      const ln = parseInt(el.getAttribute("data-line"), 10);
      let col = 0;
      const sel = window.getSelection();
      const textSpan = el.querySelector(".l4iv-line-text");
      if (sel && sel.focusNode && textSpan && textSpan.contains(sel.focusNode)) {{
        col = sel.focusOffset || 0;
      }}
      selectPosition(ln, col, "widget");
    }});
  }});

  // Prev / Next tactic step buttons
  const prevBtn = root.querySelector("[data-action='prev']");
  const nextBtn = root.querySelector("[data-action='next']");
  if (prevBtn) {{
    prevBtn.addEventListener("click", function() {{
      if (!DATA.tactics.length) return;
      activeStepIdx = Math.max(0, (activeStepIdx <= 0 ? 0 : activeStepIdx - 1));
      const st = DATA.tactics[activeStepIdx];
      activeLine = st.line;
      activeCol = st.col;
      renderInfoPane();
    }});
  }}
  if (nextBtn) {{
    nextBtn.addEventListener("click", function() {{
      if (!DATA.tactics.length) return;
      activeStepIdx = Math.min(DATA.tactics.length - 1, activeStepIdx + 1);
      const st = DATA.tactics[activeStepIdx];
      activeLine = st.line;
      activeCol = st.col;
      renderInfoPane();
    }});
  }}

  // Mode toggle buttons (Both / After / Before)
  modeBtns.forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      viewMode = btn.getAttribute("data-mode") || "both";
      modeBtns.forEach(function(b) {{
        b.classList.toggle("l4iv-active", b === btn);
      }});
      renderInfoPane();
    }});
  }});

  if (expandBtn) {{
    expandBtn.addEventListener("click", function() {{
      const expanded = root.classList.toggle("l4iv-expanded");
      expandBtn.classList.toggle("l4iv-active", expanded);
      expandBtn.textContent = expanded ? "↕ Compact" : "↕ Expand";
    }});
  }}

  // Hybrid Editor Cursor Sync: bind to enclosing Jupyter Code Cell (CodeMirror 6 & 5) or Proof Notebook Web Cell
  function bindParentEditorCursor() {{
    const cellEl = root.closest(".jp-Cell, .jp-CodeCell, .cell, .pn-cell");
    if (!cellEl) return;
    const editorArea = cellEl.querySelector(".jp-InputArea-editor, .cm-editor, .CodeMirror, textarea.pn-code-editor, textarea");
    if (!editorArea) return;

    function handleCursorEvent() {{
      // 0. Native textarea (Proof Notebook Web Studio)
      const taEl = editorArea.tagName === "TEXTAREA" ? editorArea : cellEl.querySelector("textarea.pn-code-editor, textarea");
      if (taEl && typeof taEl.selectionStart === "number") {{
        const upto = (taEl.value || "").slice(0, taEl.selectionStart);
        const lines = upto.split("\n");
        const lineOneBased = lines.length;
        const colZeroBased = lines[lines.length - 1].length;
        selectPosition(lineOneBased, colZeroBased, "editor");
        return;
      }}
      // 1. CodeMirror 5 (Classic Notebook)
      const cm5El = cellEl.querySelector(".CodeMirror");
      if (cm5El && cm5El.CodeMirror) {{
        const pos = cm5El.CodeMirror.getCursor();
        selectPosition(pos.line + 1, pos.ch, "editor");
        return;
      }}
      // 2. CodeMirror 6 (JupyterLab 4 / Notebook 7) via DOM Selection inside .cm-content
      const cmContent = cellEl.querySelector(".cm-content");
      if (cmContent) {{
        const sel = window.getSelection();
        if (!sel || !sel.focusNode || !cmContent.contains(sel.focusNode)) return;
        const focusEl = sel.focusNode.nodeType === 3 ? sel.focusNode.parentElement : sel.focusNode;
        if (!focusEl) return;
        const cmLine = focusEl.closest(".cm-line");
        if (!cmLine) return;
        const allLines = cmContent.querySelectorAll(".cm-line");
        const lineIdx = Array.prototype.indexOf.call(allLines, cmLine);
        if (lineIdx === -1) return;
        let col = 0;
        try {{
          const r = document.createRange();
          r.selectNodeContents(cmLine);
          r.setEnd(sel.focusNode, sel.focusOffset);
          col = r.toString().length;
        }} catch (_) {{}}
        selectPosition(lineIdx + 1, col, "editor");
      }}
    }}

    editorArea.addEventListener("mouseup", handleCursorEvent, {{ passive: true }});
    editorArea.addEventListener("keyup", handleCursorEvent, {{ passive: true }});
    editorArea.addEventListener("click", handleCursorEvent, {{ passive: true }});
    editorArea.addEventListener("input", handleCursorEvent, {{ passive: true }});
    editorArea.addEventListener("select", handleCursorEvent, {{ passive: true }});
    if (syncPill) {{
      syncPill.textContent = "⚡ Editor Linked (Ln " + activeLine + ":" + activeCol + ")";
      syncPill.classList.add("l4iv-synced");
    }}
  }}

  // Initial render
  activeStepIdx = findBestTacticIndex(activeLine, activeCol);
  renderInfoPane();
  bindParentEditorCursor();
}})();
</script>
""".strip()

    return (
        f"{_INFOVIEW_CSS}"
        f"<div id='{uid}' class='l4iv-wrap'>"
        f"  <div class='l4iv-header'>"
        f"    <div class='l4iv-title'>"
        f"      <span>∀ Lean 4 InfoView ({html.escape(env_badge)})</span>"
        f"      <span class='l4iv-sync-pill'>Ln {payload.initial_line}:{payload.initial_col}</span>"
        f"    </div>"
        f"    <div class='l4iv-controls'>"
        f"      {step_nav_html}"
        f"      <button type='button' class='l4iv-btn l4iv-active' data-mode='both'>Before → After</button>"
        f"      <button type='button' class='l4iv-btn' data-mode='after'>After</button>"
        f"      <button type='button' class='l4iv-btn' data-mode='before'>Before</button>"
        f"      <button type='button' class='l4iv-btn' data-action='expand' title='Toggle scrollable height vs full height'>↕ Expand</button>"
        f"    </div>"
        f"  </div>"
        f"  <div class='l4iv-grid'>"
        f"    <div class='l4iv-code-pane'>{''.join(lines_html)}</div>"
        f"    <div class='l4iv-info-pane'>{initial_info_html}</div>"
        f"  </div>"
        f"</div>"
        f"{script}"
    )
