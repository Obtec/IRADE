"""Jupyter Kernel implementation (`ipykernel.kernelbase.Kernel`) for Lean 4."""

from __future__ import annotations

import re
from typing import Any

from ipykernel.kernelbase import Kernel

from . import __version__
from .magics import MAGIC_NAMES, dispatch_magic, is_magic_cell
from .renderer import render_outcome
from .repl import REPLError, REPLTimeoutError
from .session import ExecutionOutcome, LeanSession
from .unicode_map import LEAN4_KEYWORDS, LEAN4_TACTICS, UNICODE_ABBREVIATIONS

_BACKSLASH_TOKEN_RE = re.compile(r"\\[^\s\\]*$")
_IDENT_CHAR_RE = re.compile(r"[A-Za-z0-9_.'\u0391-\u03c9\u03ca-\u03fb]")
_WORD_TOKEN_RE = re.compile(r"#?[A-Za-z_\u0391-\u03c9\u03ca-\u03fb][A-Za-z0-9_.'\u0391-\u03c9\u03ca-\u03fb]*$")
_INCOMPLETE_SUFFIX_RE = re.compile(r"(?::=|(?:\b(?:by|where|do|with|match|fun|=>)))\s*$")


def _extract_ident_at_cursor(code: str, cursor_pos: int) -> str | None:
    if not code:
        return None
    pos = min(max(cursor_pos, 0), len(code))
    if pos == len(code) or not _IDENT_CHAR_RE.match(code[pos]):
        if pos > 0 and _IDENT_CHAR_RE.match(code[pos - 1]):
            pos -= 1
        else:
            return None
    start = pos
    while start > 0 and _IDENT_CHAR_RE.match(code[start - 1]):
        start -= 1
    end = pos + 1
    while end < len(code) and _IDENT_CHAR_RE.match(code[end]):
        end += 1
    token = code[start:end].strip(".")
    return token or None


def _format_ansi_traceback(outcome: ExecutionOutcome) -> list[str]:
    lines: list[str] = []
    err_msgs = [m for m in outcome.messages if m.get("severity") == "error"]
    if err_msgs:
        for msg in err_msgs:
            pos = msg.get("pos")
            loc = ""
            if isinstance(pos, dict) and pos.get("line", 0):
                loc = f" (line {pos['line']}, column {pos.get('column', 0)})"
            header = f"\x1b[1;31mLeanError{loc}:\x1b[0m"
            lines.append(f"{header}\n{msg.get('data', '')}")
    elif outcome.error_summary:
        lines.append(f"\x1b[1;31mLeanError:\x1b[0m {outcome.error_summary}")
    return lines or ["\x1b[1;31mLeanError\x1b[0m"]


