"""Jupyter Kernel implementation (``ipykernel.kernelbase.Kernel``) for Rocq (Coq)."""

from __future__ import annotations

import html
import re
import shlex
from typing import Any

from ipykernel.kernelbase import Kernel

from .driver import RocqDriverError, RocqTimeoutError
from .infoview import render_rocq_outcome
from .session import RocqSession, _strip_leading_comments
from .unicode_map import ROCQ_KEYWORDS, ROCQ_TACTICS, UNICODE_ABBREVIATIONS

_BACKSLASH_TOKEN_RE = re.compile(r"\\[^\s\\]*$")
_IDENT_CHAR_RE = re.compile(r"[A-Za-z0-9_']")
_WORD_TOKEN_RE = re.compile(r"%?[A-Za-z_][A-Za-z0-9_']*$")

ROCQ_MAGICS: tuple[str, ...] = (
    "%help",
    "%lsmagic",
    "%goals",
    "%state",
    "%history",
    "%undo",
    "%reset",
    "%load",
)


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


def check_rocq_completeness(code: str) -> dict[str, str]:
    """Check whether a Rocq code buffer is complete or requires continuation."""
    stripped = (code or "").strip()
    if not stripped:
        return {"status": "complete"}
    if stripped.startswith("%"):
        return {"status": "complete"}

    # Check unclosed comments (* ... *) or strings "..."
    n = len(code)
    i = 0
    comment_depth = 0
    in_string = False
    paren_depth = 0
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if comment_depth > 0:
            if ch == "(" and nxt == "*":
                comment_depth += 1
                i += 2
            elif ch == "*" and nxt == ")":
                comment_depth -= 1
                i += 2
            else:
                i += 1
            continue
        if in_string:
            if ch == '"' and nxt == '"':
                i += 2
            elif ch == '"':
                in_string = False
                i += 1
            else:
                i += 1
            continue
        if ch == "(" and nxt == "*":
            comment_depth = 1
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch in "([{":
            paren_depth += 1
        elif ch in ")]}":
            paren_depth = max(0, paren_depth - 1)
        i += 1

    if comment_depth > 0 or in_string or paren_depth > 0:
        return {"status": "incomplete", "indent": "  "}

    cleaned = _strip_leading_comments(stripped).strip()
    if not cleaned:
        return {"status": "complete"}
    if cleaned.endswith(".") or cleaned in ("-", "+", "*", "{", "}"):
        return {"status": "complete"}
    return {"status": "incomplete", "indent": "  "}


