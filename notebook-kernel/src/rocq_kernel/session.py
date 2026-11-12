"""Stateful Rocq (Coq) session manager, sentence tokenizer, and STM snapshot DAG."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .driver import (
    RocqDriverError,
    RocqProcess,
    RocqProcessCrashedError,
    RocqTimeoutError,
    detect_rocq_version,
)
from .xml_protocol import (
    RocqFeedback,
    RocqGoalsBundle,
    build_add_call,
    build_edit_at_call,
    build_goal_call,
    build_query_call,
    build_status_call,
)

_DECL_RE = re.compile(
    r"^\s*(?:Local\s+|Global\s+|Polymorphic\s+|Monomorphic\s+|Program\s+)*"
    r"(?:Definition|Fixpoint|CoFixpoint|Lemma|Theorem|Remark|Fact|Corollary|"
    r"Proposition|Example|Inductive|CoInductive|Record|Structure|Class|"
    r"Instance|Ltac|Axiom|Parameter|Variable|Hypothesis|Module|Section)\s+"
    r"([A-Za-z_][A-Za-z0-9_']*)"
)

_BULLET_PREFIX_RE = re.compile(r"^\s*([-+*]+|\{|\})\s*(.*)$", re.DOTALL)


@dataclass(frozen=True)
class SentenceSpan:
    """A single Rocq sentence with 1-based line and 0-based column coordinates."""

    sentence: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass
class STMNode:
    """Node in the Rocq State Transaction Manager (STM) state graph."""

    state_id: int
    parent_state_id: int | None
    sentence: str
    cell_id: str | None = None
    declared_names: tuple[str, ...] = ()
    goals: RocqGoalsBundle | None = None
    feedbacks: list[RocqFeedback] = field(default_factory=list)


@dataclass
class SentenceStepOutcome:
    """Execution record for a single Rocq sentence within a cell."""

    span: SentenceSpan
    state_id_before: int
    state_id_after: int | None
    ok: bool
    goals_before: RocqGoalsBundle | None = None
    goals_after: RocqGoalsBundle | None = None
    feedbacks: list[RocqFeedback] = field(default_factory=list)
    error_message: str | None = None
    badge: str = ""


@dataclass
class RocqExecutionOutcome:
    """Result of executing a cell or command in ``RocqSession``."""

    ok: bool
    state_id: int
    source_code: str = ""
    steps: list[SentenceStepOutcome] = field(default_factory=list)
    goals: RocqGoalsBundle | None = None
    feedbacks: list[RocqFeedback] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error_message: str | None = None
    backtracked_from: int | None = None


def extract_declared_name(sentence: str) -> str | None:
    """Extract the identifier declared by a Rocq vernacular sentence, if any."""
    # Strip leading comments before matching
    cleaned = _strip_leading_comments(sentence)
    m = _DECL_RE.match(cleaned)
    return m.group(1) if m else None


def _strip_leading_comments(text: str) -> str:
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i + 1 < n and text[i : i + 2] == "(*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if i + 1 < n and text[i : i + 2] == "(*":
                    depth += 1
                    i += 2
                elif i + 1 < n and text[i : i + 2] == "*)":
                    depth -= 1
                    i += 2
                else:
                    i += 1
        else:
            break
    return text[i:]


def tokenize_sentences(code: str) -> list[SentenceSpan]:
    """Tokenize Rocq source code into discrete sentences.

    Sentences are terminated by ``.`` followed by whitespace or EOF, ignoring
    periods inside nested comments ``(* ... *)``, string literals ``"..."``,
    or range tokens ``..``.
    """
    spans: list[SentenceSpan] = []
    n = len(code)
    i = 0
    line = 1
    col = 0

    in_token = False
    tok_start_idx = 0
    tok_start_line = 1
    tok_start_col = 0
    comment_depth = 0
    in_string = False

    def advance(steps: int = 1) -> None:
        nonlocal i, line, col
        for _ in range(steps):
            if i < n:
                if code[i] == "\n":
                    line += 1
                    col = 0
                else:
                    col += 1
                i += 1

    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if comment_depth > 0:
            if ch == "(" and nxt == "*":
                comment_depth += 1
                advance(2)
            elif ch == "*" and nxt == ")":
                comment_depth -= 1
                advance(2)
            else:
                advance(1)
            continue

        if in_string:
            if ch == '"' and nxt == '"':
                advance(2)
            elif ch == '"':
                in_string = False
                advance(1)
            else:
                advance(1)
            continue

        if not in_token:
            if ch.isspace():
                advance(1)
                continue
            if ch == "(" and nxt == "*":
                comment_depth = 1
                advance(2)
                continue
            in_token = True
            tok_start_idx = i
            tok_start_line = line
            tok_start_col = col

        if ch == "(" and nxt == "*":
            comment_depth = 1
            advance(2)
            continue

        if ch == '"':
            in_string = True
            advance(1)
            continue

        if ch == ".":
            # Ignore '..' range or ellipsis tokens
            if nxt == ".":
                while i < n and code[i] == ".":
                    advance(1)
                continue
            # A sentence terminator is '.' followed by whitespace or EOF
            if i + 1 == n or nxt.isspace():
                end_line = line
                end_col = col + 1
                advance(1)
                raw_sentence = code[tok_start_idx:i].strip()
                if raw_sentence:
                    spans.append(
                        SentenceSpan(
                            sentence=raw_sentence,
                            start_line=tok_start_line,
                            start_col=tok_start_col,
                            end_line=end_line,
                            end_col=end_col,
                        )
                    )
                in_token = False
                continue

        advance(1)

    if in_token:
        leftover = code[tok_start_idx:n].strip()
        cleaned = _strip_leading_comments(leftover).strip()
        if cleaned:
            spans.append(
                SentenceSpan(
                    sentence=leftover,
                    start_line=tok_start_line,
                    start_col=tok_start_col,
                    end_line=line,
                    end_col=col,
                )
            )

    return spans


def _split_bullet_subcommands(sentence: str) -> list[str]:
    """Split leading proof bullets (``-``, ``+``, ``*``, ``{``, ``}``) into separate STM calls."""
    parts: list[str] = []
    rem = _strip_leading_comments(sentence).strip()
    while rem:
        m = _BULLET_PREFIX_RE.match(rem)
        if not m:
            parts.append(rem)
            break
        bullet, rest = m.group(1), m.group(2).strip()
        parts.append(bullet)
        rem = rest
    return parts or [sentence]


class RocqSession:
    """Manages a persistent Rocq session with STM state tracking and auto-backtracking."""

    def __init__(
        self,
        binary_path: str | Path | None = None,
        cwd: str | Path | None = None,
        default_timeout: float = 30.0,
        rollback_whole_cell_on_error: bool = True,
        process: RocqProcess | None = None,
    ) -> None:
        self.cwd = Path(cwd).resolve() if cwd else Path.cwd()
        self.default_timeout = default_timeout
        self.rollback_whole_cell_on_error = rollback_whole_cell_on_error
        self.process = process or RocqProcess(
            binary_path=binary_path,
            cwd=self.cwd,
            default_timeout=default_timeout,
        )

        self._started = False
        self.root_state_id: int = 1
        self.current_state_id: int = 1
        self._edit_counter: int = 0
        self.nodes: dict[int, STMNode] = {}
        self.history_stack: list[int] = []
        self._cell_entry_state: dict[str, int] = {}
        self.last_outcome: RocqExecutionOutcome | None = None

    def ensure_started(self) -> int:
        if not self._started or not self.process.is_alive:
            root_id = self.process.start()
            self.root_state_id = root_id
            self.current_state_id = root_id
            self.nodes = {
                root_id: STMNode(
                    state_id=root_id,
                    parent_state_id=None,
                    sentence="",
                    cell_id=None,
                )
            }
            self.history_stack = [root_id]
            self._cell_entry_state.clear()
            self._started = True
        return self.current_state_id

    def rocq_version(self) -> str:
        return detect_rocq_version()

    def active_lineage(self, state_id: int | None = None) -> list[STMNode]:
        """Return the chain of ``STMNode``s from root down to ``state_id``."""
        target = self.current_state_id if state_id is None else state_id
        chain: list[STMNode] = []
        visited: set[int] = set()
        curr: int | None = target
        while curr is not None and curr in self.nodes and curr not in visited:
            visited.add(curr)
            node = self.nodes[curr]
            chain.append(node)
            curr = node.parent_state_id
        chain.reverse()
        return chain

    def user_identifiers(self) -> list[str]:
        """Return all user-declared identifiers in the active STM lineage."""
        seen: set[str] = set()
        result: list[str] = []
        for node in self.active_lineage():
            for name in node.declared_names:
                if name not in seen:
                    seen.add(name)
                    result.append(name)
        return result

    def backtrack_to(self, target_state_id: int) -> bool:
        """Issue ``Edit_at`` to revert the Rocq STM to ``target_state_id``."""
        self.ensure_started()
        if target_state_id not in self.nodes:
            return False
        res = self.process.send_call(
            build_edit_at_call(target_state_id),
            timeout=self.default_timeout,
        )
        if res.ok:
            self.current_state_id = target_state_id
            return True
        return False

    def auto_backtrack(self, cell_id: str | None = None, code: str = "") -> int | None:
        """Automatically backtrack if re-executing an earlier cell or redefining a symbol.

        Returns the state ID backtracked to, or ``None`` if no backtrack was needed.
        """
        self.ensure_started()
        target_state: int | None = None

        # 1. Check if this cell_id was previously executed in the active lineage
        if cell_id and cell_id in self._cell_entry_state:
            candidate = self._cell_entry_state[cell_id]
            active_ids = {n.state_id for n in self.active_lineage()}
            if candidate in active_ids and candidate != self.current_state_id:
                target_state = candidate

        # 2. Check if any sentence in `code` redefines an identifier in active lineage
        if target_state is None and code:
            spans = tokenize_sentences(code)
            new_decls = {
                name
                for sp in spans
                if (name := extract_declared_name(sp.sentence)) is not None
            }
            if new_decls:
                for node in self.active_lineage():
                    if any(d in new_decls for d in node.declared_names):
                        if node.parent_state_id is not None:
                            target_state = node.parent_state_id
                            break

        if target_state is not None and target_state != self.current_state_id:
            if self.backtrack_to(target_state):
                return target_state
        return None

    def execute_command(
        self,
        code: str,
        cell_id: str | None = None,
        timeout: float | None = None,
    ) -> RocqExecutionOutcome:
        """Execute one or more Rocq sentences in a cell with atomic rollback on error."""
        self.ensure_started()
        eff_timeout = self.default_timeout if timeout is None else timeout
        notes: list[str] = []

        backtracked_to = self.auto_backtrack(cell_id=cell_id, code=code)
        if backtracked_to is not None:
            notes.append(f"Auto-backtracked to STM state #{backtracked_to}")

        pre_cell_state = self.current_state_id
        if cell_id:
            self._cell_entry_state[cell_id] = pre_cell_state

        spans = tokenize_sentences(code)
        if not spans:
            outcome = RocqExecutionOutcome(
                ok=True,
                state_id=self.current_state_id,
                source_code=code,
                goals=self.nodes.get(self.current_state_id, STMNode(self.current_state_id, None, "")).goals,
                notes=notes,
                backtracked_from=backtracked_to,
            )
            self.last_outcome = outcome
            return outcome

        steps: list[SentenceStepOutcome] = []
        all_feedbacks: list[RocqFeedback] = []
        prev_goals = self.nodes[self.current_state_id].goals if self.current_state_id in self.nodes else None

        for span in spans:
            state_before = self.current_state_id
            sub_cmds = _split_bullet_subcommands(span.sentence)
            step_feedbacks: list[RocqFeedback] = []
            step_ok = True
            step_err: str | None = None
            state_after: int | None = state_before
            goals_after: RocqGoalsBundle | None = prev_goals

            for sub_cmd in sub_cmds:
                self._edit_counter += 1
                try:
                    add_res = self.process.send_call(
                        build_add_call(
                            sentence=sub_cmd,
                            edit_id=self._edit_counter,
                            state_id=self.current_state_id,
                            line_nb=span.start_line,
                        ),
                        timeout=eff_timeout,
                    )
                except (RocqTimeoutError, RocqProcessCrashedError) as exc:
                    step_ok = False
                    step_err = str(exc)
                    self._recover_after_crash(pre_cell_state)
                    break

                step_feedbacks.extend(add_res.feedbacks)
                if not add_res.ok or add_res.state_id is None:
                    step_ok = False
                    step_err = add_res.error_message or "Syntax or command error in Add"
                    break

                new_sid = add_res.state_id
                # Force evaluation and query goals at the new state
                try:
                    goal_res = self.process.send_call(build_goal_call(), timeout=eff_timeout)
                except (RocqTimeoutError, RocqProcessCrashedError) as exc:
                    step_ok = False
                    step_err = str(exc)
                    self._recover_after_crash(pre_cell_state)
                    break

                step_feedbacks.extend(goal_res.feedbacks)
                has_err_fb = any(fb.severity == "error" for fb in goal_res.feedbacks)
                if not goal_res.ok or has_err_fb:
                    step_ok = False
                    step_err = goal_res.error_message
                    if not step_err:
                        for fb in goal_res.feedbacks:
                            if fb.severity == "error" and fb.message:
                                step_err = fb.message
                                break
                    step_err = step_err or "Rocq tactic or typecheck error"
                    break

                # Commit node in STM graph
                decl_name = extract_declared_name(sub_cmd)
                decls = (decl_name,) if decl_name else ()
                node = STMNode(
                    state_id=new_sid,
                    parent_state_id=self.current_state_id,
                    sentence=sub_cmd,
                    cell_id=cell_id,
                    declared_names=decls,
                    goals=goal_res.goals,
                    feedbacks=list(goal_res.feedbacks),
                )
                self.nodes[new_sid] = node
                self.current_state_id = new_sid
                state_after = new_sid
                goals_after = goal_res.goals

            all_feedbacks.extend(step_feedbacks)

            if not step_ok:
                rollback_target = pre_cell_state if self.rollback_whole_cell_on_error else state_before
                self.backtrack_to(rollback_target)
                notes.append(f"Rolled back STM to state #{rollback_target} due to error.")
                steps.append(
                    SentenceStepOutcome(
                        span=span,
                        state_id_before=state_before,
                        state_id_after=None,
                        ok=False,
                        goals_before=prev_goals,
                        goals_after=None,
                        feedbacks=step_feedbacks,
                        error_message=step_err,
                        badge="error",
                    )
                )
                outcome = RocqExecutionOutcome(
                    ok=False,
                    state_id=self.current_state_id,
                    source_code=code,
                    steps=steps,
                    goals=self.nodes[self.current_state_id].goals if self.current_state_id in self.nodes else None,
                    feedbacks=all_feedbacks,
                    notes=notes,
                    error_message=step_err,
                    backtracked_from=backtracked_to,
                )
                self.last_outcome = outcome
                return outcome

            badge = self._compute_step_badge(span.sentence, prev_goals, goals_after)
            steps.append(
                SentenceStepOutcome(
                    span=span,
                    state_id_before=state_before,
                    state_id_after=state_after,
                    ok=True,
                    goals_before=prev_goals,
                    goals_after=goals_after,
                    feedbacks=step_feedbacks,
                    badge=badge,
                )
            )
            prev_goals = goals_after

        if self.current_state_id != pre_cell_state:
            self.history_stack.append(self.current_state_id)

        outcome = RocqExecutionOutcome(
            ok=True,
            state_id=self.current_state_id,
            source_code=code,
            steps=steps,
            goals=prev_goals,
            feedbacks=all_feedbacks,
            notes=notes,
            backtracked_from=backtracked_to,
        )
        self.last_outcome = outcome
        return outcome

    @staticmethod
    def _compute_step_badge(
        sentence: str,
        goals_before: RocqGoalsBundle | None,
        goals_after: RocqGoalsBundle | None,
    ) -> str:
        cleaned = _strip_leading_comments(sentence).strip()
        if cleaned in ("Qed.", "Defined."):
            return "✓"
        if cleaned == "Admitted.":
            return "admitted"
        if goals_after is None:
            if goals_before is not None:
                return "✓"
            return ""
        if goals_after.shelved and not goals_after.focused:
            return f"{len(goals_after.shelved)} shelved"
        n_foc = len(goals_after.focused)
        n_bg = len(goals_after.background)
        if n_foc == 0 and n_bg == 0:
            return "✓"
        if n_foc == 1:
            return "1 goal"
        if n_foc > 1:
            return f"{n_foc} goals"
        return f"{n_bg} bg"

    def inspect_symbol(self, symbol: str) -> str | None:
        """Query ``Check <symbol>.`` without mutating the STM state."""
        self.ensure_started()
        ident = symbol.strip().rstrip(".")
        if not ident:
            return None
        query = f"Check {ident}."
        try:
            res = self.process.send_call(
                build_query_call(query, self.current_state_id),
                timeout=5.0,
            )
        except RocqDriverError:
            return None
        notices = [
            fb.message
            for fb in res.feedbacks
            if fb.severity in ("notice", "info") and fb.message
        ]
        if notices:
            return "\n".join(notices)
        return None

    def current_goals(self) -> RocqGoalsBundle | None:
        """Query the active goals at ``current_state_id``."""
        self.ensure_started()
        res = self.process.send_call(build_goal_call(), timeout=self.default_timeout)
        return res.goals if res.ok else None

    def undo(self) -> int:
        """Step back one cell/transaction in ``history_stack``."""
        self.ensure_started()
        if len(self.history_stack) > 1:
            self.history_stack.pop()
            target = self.history_stack[-1]
            self.backtrack_to(target)
        elif self.current_state_id != self.root_state_id:
            self.backtrack_to(self.root_state_id)
        return self.current_state_id

    def reset(self, hard: bool = False) -> int:
        """Reset session to root state (or restart process if ``hard=True``)."""
        if hard:
            self.process.kill()
            self._started = False
            return self.ensure_started()
        self.ensure_started()
        self.backtrack_to(self.root_state_id)
        self.history_stack = [self.root_state_id]
        self._cell_entry_state.clear()
        return self.current_state_id

    def load_file(self, path: str | Path, fresh: bool = False) -> RocqExecutionOutcome:
        """Read and execute a ``.v`` file sentence-by-sentence."""
        file_p = Path(path).expanduser()
        if not file_p.is_absolute():
            file_p = (self.cwd / file_p).resolve()
        if not file_p.is_file():
            return RocqExecutionOutcome(
                ok=False,
                state_id=self.current_state_id,
                error_message=f"Rocq file not found: {file_p}",
            )
        if fresh:
            self.reset(hard=False)
        content = file_p.read_text(encoding="utf-8")
        outcome = self.execute_command(content)
        outcome.notes.insert(0, f"Loaded {file_p.name} ({len(outcome.steps)} sentences)")
        return outcome

    def _recover_after_crash(self, target_state_id: int) -> None:
        """Respawn ``coqidetop`` after a timeout/crash and replay lineage up to ``target_state_id``."""
        lineage = [
            node.sentence
            for node in self.active_lineage(target_state_id)
            if node.sentence
        ]
        self.process.kill()
        self._started = False
        self.ensure_started()
        for sent in lineage:
            self._edit_counter += 1
            try:
                add_res = self.process.send_call(
                    build_add_call(sent, self._edit_counter, self.current_state_id),
                    timeout=self.default_timeout,
                )
                if add_res.ok and add_res.state_id is not None:
                    goal_res = self.process.send_call(build_goal_call(), timeout=self.default_timeout)
                    if goal_res.ok:
                        decl_name = extract_declared_name(sent)
                        self.nodes[add_res.state_id] = STMNode(
                            state_id=add_res.state_id,
                            parent_state_id=self.current_state_id,
                            sentence=sent,
                            declared_names=(decl_name,) if decl_name else (),
                            goals=goal_res.goals,
                        )
                        self.current_state_id = add_res.state_id
            except RocqDriverError:
                break
        self.process.send_call(build_status_call(False), timeout=self.default_timeout)

    def close(self) -> None:
        self.process.kill()
        self._started = False