class Lean4Kernel(Kernel):
    """Interactive Jupyter kernel for Lean 4 backed by ``leanprover-community/repl``."""

    implementation = "lean4_kernel"
    implementation_version = __version__
    language = "lean4"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lean_session = LeanSession()

    @property
    def banner(self) -> str:
        return (
            f"Lean 4 Jupyter Kernel v{__version__} "
            f"(Lean {self.lean_session.lean_version}, powered by leanprover-community/repl)\n"
            f"Type `%help` for magic commands, or `\\alpha<Tab>` for Unicode symbols."
        )

    @property
    def language_info(self) -> dict[str, Any]:
        return {
            "name": "lean4",
            "version": self.lean_session.lean_version,
            "mimetype": "text/x-lean4",
            "file_extension": ".lean",
            "pygments_lexer": "lean4",
            "codemirror_mode": "lean4",
        }

    def _extract_cell_id(self, explicit_cell_id: str | None) -> str | None:
        if explicit_cell_id:
            return explicit_cell_id
        try:
            parent = self.get_parent("shell") if hasattr(self, "get_parent") else None
            if isinstance(parent, dict):
                meta = parent.get("metadata")
                if isinstance(meta, dict) and isinstance(meta.get("cellId"), str):
                    return meta["cellId"]
        except Exception:
            pass
        return None

    def _publish_display(self, mime_bundle: dict[str, str] | None, silent: bool) -> None:
        if not silent and mime_bundle:
            self.send_response(
                self.iopub_socket,
                "display_data",
                {"data": mime_bundle, "metadata": {}},
            )

    def _publish_error(
        self,
        ename: str,
        evalue: str,
        traceback_lines: list[str],
        silent: bool,
    ) -> dict[str, Any]:
        if not silent:
            self.send_response(
                self.iopub_socket,
                "error",
                {
                    "ename": ename,
                    "evalue": evalue,
                    "traceback": traceback_lines,
                },
            )
        return {
            "status": "error",
            "execution_count": self.execution_count,
            "ename": ename,
            "evalue": evalue,
            "traceback": traceback_lines,
        }

    def do_execute(
        self,
        code: str,
        silent: bool,
        store_history: bool = True,
        user_expressions: dict[str, Any] | None = None,
        allow_stdin: bool = False,
        *,
        cell_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not code or not code.strip():
            return {
                "status": "ok",
                "execution_count": self.execution_count,
                "payload": [],
                "user_expressions": {},
            }

        resolved_cell_id = self._extract_cell_id(cell_id)

        try:
            if is_magic_cell(code):
                mres = dispatch_magic(self.lean_session, code)
                if mres.ok:
                    self._publish_display(mres.mime_bundle, silent)
                    return {
                        "status": "ok",
                        "execution_count": self.execution_count,
                        "payload": [],
                        "user_expressions": {},
                    }
                err_val = mres.error_summary or "Magic command failed"
                return self._publish_error(
                    "LeanMagicError",
                    err_val,
                    [f"\x1b[1;31mLeanMagicError:\x1b[0m {err_val}"],
                    silent,
                )

            outcome = self.lean_session.execute_command(code, cell_id=resolved_cell_id)
            if outcome.ok:
                self._publish_display(render_outcome(outcome), silent)
                return {
                    "status": "ok",
                    "execution_count": self.execution_count,
                    "payload": [],
                    "user_expressions": {},
                }

            # Display any non-error context or Interactive InfoView (when proof failed mid-tactic)
            non_error_msgs = [
                m for m in outcome.messages if m.get("severity") != "error"
            ]
            if (
                non_error_msgs
                or outcome.sorries
                or outcome.notes
                or outcome.aux_stdout
                or (outcome.infotree is not None and outcome.infoview_mode != "off")
            ):
                partial = ExecutionOutcome(
                    ok=False,
                    mode=outcome.mode,
                    env=outcome.env,
                    proof_state=outcome.proof_state,
                    messages=outcome.messages if outcome.infotree is not None else non_error_msgs,
                    sorries=outcome.sorries,
                    tactics=outcome.tactics,
                    traces=outcome.traces,
                    infotree=outcome.infotree,
                    show_raw_infotree=outcome.show_raw_infotree,
                    infoview_mode=outcome.infoview_mode,
                    source_code=outcome.source_code,
                    aux_stdout=outcome.aux_stdout,
                    notes=outcome.notes,
                )
                # Only publish before traceback if InfoView or non-error cards were produced
                if outcome.infotree is None or non_error_msgs or outcome.sorries or outcome.notes or outcome.infoview_mode != "off":
                    bundle = render_outcome(partial)
                    # Avoid duplicating pure error card when InfoView did not activate
                    if bundle and (
                        "l4iv-wrap" in bundle.get("text/html", "")
                        or non_error_msgs
                        or outcome.sorries
                        or outcome.notes
                        or outcome.aux_stdout
                    ):
                        self._publish_display(bundle, silent)

            tb = _format_ansi_traceback(outcome)
            evalue = outcome.error_summary or "Lean elaboration error"
            return self._publish_error("LeanError", evalue, tb, silent)

        except KeyboardInterrupt:
            return self._publish_error(
                "KeyboardInterrupt",
                "Execution interrupted by user (previous Lean environment preserved).",
                [
                    "\x1b[1;33mKeyboardInterrupt:\x1b[0m Execution interrupted. "
                    "Previous Lean environment will be automatically restored on next run."
                ],
                silent,
            )
        except REPLTimeoutError as exc:
            return self._publish_error(
                "LeanTimeoutError",
                str(exc),
                [f"\x1b[1;31mLeanTimeoutError:\x1b[0m {exc}"],
                silent,
            )
        except REPLError as exc:
            return self._publish_error(
                "LeanREPLError",
                str(exc),
                [f"\x1b[1;31mLeanREPLError:\x1b[0m {exc}"],
                silent,
            )

    def do_complete(self, code: str, cursor_pos: int) -> dict[str, Any]:
        cursor_pos = min(max(cursor_pos, 0), len(code))
        prefix = code[:cursor_pos]

        # 1. Backslash Unicode symbol completion (`\alpha` -> `α`, `\to` -> `→`, etc.)
        bs_match = _BACKSLASH_TOKEN_RE.search(prefix)
        if bs_match:
            frag = bs_match.group(0)
            start = bs_match.start()
            # Put exact match first if present, then prefix matches
            matched_pairs: list[tuple[str, str]] = []
            if frag in UNICODE_ABBREVIATIONS:
                matched_pairs.append((frag, UNICODE_ABBREVIATIONS[frag]))
            for abbr, sym in sorted(UNICODE_ABBREVIATIONS.items()):
                if abbr != frag and abbr.startswith(frag):
                    matched_pairs.append((abbr, sym))

            if matched_pairs:
                unique_syms: list[str] = []
                exp_types: list[dict[str, Any]] = []
                for abbr, sym in matched_pairs:
                    if sym not in unique_syms:
                        unique_syms.append(sym)
                        exp_types.append(
                            {
                                "text": sym,
                                "type": f"{abbr} → {sym}",
                                "start": start,
                                "end": cursor_pos,
                            }
                        )
                return {
                    "status": "ok",
                    "matches": unique_syms,
                    "cursor_start": start,
                    "cursor_end": cursor_pos,
                    "metadata": {"_jupyter_types_experimental": exp_types},
                }

        # 2. Magic command completion at the start of the cell
        lstripped = prefix.lstrip()
        if lstripped.startswith("%") and "\n" not in lstripped:
            start = len(prefix) - len(lstripped)
            matches = [m for m in MAGIC_NAMES if m.startswith(lstripped)]
            return {
                "status": "ok",
                "matches": matches,
                "cursor_start": start,
                "cursor_end": cursor_pos,
                "metadata": {},
            }

        # 3. Lean keywords, `#` commands, tactics, and user-declared identifiers
        word_match = _WORD_TOKEN_RE.search(prefix)
        if word_match:
            frag = word_match.group(0)
            start = word_match.start()
            pool = list(dict.fromkeys([
                *self.lean_session.user_identifiers(),
                *LEAN4_KEYWORDS,
                *LEAN4_TACTICS,
            ]))
            matches = [item for item in pool if item.startswith(frag)]
            return {
                "status": "ok",
                "matches": matches,
                "cursor_start": start,
                "cursor_end": cursor_pos,
                "metadata": {},
            }

        return {
            "status": "ok",
            "matches": [],
            "cursor_start": cursor_pos,
            "cursor_end": cursor_pos,
            "metadata": {},
        }

    def do_inspect(
        self,
        code: str,
        cursor_pos: int,
        detail_level: int = 0,
        omit_sections: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        ident = _extract_ident_at_cursor(code, cursor_pos)
        if not ident:
            return {"status": "ok", "found": False, "data": {}, "metadata": {}}

        info = self.lean_session.inspect_symbol(ident, detail_level=detail_level)
        if not info:
            return {"status": "ok", "found": False, "data": {}, "metadata": {}}

        from .renderer import render_text_card

        return {
            "status": "ok",
            "found": True,
            "data": render_text_card(ident, info, "info"),
            "metadata": {},
        }

    def do_is_complete(self, code: str) -> dict[str, str]:
        stripped = code.strip()
        if not stripped:
            return {"status": "complete"}

        # Check block comments `/- ... -/`, strings, and bracket balance
        in_string = False
        comment_depth = 0
        in_line_comment = False
        paren_stack: list[str] = []
        pairs = {"(": ")", "[": "]", "{": "}", "⟨": "⟩"}
        closers = {v: k for k, v in pairs.items()}

        i = 0
        n = len(code)
        while i < n:
            ch = code[i]
            nxt = code[i + 1] if i + 1 < n else ""

            if in_line_comment:
                if ch == "\n":
                    in_line_comment = False
                i += 1
                continue

            if comment_depth > 0:
                if ch == "/" and nxt == "-":
                    comment_depth += 1
                    i += 2
                    continue
                if ch == "-" and nxt == "/":
                    comment_depth -= 1
                    i += 2
                    continue
                i += 1
                continue

            if in_string:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_string = False
                i += 1
                continue

            if ch == "-" and nxt == "-":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "-":
                comment_depth = 1
                i += 2
                continue
            if ch == '"':
                in_string = True
                i += 1
                continue

            if ch in pairs:
                paren_stack.append(ch)
            elif ch in closers:
                if not paren_stack or paren_stack[-1] != closers[ch]:
                    return {"status": "invalid"}
                paren_stack.pop()
            i += 1

        if comment_depth > 0 or in_string or paren_stack:
            return {"status": "incomplete", "indent": "  "}

        # Multi-line cell magic or block opener without trailing blank line
        if not code.endswith("\n\n"):
            if stripped.startswith("%%") and "\n" not in stripped:
                return {"status": "incomplete", "indent": ""}
            if _INCOMPLETE_SUFFIX_RE.search(code):
                return {"status": "incomplete", "indent": "  "}

        return {"status": "complete"}

    def do_shutdown(self, restart: bool) -> dict[str, Any]:
        if restart:
            self.lean_session.reset(hard=True)
        else:
            self.lean_session.close()
        return {"status": "ok", "restart": restart}