class RocqKernel(Kernel):
    """Interactive Jupyter kernel for Rocq (Coq) backed by ``coqidetop`` STM."""

    implementation = "rocq_kernel"
    implementation_version = "0.1.0"
    language = "coq"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rocq_session = RocqSession()

    @property
    def banner(self) -> str:
        return (
            f"Rocq (Coq) Jupyter Kernel v{self.implementation_version} "
            f"({self.rocq_session.rocq_version()}, powered by coqidetop STM)\n"
            "Type `%help` for magic commands, or `\\forall<Tab>` for Unicode symbols."
        )

    @property
    def language_info(self) -> dict[str, Any]:
        return {
            "name": "coq",
            "version": self.rocq_session.rocq_version(),
            "mimetype": "text/x-coq",
            "file_extension": ".v",
            "pygments_lexer": "coq",
            "codemirror_mode": "coq",
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

    def _publish_display(self, text_plain: str, text_html: str, silent: bool) -> None:
        if not silent:
            self.send_response(
                self.iopub_socket,
                "display_data",
                {
                    "data": {
                        "text/plain": text_plain,
                        "text/html": text_html,
                    },
                    "metadata": {},
                },
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

    def _handle_magic(self, code: str, silent: bool) -> dict[str, Any]:
        line = code.strip().splitlines()[0].strip()
        parts = shlex.split(line)
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("%help", "%lsmagic"):
            txt = (
                "Rocq Kernel Magic Commands:\n"
                "  %goals              Show active proof goals at current STM state\n"
                "  %state [id]         Inspect or backtrack to STM state #<id>\n"
                "  %history            Display active STM state lineage\n"
                "  %undo               Step back one cell transaction\n"
                "  %reset [--hard]     Reset to root state (or restart coqidetop)\n"
                "  %load [--fresh] f.v Execute a Rocq (.v) source file"
            )
            html_card = f"<pre class='l4iv-pre'>{html.escape(txt)}</pre>"
            self._publish_display(txt, html_card, silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        if cmd == "%goals":
            goals = self.rocq_session.current_goals()
            if goals is None or goals.is_proved:
                txt = f"STM state #{self.rocq_session.current_state_id}: No active proof goals."
            else:
                blocks = [f"--- Goal #{i} ---\n{g.format_block()}" for i, g in enumerate(goals.focused, 1)]
                txt = "\n".join(blocks)
            html_card = f"<pre class='l4iv-pre'>{html.escape(txt)}</pre>"
            self._publish_display(txt, html_card, silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        if cmd == "%state":
            if args and args[0].isdigit():
                target = int(args[0])
                ok = self.rocq_session.backtrack_to(target)
                txt = (
                    f"Backtracked to STM state #{self.rocq_session.current_state_id}"
                    if ok
                    else f"State #{target} not found in STM graph."
                )
            else:
                txt = (
                    f"Active STM state: #{self.rocq_session.current_state_id} "
                    f"(root #{self.rocq_session.root_state_id}, version: {self.rocq_session.rocq_version()})"
                )
            self._publish_display(txt, f"<pre class='l4iv-pre'>{html.escape(txt)}</pre>", silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        if cmd == "%history":
            lineage = self.rocq_session.active_lineage()
            lines_out = [
                f"#{n.state_id} (parent #{n.parent_state_id}): {n.sentence or '<root>'}"
                for n in lineage
            ]
            txt = "\n".join(lines_out)
            self._publish_display(txt, f"<pre class='l4iv-pre'>{html.escape(txt)}</pre>", silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        if cmd == "%undo":
            sid = self.rocq_session.undo()
            txt = f"Reverted to STM state #{sid}."
            self._publish_display(txt, f"<pre class='l4iv-pre'>{html.escape(txt)}</pre>", silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        if cmd == "%reset":
            hard = "--hard" in args
            sid = self.rocq_session.reset(hard=hard)
            txt = f"Reset Rocq session to root STM state #{sid} (hard={hard})."
            self._publish_display(txt, f"<pre class='l4iv-pre'>{html.escape(txt)}</pre>", silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        if cmd == "%load":
            fresh = "--fresh" in args
            file_args = [a for a in args if a != "--fresh"]
            if not file_args:
                return self._publish_error("RocqMagicError", "Usage: %load [--fresh] <path.v>", ["Usage: %load [--fresh] <path.v>"], silent)
            outcome = self.rocq_session.load_file(file_args[0], fresh=fresh)
            txt, html_out = render_rocq_outcome(outcome)
            self._publish_display(txt, html_out, silent)
            if not outcome.ok:
                err = outcome.error_message or "Failed to load .v file"
                return self._publish_error("RocqError", err, [f"\x1b[1;31mRocqError:\x1b[0m {err}"], silent)
            return {"status": "ok", "execution_count": self.execution_count, "payload": [], "user_expressions": {}}

        return self._publish_error(
            "RocqMagicError",
            f"Unknown magic command: {cmd}",
            [f"Unknown magic command: {cmd}. Type %help for available commands."],
            silent,
        )

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

        if code.lstrip().startswith("%"):
            return self._handle_magic(code, silent)

        resolved_cell_id = self._extract_cell_id(cell_id)
        try:
            outcome = self.rocq_session.execute_command(code, cell_id=resolved_cell_id)
            txt_out, html_out = render_rocq_outcome(outcome)
            self._publish_display(txt_out, html_out, silent)

            if outcome.ok:
                return {
                    "status": "ok",
                    "execution_count": self.execution_count,
                    "payload": [],
                    "user_expressions": {},
                }

            err_msg = outcome.error_message or "Rocq command failed"
            tb = [f"\x1b[1;31mRocqError (STM state #{outcome.state_id}):\x1b[0m\n{err_msg}"]
            return self._publish_error("RocqError", err_msg, tb, silent)

        except KeyboardInterrupt:
            self.rocq_session.process.interrupt()
            return self._publish_error(
                "KeyboardInterrupt",
                "Rocq execution interrupted by user.",
                ["\x1b[1;33mKeyboardInterrupt:\x1b[0m Rocq execution interrupted."],
                silent,
            )
        except (RocqTimeoutError, RocqDriverError) as exc:
            return self._publish_error(
                "RocqDriverError",
                str(exc),
                [f"\x1b[1;31mRocqDriverError:\x1b[0m {exc}"],
                silent,
            )

    def do_complete(self, code: str, cursor_pos: int) -> dict[str, Any]:
        pos = min(max(cursor_pos, 0), len(code))
        before = code[:pos]

        m_bs = _BACKSLASH_TOKEN_RE.search(before)
        if m_bs:
            prefix = m_bs.group(0)
            start = m_bs.start()
            if prefix in UNICODE_ABBREVIATIONS:
                matches = [UNICODE_ABBREVIATIONS[prefix]]
            else:
                matches = [
                    sym
                    for k, sym in sorted(UNICODE_ABBREVIATIONS.items())
                    if k.startswith(prefix)
                ]
            return {
                "status": "ok",
                "matches": matches,
                "cursor_start": start,
                "cursor_end": pos,
                "metadata": {},
            }

        m_word = _WORD_TOKEN_RE.search(before)
        if not m_word:
            return {
                "status": "ok",
                "matches": [],
                "cursor_start": pos,
                "cursor_end": pos,
                "metadata": {},
            }

        prefix = m_word.group(0)
        start = m_word.start()
        if prefix.startswith("%"):
            matches = [m for m in ROCQ_MAGICS if m.startswith(prefix)]
        else:
            candidates = list(dict.fromkeys([
                *self.rocq_session.user_identifiers(),
                *ROCQ_KEYWORDS,
                *ROCQ_TACTICS,
            ]))
            matches = [c for c in candidates if c.startswith(prefix)]

        return {
            "status": "ok",
            "matches": matches,
            "cursor_start": start,
            "cursor_end": pos,
            "metadata": {},
        }

    def do_inspect(self, code: str, cursor_pos: int, detail_level: int = 0, omit_sections: tuple[str, ...] = ()) -> dict[str, Any]:
        ident = _extract_ident_at_cursor(code, cursor_pos)
        if not ident:
            return {"status": "ok", "found": False, "data": {}, "metadata": {}}

        sig = self.rocq_session.inspect_symbol(ident)
        if not sig:
            return {"status": "ok", "found": False, "data": {}, "metadata": {}}

        return {
            "status": "ok",
            "found": True,
            "data": {
                "text/plain": sig,
                "text/html": f"<pre class='l4iv-pre'><code>{html.escape(sig)}</code></pre>",
            },
            "metadata": {},
        }

    def do_is_complete(self, code: str) -> dict[str, Any]:
        return check_rocq_completeness(code)

    def do_shutdown(self, restart: bool) -> dict[str, Any]:
        self.rocq_session.close()
        return {"status": "ok", "restart": restart}
