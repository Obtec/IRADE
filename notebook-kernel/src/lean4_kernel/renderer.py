"""Plain-text and theme-adaptive HTML rendering for Lean 4 Jupyter outputs."""

from __future__ import annotations

import html
import json
from typing import Any

from .infoview import _augment_error_message, extract_infoview_payload, render_infoview_html
from .session import ExecutionOutcome

_BASE_STYLE = """
<style>
.lean4-out {
  font-family: var(--jp-code-font-family, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace);
  font-size: var(--jp-code-font-size, 13px);
  line-height: 1.5;
  color: var(--jp-ui-font-color1, inherit);
}
.lean4-note {
  font-size: 12px;
  opacity: 0.8;
  margin: 2px 0 6px 0;
}
.lean4-card {
  border-left: 3px solid #3b82f6;
  background: rgba(59, 130, 246, 0.07);
  border-radius: 4px;
  padding: 8px 12px;
  margin: 6px 0;
}
.lean4-card-goal {
  border-left-color: #8b5cf6;
  background: rgba(139, 92, 246, 0.08);
}
.lean4-card-ok {
  border-left-color: #10b981;
  background: rgba(16, 185, 129, 0.09);
}
.lean4-card-warn {
  border-left-color: #fbbf24;
  background: rgba(245, 158, 11, 0.14);
}
.lean4-card-err {
  border-left: 4px solid #f87171;
  border: 1px solid rgba(248, 113, 113, 0.45);
  border-left-width: 4px;
  background: rgba(239, 68, 68, 0.16);
  color: var(--vsc-err-text, inherit);
}
.lean4-card-err .lean4-badge {
  background: rgba(239, 68, 68, 0.28);
  color: #ff7b72;
}
.lean4-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
  margin-bottom: 4px;
  background: rgba(127, 127, 127, 0.16);
}
.lean4-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: inherit;
}
.lean4-turnstile {
  color: #8b5cf6;
  font-weight: 700;
}
.lean4-hyp {
  opacity: 0.9;
}
</style>
""".strip()


def _format_pos(msg: dict[str, Any]) -> str:
    pos = msg.get("pos")
    if isinstance(pos, dict) and pos.get("line", 0):
        return f"line {pos['line']}:{pos.get('column', 0)}"
    return ""


def _highlight_goal_html(goal_text: str) -> str:
    escaped = html.escape(goal_text)
    lines = escaped.splitlines()
    rendered_lines: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("⊢"):
            indent = line[: len(line) - len(stripped)]
            rest = stripped[1:]
            rendered_lines.append(
                f'{indent}<span class="lean4-turnstile">⊢</span><strong>{rest}</strong>'
            )
        else:
            rendered_lines.append(f'<span class="lean4-hyp">{line}</span>')
    return "\n".join(rendered_lines)


