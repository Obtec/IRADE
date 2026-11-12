"""Interactive Split-Pane InfoView widget generator for Rocq (Coq)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import html
import json
from typing import Any
import uuid

from .session import RocqExecutionOutcome
from .xml_protocol import RocqGoalsBundle


@dataclass
class RocqStepInfo:
    """Serialized proof step information for a single Rocq sentence."""

    sentence: str
    line: int
    col: int
    end_line: int
    end_col: int
    state_id_before: int
    state_id_after: int | None
    goals_before: list[str] = field(default_factory=list)
    goals_after: list[str] = field(default_factory=list)
    background_goals: list[str] = field(default_factory=list)
    shelved_goals: list[str] = field(default_factory=list)
    admitted_goals: list[str] = field(default_factory=list)
    badge: str = ""
    ok: bool = True
    error_message: str | None = None


@dataclass
class RocqInfoViewPayload:
    """Complete payload for rendering the Rocq Split-Pane InfoView."""

    source: str
    state_id: int
    ok: bool = True
    steps: list[RocqStepInfo] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _bundle_to_goal_strings(bundle: RocqGoalsBundle | None) -> tuple[list[str], list[str], list[str], list[str]]:
    if bundle is None:
        return [], [], [], []
    focused = [g.format_block() for g in bundle.focused]
    bg = [g.format_block() for g in bundle.background]
    shelved = [g.format_block() for g in bundle.shelved]
    admitted = [g.format_block() for g in bundle.admitted]
    return focused, bg, shelved, admitted


def extract_rocq_infoview_payload(outcome: RocqExecutionOutcome) -> RocqInfoViewPayload:
    """Convert a ``RocqExecutionOutcome`` into a ``RocqInfoViewPayload``."""
    steps: list[RocqStepInfo] = []
    messages: list[dict[str, Any]] = []

    for st in outcome.steps:
        fb_bef, _, _, _ = _bundle_to_goal_strings(st.goals_before)
        fb_aft, bg_aft, sh_aft, adm_aft = _bundle_to_goal_strings(st.goals_after)
        steps.append(
            RocqStepInfo(
                sentence=st.span.sentence,
                line=st.span.start_line,
                col=st.span.start_col,
                end_line=st.span.end_line,
                end_col=st.span.end_col,
                state_id_before=st.state_id_before,
                state_id_after=st.state_id_after,
                goals_before=fb_bef,
                goals_after=fb_aft,
                background_goals=bg_aft,
                shelved_goals=sh_aft,
                admitted_goals=adm_aft,
                badge=st.badge,
                ok=st.ok,
                error_message=st.error_message,
            )
        )
        for fb in st.feedbacks:
            sev = "info" if fb.severity == "notice" else fb.severity
            messages.append(
                {
                    "severity": sev,
                    "line": st.span.start_line,
                    "col": st.span.start_col,
                    "data": fb.message,
                }
            )
        if not st.ok and st.error_message:
            already = any(
                m["severity"] == "error" and m["data"] == st.error_message
                for m in messages
            )
            if not already:
                messages.append(
                    {
                        "severity": "error",
                        "line": st.span.start_line,
                        "col": st.span.start_col,
                        "data": st.error_message,
                    }
                )

    if not outcome.steps and outcome.error_message:
        messages.append(
            {
                "severity": "error",
                "line": 1,
                "col": 0,
                "data": outcome.error_message,
            }
        )

    return RocqInfoViewPayload(
        source=outcome.source_code,
        state_id=outcome.state_id,
        ok=outcome.ok,
        steps=steps,
        messages=messages,
        notes=list(outcome.notes),
    )


def _format_goal_html(goal_text: str) -> str:
    """Format a Rocq goal string with turnstile ``⊢`` highlighting."""
    out_lines: list[str] = []
    for raw_line in html.escape(goal_text or "").split("\n"):
        trimmed = raw_line.lstrip()
        indent = raw_line[: len(raw_line) - len(trimmed)]
        if trimmed.startswith("⊢"):
            out_lines.append(
                f"{indent}<span class='l4iv-turnstile'>⊢</span><strong>{trimmed[1:]}</strong>"
            )
        else:
            out_lines.append(raw_line)
    return "\n".join(out_lines)


def _render_ssr_info_pane(payload: RocqInfoViewPayload) -> str:
    """Pre-render the initial right-hand InfoView pane for SSR and no-JS environments."""
    sections: list[str] = []

    active_step: RocqStepInfo | None = None
    if payload.steps:
        # Pick the last step with open goals, or the error step, or the final step
        for st in reversed(payload.steps):
            if not st.ok or st.goals_after or st.shelved_goals or st.admitted_goals:
                active_step = st
                break
        if active_step is None:
            active_step = payload.steps[-1]

    if active_step is not None:
        cmd_esc = html.escape(active_step.sentence[:48])
        loc_str = f"Ln {active_step.line}:{active_step.col}"

        if active_step.goals_before:
            bef_html = "<hr class='l4iv-goal-sep'/>".join(
                _format_goal_html(g) for g in active_step.goals_before
            )
            sections.append(
                "<div class='l4iv-section l4iv-sec-GoalBefore'>"
                f"<div class='l4iv-sec-hdr'><span>Goals Before <code>{cmd_esc}</code> ({len(active_step.goals_before)})</span>"
                f"<span>state #{active_step.state_id_before}</span></div>"
                f"<pre class='l4iv-pre'>{bef_html}</pre></div>"
            )

        if active_step.goals_after:
            aft_html = "<hr class='l4iv-goal-sep'/>".join(
                _format_goal_html(g) for g in active_step.goals_after
            )
            sections.append(
                "<div class='l4iv-section l4iv-sec-GoalAfter'>"
                f"<div class='l4iv-sec-hdr'><span>Focused Goals ({len(active_step.goals_after)})</span>"
                f"<span>{loc_str}</span></div>"
                f"<pre class='l4iv-pre'>{aft_html}</pre></div>"
            )
        elif active_step.ok and (active_step.goals_before or active_step.badge == "✓"):
            bg_note = (
                f" ({len(active_step.background_goals)} unfocused subgoals remaining)"
                if active_step.background_goals
                else ""
            )
            sections.append(
                "<div class='l4iv-section l4iv-sec-Done'>"
                f"<div class='l4iv-sec-hdr'><span>Goals After <code>{cmd_esc}</code></span>"
                "<span>✓ Resolved</span></div>"
                f"<pre class='l4iv-pre'><strong>✓ Goals accomplished!{html.escape(bg_note)}</strong></pre></div>"
            )

        if active_step.background_goals:
            bg_html = "<hr class='l4iv-goal-sep'/>".join(
                _format_goal_html(g) for g in active_step.background_goals
            )
            sections.append(
                "<div class='l4iv-section l4iv-sec-GoalBefore'>"
                f"<div class='l4iv-sec-hdr'><span>Unfocused / Background Goals ({len(active_step.background_goals)})</span>"
                "<span>background</span></div>"
                f"<pre class='l4iv-pre'>{bg_html}</pre></div>"
            )

        if active_step.shelved_goals:
            sh_html = "<hr class='l4iv-goal-sep'/>".join(
                _format_goal_html(g) for g in active_step.shelved_goals
            )
            sections.append(
                "<div class='l4iv-section l4iv-sec-MsgWarn'>"
                f"<div class='l4iv-sec-hdr'><span>Shelved Goals ({len(active_step.shelved_goals)})</span>"
                "<span>shelved</span></div>"
                f"<pre class='l4iv-pre'>{sh_html}</pre></div>"
            )

        if active_step.admitted_goals:
            adm_html = "<hr class='l4iv-goal-sep'/>".join(
                _format_goal_html(g) for g in active_step.admitted_goals
            )
            sections.append(
                "<div class='l4iv-section l4iv-sec-MsgWarn'>"
                f"<div class='l4iv-sec-hdr'><span>Admitted / Given-Up Goals ({len(active_step.admitted_goals)})</span>"
                "<span>admitted</span></div>"
                f"<pre class='l4iv-pre'>{adm_html}</pre></div>"
            )

    for msg in payload.messages:
        sev = str(msg.get("severity", "info")).lower()
        cls = (
            "l4iv-sec-MsgErr"
            if sev == "error"
            else ("l4iv-sec-MsgWarn" if sev == "warning" else "l4iv-sec-MsgInfo")
        )
        line = msg.get("line", 1)
        col = msg.get("col", 0)
        data = html.escape(str(msg.get("data", "")))
        sections.append(
            f"<div class='l4iv-section {cls}'>"
            f"<div class='l4iv-sec-hdr'><span>{html.escape(sev.upper())}</span><span>Ln {line}:{col}</span></div>"
            f"<pre class='l4iv-pre'>{data}</pre></div>"
        )

    for note in payload.notes:
        sections.append(
            "<div class='l4iv-section l4iv-sec-MsgInfo'>"
            "<div class='l4iv-sec-hdr'><span>STM NOTE</span><span>session</span></div>"
            f"<pre class='l4iv-pre'>{html.escape(note)}</pre></div>"
        )

    if not sections:
        sections.append(
            "<div class='l4iv-section l4iv-sec-Done'>"
            f"<div class='l4iv-sec-hdr'><span>Rocq STM State #{payload.state_id}</span><span>✓ Ready</span></div>"
            "<pre class='l4iv-pre'>No active proof goals.</pre></div>"
        )

    return "".join(sections)


_ROCQ_INFOVIEW_STYLE = """
<style>
.l4iv-wrap {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.5;
  border: 1px solid rgba(127, 127, 127, 0.3);
  border-radius: 8px;
  overflow: hidden;
  margin: 6px 0;
  background: rgba(127, 127, 127, 0.04);
}
.l4iv-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 12px;
  background: rgba(139, 92, 246, 0.1);
  border-bottom: 1px solid rgba(127, 127, 127, 0.25);
}
.l4iv-title {
  font-weight: 700;
  color: #a78bfa;
  display: flex;
  align-items: center;
  gap: 8px;
}
.l4iv-controls {
  display: flex;
  align-items: center;
  gap: 4px;
}
.l4iv-btn {
  background: rgba(127, 127, 127, 0.14);
  color: inherit;
  border: 1px solid rgba(127, 127, 127, 0.28);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 11px;
  cursor: pointer;
}
.l4iv-btn.active {
  background: rgba(139, 92, 246, 0.28);
  border-color: #a78bfa;
  font-weight: 600;
}
.l4iv-split {
  display: grid;
  grid-template-columns: minmax(260px, 46%) 1fr;
  align-items: stretch;
}
@media (max-width: 720px) {
  .l4iv-split { grid-template-columns: 1fr; }
}
.l4iv-code-pane {
  border-right: 1px solid rgba(127, 127, 127, 0.22);
  padding: 6px 0;
  max-height: 440px;
  overflow-y: auto;
  overflow-x: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
}
.l4iv-line {
  display: flex;
  align-items: baseline;
  padding: 1px 10px;
  cursor: pointer;
  user-select: text;
}
.l4iv-line:hover {
  background: rgba(139, 92, 246, 0.1);
}
.l4iv-line.active {
  background: rgba(139, 92, 246, 0.22);
  border-left: 3px solid #a78bfa;
  padding-left: 7px;
}
.l4iv-ln {
  width: 28px;
  flex-shrink: 0;
  opacity: 0.45;
  font-size: 11px;
  text-align: right;
  margin-right: 10px;
  user-select: none;
}
.l4iv-src {
  flex: 1;
  white-space: pre;
}
.l4iv-badge {
  margin-left: 8px;
  padding: 0 6px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 600;
  background: rgba(139, 92, 246, 0.2);
  color: #c4b5fd;
}
.l4iv-badge.done {
  background: rgba(16, 185, 129, 0.2);
  color: #34d399;
}
.l4iv-badge.err {
  background: rgba(239, 68, 68, 0.25);
  color: #f87171;
}
.l4iv-badge.warn {
  background: rgba(245, 158, 11, 0.25);
  color: #fbbf24;
}
.l4iv-info-pane {
  max-height: 440px;
  overflow-y: auto;
  overflow-x: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.l4iv-wrap.l4iv-expanded .l4iv-code-pane,
.l4iv-wrap.l4iv-expanded .l4iv-info-pane,
.l4iv-wrap.l4iv-expanded .l4iv-pre {
  max-height: none;
}
.l4iv-section {
  border: 1px solid rgba(127, 127, 127, 0.24);
  border-radius: 6px;
  padding: 8px 10px;
  background: rgba(127, 127, 127, 0.05);
}
.l4iv-sec-GoalBefore { border-left: 3px solid #818cf8; }
.l4iv-sec-GoalAfter { border-left: 3px solid #a78bfa; }
.l4iv-sec-Done {
  border-left: 3px solid #34d399;
  background: rgba(16, 185, 129, 0.1);
}
.l4iv-sec-MsgErr {
  border-left: 4px solid #f87171;
  background: rgba(239, 68, 68, 0.16);
}
.l4iv-sec-MsgWarn {
  border-left: 4px solid #fbbf24;
  background: rgba(245, 158, 11, 0.14);
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
}
.l4iv-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow-y: auto;
  font-family: inherit;
  font-size: 12.5px;
}
.l4iv-turnstile {
  color: #a78bfa;
  font-weight: 700;
}
.l4iv-goal-sep {
  border: none;
  border-top: 1px dashed rgba(127, 127, 127, 0.28);
  margin: 7px 0;
}
</style>
""".strip()


def render_rocq_infoview_html(payload: RocqInfoViewPayload) -> str:
    """Render the interactive Split-Pane Rocq InfoView widget HTML."""
    root_id = f"l4iv-{uuid.uuid4().hex[:10]}"
    lines = (payload.source or "").split("\n")

    # Map line numbers to step badges
    line_badges: dict[int, tuple[str, str]] = {}
    for st in payload.steps:
        if st.badge:
            b_cls = (
                "err"
                if not st.ok
                else ("done" if st.badge == "✓" else ("warn" if "shelved" in st.badge or "admitted" in st.badge else ""))
            )
            line_badges[st.end_line] = (st.badge, b_cls)

    active_line = payload.steps[-1].end_line if payload.steps else 1
    code_rows: list[str] = []
    for idx, text in enumerate(lines, start=1):
        act_cls = " active" if idx == active_line else ""
        badge_html = ""
        if idx in line_badges:
            b_txt, b_cls = line_badges[idx]
            extra = f" {b_cls}" if b_cls else ""
            badge_html = f"<span class='l4iv-badge{extra}'>{html.escape(b_txt)}</span>"
        code_rows.append(
            f"<div class='l4iv-line{act_cls}' data-line='{idx}'>"
            f"<span class='l4iv-ln'>{idx}</span>"
            f"<span class='l4iv-src'>{html.escape(text) or ' '}</span>"
            f"{badge_html}</div>"
        )

    ssr_right = _render_ssr_info_pane(payload)
    json_data = json.dumps(asdict(payload), ensure_ascii=False).replace("</", "<\\/")

    script = f"""
<script>
(function() {{
  const root = document.getElementById("{root_id}");
  if (!root) return;
  const payload = {json_data};
  const lines = Array.from(root.querySelectorAll(".l4iv-line"));
  const infoPane = root.querySelector(".l4iv-info-pane");
  const posLabel = root.querySelector(".l4iv-pos-label");
  const expandBtn = root.querySelector(".l4iv-expand-btn");
  let currentStepIdx = payload.steps.length > 0 ? payload.steps.length - 1 : -1;
  let viewMode = "both";

  function esc(s) {{
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }}
  function fmtGoal(g) {{
    return esc(g).split("\\n").map(line => {{
      const tr = line.trimStart();
      const ind = line.slice(0, line.length - tr.length);
      if (tr.startsWith("⊢")) {{
        return ind + "<span class='l4iv-turnstile'>⊢</span><strong>" + tr.slice(1) + "</strong>";
      }}
      return line;
    }}).join("\\n");
  }}

  function renderStep(stepIdx) {{
    if (!payload.steps.length) return;
    currentStepIdx = Math.max(0, Math.min(stepIdx, payload.steps.length - 1));
    const st = payload.steps[currentStepIdx];
    lines.forEach(el => {{
      const ln = parseInt(el.getAttribute("data-line") || "1", 10);
      el.classList.toggle("active", ln >= st.line && ln <= st.end_line);
    }});
    if (posLabel) {{
      posLabel.textContent = "Ln " + st.line + ":" + st.col + " · state #" + (st.state_id_after || st.state_id_before);
    }}
    let htmlParts = [];
    const cmdShort = esc(st.sentence.slice(0, 48));
    if ((viewMode === "both" || viewMode === "before") && st.goals_before && st.goals_before.length) {{
      htmlParts.push(
        "<div class='l4iv-section l4iv-sec-GoalBefore'>" +
        "<div class='l4iv-sec-hdr'><span>Goals Before <code>" + cmdShort + "</code> (" + st.goals_before.length + ")</span><span>state #" + st.state_id_before + "</span></div>" +
        "<pre class='l4iv-pre'>" + st.goals_before.map(fmtGoal).join("<hr class='l4iv-goal-sep'/>") + "</pre></div>"
      );
    }}
    if (viewMode === "both" || viewMode === "after") {{
      if (st.goals_after && st.goals_after.length) {{
        htmlParts.push(
          "<div class='l4iv-section l4iv-sec-GoalAfter'>" +
          "<div class='l4iv-sec-hdr'><span>Focused Goals (" + st.goals_after.length + ")</span><span>Ln " + st.line + ":" + st.col + "</span></div>" +
          "<pre class='l4iv-pre'>" + st.goals_after.map(fmtGoal).join("<hr class='l4iv-goal-sep'/>") + "</pre></div>"
        );
      }} else if (st.ok && ((st.goals_before && st.goals_before.length) || st.badge === "✓")) {{
        htmlParts.push(
          "<div class='l4iv-section l4iv-sec-Done'>" +
          "<div class='l4iv-sec-hdr'><span>Goals After <code>" + cmdShort + "</code></span><span>✓ Resolved</span></div>" +
          "<pre class='l4iv-pre'><strong>✓ Goals accomplished!</strong></pre></div>"
        );
      }}
    }}
    if (st.shelved_goals && st.shelved_goals.length) {{
      htmlParts.push(
        "<div class='l4iv-section l4iv-sec-MsgWarn'>" +
        "<div class='l4iv-sec-hdr'><span>Shelved Goals (" + st.shelved_goals.length + ")</span><span>shelved</span></div>" +
        "<pre class='l4iv-pre'>" + st.shelved_goals.map(fmtGoal).join("<hr class='l4iv-goal-sep'/>") + "</pre></div>"
      );
    }}
    if (!st.ok && st.error_message) {{
      htmlParts.push(
        "<div class='l4iv-section l4iv-sec-MsgErr'>" +
        "<div class='l4iv-sec-hdr'><span>ERROR</span><span>Ln " + st.line + ":" + st.col + "</span></div>" +
        "<pre class='l4iv-pre'>" + esc(st.error_message) + "</pre></div>"
      );
    }}
    payload.messages.forEach(m => {{
      if (m.line >= st.line && m.line <= st.end_line && m.data !== st.error_message) {{
        const cls = m.severity === "error" ? "l4iv-sec-MsgErr" : (m.severity === "warning" ? "l4iv-sec-MsgWarn" : "l4iv-sec-MsgInfo");
        htmlParts.push(
          "<div class='l4iv-section " + cls + "'>" +
          "<div class='l4iv-sec-hdr'><span>" + esc(String(m.severity).toUpperCase()) + "</span><span>Ln " + m.line + ":" + m.col + "</span></div>" +
          "<pre class='l4iv-pre'>" + esc(m.data) + "</pre></div>"
        );
      }}
    }});
    if (!htmlParts.length) {{
      htmlParts.push(
        "<div class='l4iv-section l4iv-sec-Done'>" +
        "<div class='l4iv-sec-hdr'><span>Sentence <code>" + cmdShort + "</code></span><span>state #" + (st.state_id_after || st.state_id_before) + "</span></div>" +
        "<pre class='l4iv-pre'>No active proof goals at this sentence.</pre></div>"
      );
    }}
    infoPane.innerHTML = htmlParts.join("");
  }}

  function selectByLineCol(line, col) {{
    if (!payload.steps.length) return;
    for (let i = payload.steps.length - 1; i >= 0; i--) {{
      const s = payload.steps[i];
      if (line >= s.line && line <= s.end_line) {{
        renderStep(i);
        return;
      }}
    }}
    for (let i = payload.steps.length - 1; i >= 0; i--) {{
      if (payload.steps[i].line <= line) {{
        renderStep(i);
        return;
      }}
    }}
    renderStep(0);
  }}

  lines.forEach(el => {{
    el.addEventListener("click", () => {{
      const ln = parseInt(el.getAttribute("data-line") || "1", 10);
      selectByLineCol(ln, 0);
    }});
  }});

  root.querySelectorAll("[data-nav]").forEach(btn => {{
    btn.addEventListener("click", () => {{
      const dir = btn.getAttribute("data-nav");
      if (dir === "prev") renderStep(currentStepIdx - 1);
      if (dir === "next") renderStep(currentStepIdx + 1);
    }});
  }});

  root.querySelectorAll("[data-mode]").forEach(btn => {{
    btn.addEventListener("click", () => {{
      viewMode = btn.getAttribute("data-mode") || "both";
      root.querySelectorAll("[data-mode]").forEach(b => b.classList.toggle("active", b === btn));
      renderStep(currentStepIdx);
    }});
  }});

  if (expandBtn) {{
    expandBtn.addEventListener("click", () => {{
      const expanded = root.classList.toggle("l4iv-expanded");
      expandBtn.classList.toggle("active", expanded);
      expandBtn.textContent = expanded ? "↕ Collapse" : "↕ Expand";
    }});
  }}

  root.selectCursor = selectByLineCol;
}})();
</script>
""".strip()

    return (
        f"{_ROCQ_INFOVIEW_STYLE}"
        f"<div id='{root_id}' class='l4iv-wrap l4iv-root' data-rocq-infoview='1'>"
        "<div class='l4iv-hdr'>"
        f"<div class='l4iv-title'><span>🐓 Rocq InfoView</span><span class='l4iv-pos-label' style='font-weight:400;opacity:0.8;font-size:11px'>STM #{payload.state_id}</span></div>"
        "<div class='l4iv-controls'>"
        "<button type='button' class='l4iv-btn' data-nav='prev'>◀ Prev</button>"
        "<button type='button' class='l4iv-btn' data-nav='next'>Next ▶</button>"
        "<button type='button' class='l4iv-btn active' data-mode='both'>Before → After</button>"
        "<button type='button' class='l4iv-btn' data-mode='after'>After</button>"
        "<button type='button' class='l4iv-btn' data-mode='before'>Before</button>"
        "<button type='button' class='l4iv-btn l4iv-expand-btn'>↕ Expand</button>"
        "</div></div>"
        "<div class='l4iv-split'>"
        f"<div class='l4iv-code-pane l4iv-scrollable'>{''.join(code_rows)}</div>"
        f"<div class='l4iv-info-pane l4iv-scrollable'>{ssr_right}</div>"
        "</div>"
        f"{script}"
        "</div>"
    )


def render_rocq_outcome_text(outcome: RocqExecutionOutcome) -> str:
    """Format a plaintext summary of ``RocqExecutionOutcome`` for ``text/plain`` MIME output."""
    parts: list[str] = []
    for note in outcome.notes:
        parts.append(f"[STM] {note}")
    for fb in outcome.feedbacks:
        parts.append(f"[{fb.severity.upper()}] {fb.message}")
    if not outcome.ok and outcome.error_message:
        already = any(outcome.error_message in p for p in parts)
        if not already:
            parts.append(f"[ERROR] {outcome.error_message}")
    if outcome.goals is not None:
        if outcome.goals.focused:
            parts.append(f"Focused goals ({len(outcome.goals.focused)}):")
            for idx, g in enumerate(outcome.goals.focused, start=1):
                parts.append(f"--- Goal #{idx} (id {g.goal_id}) ---")
                parts.append(g.format_block())
        elif outcome.goals.is_proved:
            parts.append("✓ No more subgoals.")
        if outcome.goals.shelved:
            parts.append(f"Shelved goals: {len(outcome.goals.shelved)}")
        if outcome.goals.admitted:
            parts.append(f"Admitted goals: {len(outcome.goals.admitted)}")
    if not parts:
        parts.append(f"Rocq STM state #{outcome.state_id} ✓")
    return "\n".join(parts)


def render_rocq_outcome(outcome: RocqExecutionOutcome) -> tuple[str, str]:
    """Return ``(text_plain, text_html)`` for a ``RocqExecutionOutcome``."""
    payload = extract_rocq_infoview_payload(outcome)
    return render_rocq_outcome_text(outcome), render_rocq_infoview_html(payload)