def render_outcome(outcome: ExecutionOutcome) -> dict[str, str] | None:
    """Render ``ExecutionOutcome`` as a Jupyter MIME bundle ``{'text/plain': ..., 'text/html': ...}``.

    Returns ``None`` when the execution succeeded silently (e.g. a pure declaration ``def f := 2``
    with no messages, sorries, notes, or goals), matching standard Lean / REPL conventions.
    """
    if outcome.mode == "raw":
        pretty = json.dumps(outcome.raw_response, ensure_ascii=False, indent=2)
        if outcome.aux_stdout:
            pretty = f"{outcome.aux_stdout}\n{pretty}"
        html_body = (
            f"{_BASE_STYLE}<div class='lean4-out'>"
            f"<div class='lean4-card'><pre class='lean4-pre'>{html.escape(pretty)}</pre></div>"
            f"</div>"
        )
        return {"text/plain": pretty, "text/html": html_body}

    plain_parts: list[str] = []
    html_parts: list[str] = []

    for note in outcome.notes:
        plain_parts.append(f"ℹ {note}")
        html_parts.append(f"<div class='lean4-note'>ℹ {html.escape(note)}</div>")

    if outcome.aux_stdout:
        plain_parts.append(outcome.aux_stdout)
        html_parts.append(
            f"<div class='lean4-card'><pre class='lean4-pre'>{html.escape(outcome.aux_stdout)}</pre></div>"
        )

    # Messages (info / warning / error / trace)
    for msg in outcome.messages:
        sev = str(msg.get("severity", "info"))
        data = _augment_error_message(sev, str(msg.get("data", "")))
        loc = _format_pos(msg)
        if sev == "error":
            header = f"error ({loc})" if loc else "error"
            plain_parts.append(f"[{header}]\n{data}")
            html_parts.append(
                f"<div class='lean4-card lean4-card-err'>"
                f"<div class='lean4-badge'>{html.escape(header)}</div>"
                f"<pre class='lean4-pre'>{html.escape(data)}</pre></div>"
            )
        elif sev == "warning":
            header = f"warning ({loc})" if loc else "warning"
            plain_parts.append(f"[{header}]\n{data}")
            html_parts.append(
                f"<div class='lean4-card lean4-card-warn'>"
                f"<div class='lean4-badge'>{html.escape(header)}</div>"
                f"<pre class='lean4-pre'>{html.escape(data)}</pre></div>"
            )
        else:
            plain_parts.append(data)
            html_parts.append(
                f"<div class='lean4-card'><pre class='lean4-pre'>{html.escape(data)}</pre></div>"
            )

    # Trace lines (Tactic mode)
    if outcome.traces:
        trace_txt = "\n".join(outcome.traces)
        plain_parts.append(f"[traces]\n{trace_txt}")
        html_parts.append(
            f"<div class='lean4-card'><div class='lean4-badge'>traces</div>"
            f"<pre class='lean4-pre'>{html.escape(trace_txt)}</pre></div>"
        )

    # Tactic mode goals & status
    if outcome.mode == "tactic" and outcome.ok:
        ps_label = (
            f"proofState #{outcome.proof_state}"
            if outcome.proof_state is not None
            else "proofState"
        )
        if not outcome.goals and outcome.proof_status == "Completed":
            msg_ok = f"✓ Goals accomplished! ({ps_label}, {outcome.proof_status})"
            plain_parts.append(msg_ok)
            html_parts.append(
                f"<div class='lean4-card lean4-card-ok'><strong>{html.escape(msg_ok)}</strong></div>"
            )
        elif outcome.goals:
            n_goals = len(outcome.goals)
            badge = f"{ps_label} · {n_goals} goal{'s' if n_goals != 1 else ''}"
            if outcome.proof_status:
                badge += f" ({outcome.proof_status})"
            goals_plain = "\n\n".join(outcome.goals)
            plain_parts.append(f"[{badge}]\n{goals_plain}")
            goals_html = "<hr style='border:none;border-top:1px dashed rgba(127,127,127,0.3);margin:8px 0;'/>".join(
                f"<pre class='lean4-pre'>{_highlight_goal_html(g)}</pre>"
                for g in outcome.goals
            )
            html_parts.append(
                f"<div class='lean4-card lean4-card-goal'>"
                f"<div class='lean4-badge'>{html.escape(badge)}</div>"
                f"{goals_html}</div>"
            )
        elif outcome.proof_status:
            status_line = f"[{ps_label}] {outcome.proof_status}"
            plain_parts.append(status_line)
            html_parts.append(
                f"<div class='lean4-card lean4-card-warn'>{html.escape(status_line)}</div>"
            )

    # Open sorries (from Command mode or nested tactic sorries)
    for sorry_item in outcome.sorries:
        ps = sorry_item.get("proofState")
        goal = str(sorry_item.get("goal", "")).strip()
        loc = _format_pos(sorry_item)
        label_bits = ["open goal"]
        if ps is not None:
            label_bits.append(f"proofState #{ps}")
        if loc:
            label_bits.append(loc)
        badge = " · ".join(label_bits)
        plain_parts.append(f"[{badge}]\n{goal}")
        html_parts.append(
            f"<div class='lean4-card lean4-card-goal'>"
            f"<div class='lean4-badge'>{html.escape(badge)}</div>"
            f"<pre class='lean4-pre'>{_highlight_goal_html(goal)}</pre></div>"
        )

    # Recorded tactics (when all_tactics=True)
    if outcome.tactics:
        tac_lines = []
        for t in outcome.tactics:
            ps = t.get("proofState")
            ps_str = f"#{ps}" if ps is not None else "-"
            tac_lines.append(f"[{ps_str}] {t.get('tactic', '')}\n{t.get('goals', '')}".strip())
        joined_tacs = "\n\n".join(tac_lines)
        plain_parts.append(f"[tactics]\n{joined_tacs}")
        html_parts.append(
            f"<details class='lean4-card'><summary class='lean4-badge' style='cursor:pointer'>"
            f"Recorded tactics ({len(outcome.tactics)})</summary>"
            f"<pre class='lean4-pre' style='margin-top:6px'>{html.escape(joined_tacs)}</pre></details>"
        )

    # InfoTree JSON (only when explicitly requested via `%config infotree ...`)
    if outcome.infotree is not None and outcome.show_raw_infotree:
        tree_str = json.dumps(outcome.infotree, ensure_ascii=False, indent=2)
        plain_parts.append(f"[infotree]\n{tree_str}")
        html_parts.append(
            f"<details class='lean4-card'><summary class='lean4-badge' style='cursor:pointer'>"
            f"InfoTree JSON</summary>"
            f"<pre class='lean4-pre' style='margin-top:6px'>{html.escape(tree_str)}</pre></details>"
        )

    # Interactive Split-Pane InfoView (Method 1 + Editor Cursor Sync Hybrid)
    if (
        outcome.mode == "command"
        and outcome.source_code.strip()
        and outcome.infoview_mode != "off"
        and outcome.infotree is not None
    ):
        iv_payload = extract_infoview_payload(
            outcome.source_code,
            ok=outcome.ok,
            env=outcome.env,
            proof_state=outcome.proof_state,
            infotree=outcome.infotree,
            tactics=outcome.tactics,
            sorries=outcome.sorries,
            messages=outcome.messages,
            notes=outcome.notes,
        )
        use_infoview = (
            outcome.infoview_mode == "on" or iv_payload.has_proof_activity
        )
        if use_infoview:
            if not plain_parts:
                n_tacs = len(iv_payload.tactic_steps)
                plain_parts.append(
                    f"[Lean 4 InfoView · env #{outcome.env} · {n_tacs} tactic step{'s' if n_tacs != 1 else ''}]"
                )
            iv_html = render_infoview_html(iv_payload)
            return {
                "text/plain": "\n\n".join(plain_parts),
                "text/html": iv_html,
            }

    if not plain_parts:
        return None

    return {
        "text/plain": "\n\n".join(plain_parts),
        "text/html": f"{_BASE_STYLE}<div class='lean4-out'>{''.join(html_parts)}</div>",
    }


def render_text_card(title: str, body: str, kind: str = "info") -> dict[str, str]:
    """Render a simple titled info/error card for magic command outputs."""
    cls = {
        "info": "lean4-card",
        "ok": "lean4-card lean4-card-ok",
        "warn": "lean4-card lean4-card-warn",
        "err": "lean4-card lean4-card-err",
    }.get(kind, "lean4-card")
    plain = f"[{title}]\n{body}".strip() if title else body
    badge_html = f"<div class='lean4-badge'>{html.escape(title)}</div>" if title else ""
    html_str = (
        f"{_BASE_STYLE}<div class='lean4-out'>"
        f"<div class='{cls}'>{badge_html}"
        f"<pre class='lean4-pre'>{html.escape(body)}</pre></div></div>"
    )
    return {"text/plain": plain, "text/html": html_str}

