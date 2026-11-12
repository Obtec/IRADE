"""Stateful Lean 4 session management on top of leanprover-community/repl."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import textwrap
from typing import Any

from .repl import (
    REPLProcess,
    REPLProcessCrashedError,
    REPLRawResult,
    REPLTimeoutError,
    detect_lean_version,
    find_lake_project,
)

_REDECLARED_RE = re.compile(r"[`'‘“]([^`'’”]+)[`'’”]\s+has already been declared")
_DECL_NAME_RE = re.compile(
    r"(?:^|\n)\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)\s+)*"
    r"(?:def|abbrev|theorem|lemma|inductive|structure|class|opaque|axiom|instance)\s+"
    r"([A-Za-z_\u0391-\u03c9\u03ca-\u03fb][A-Za-z0-9_.'\u0391-\u03c9\u03ca-\u03fb]*)"
)
_SORRY_WARNING_SUBSTR = "uses `sorry`"
_SORRY_WARNING_SUBSTR_ALT = "uses 'sorry'"
_SORRY_WARNING_SUBSTR_UNICODE = "uses ‘sorry’"


@dataclass
class SessionConfig:
    """User-configurable session options."""

    all_tactics: bool = False
    root_goals: bool = False
    infotree: str | None = None
    infoview: str = "auto"  # "auto" | "on" | "off"
    auto_backtrack: bool = True
    timeout: float | None = None
    show_sorry_warnings: bool = False


@dataclass
class EnvNode:
    """A node in the immutable REPL environment snapshot tree."""

    env_id: int
    parent_env: int | None
    cmd: str | None = None
    file_path: str | None = None
    unpickle_path: str | None = None
    cell_id: str | None = None
    declared_names: tuple[str, ...] = ()


@dataclass
class ProofNode:
    """A node in the REPL proofState snapshot graph."""

    proof_state: int
    goals: list[str]
    parent_proof_state: int | None = None
    source_env: int | None = None
    tactic: str | None = None
    proof_status: str = "Incomplete: open goals remain"


@dataclass
class ExecutionOutcome:
    """Normalized result of executing a command, tactic, file, or pickle action."""

    ok: bool
    mode: str  # "command" | "tactic" | "file" | "pickle" | "raw"
    env: int | None = None
    proof_state: int | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    sorries: list[dict[str, Any]] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    proof_status: str | None = None
    tactics: list[dict[str, Any]] = field(default_factory=list)
    traces: list[str] = field(default_factory=list)
    infotree: Any = None
    show_raw_infotree: bool = False
    infoview_mode: str = "auto"
    source_code: str = ""
    aux_stdout: str = ""
    notes: list[str] = field(default_factory=list)
    error_summary: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


def strip_leading_lean_comments(code: str) -> str:
    """Strip leading whitespace, ``--`` line comments, and nested ``/- ... -/`` block comments."""
    i = 0
    n = len(code)
    while i < n:
        while i < n and code[i].isspace():
            i += 1
        if i + 1 < n and code[i : i + 2] == "--":
            nl = code.find("\n", i + 2)
            if nl == -1:
                return ""
            i = nl + 1
            continue
        if i + 1 < n and code[i : i + 2] == "/-":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if i + 1 < n and code[i : i + 2] == "/-":
                    depth += 1
                    i += 2
                elif i + 1 < n and code[i : i + 2] == "-/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        break
    return code[i:]


def starts_with_header_import(code: str) -> bool:
    """Return True if ``code`` begins with a Lean module header (``import`` or ``prelude``)."""
    rem = strip_leading_lean_comments(code)
    return bool(re.match(r"^(?:import|prelude)(?:\s|$)", rem))


def extract_declared_names(code: str) -> tuple[str, ...]:
    """Extract top-level declared identifier names from Lean source code."""
    seen: list[str] = []
    for match in _DECL_NAME_RE.finditer(code):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def format_tactic_input(tactic_code: str) -> str:
    """Wrap multi-line tactic sequences in ``( ... )`` so ``REPL`` parses ``tacticSeq``."""
    dedented = textwrap.dedent(tactic_code).strip()
    if "\n" not in dedented:
        return dedented
    return "(\n" + textwrap.indent(dedented, "  ") + "\n)"


class LeanSession:
    """High-level stateful Lean 4 session wrapping ``REPLProcess``."""

    def __init__(
        self,
        repl_process: REPLProcess | None = None,
        config: SessionConfig | None = None,
    ) -> None:
        self.repl = repl_process or REPLProcess()
        self.config = config or SessionConfig()

        self.current_env: int | None = None
        self.active_proof_state: int | None = None

        self.env_nodes: dict[int, EnvNode] = {}
        self.proof_nodes: dict[int, ProofNode] = {}
        self._undo_stack: list[tuple[int | None, int | None]] = []
        self._needs_recovery: bool = False
        self._lean_version: str | None = None
        self.last_outcome: ExecutionOutcome | None = None

    @property
    def lean_version(self) -> str:
        if self._lean_version is None:
            cwd = self.repl.project_dir or self.repl.working_dir
            self._lean_version = detect_lean_version(cwd=cwd)
        return self._lean_version

    def user_identifiers(self) -> list[str]:
        """Return identifiers declared along the active environment lineage."""
        names: list[str] = []
        for node in self.active_lineage():
            for name in node.declared_names:
                if name not in names:
                    names.append(name)
        return names

    def active_lineage(self, env_id: int | None = None) -> list[EnvNode]:
        """Return ordered list of ``EnvNode``s from root to ``env_id`` (or ``current_env``)."""
        target = self.current_env if env_id is None else env_id
        chain: list[EnvNode] = []
        visited: set[int] = set()
        cur = target
        while cur is not None and cur in self.env_nodes and cur not in visited:
            visited.add(cur)
            node = self.env_nodes[cur]
            chain.append(node)
            cur = node.parent_env
        chain.reverse()
        return chain

    def _command_options_payload(self) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        if self.config.all_tactics:
            opts["allTactics"] = True
        if self.config.root_goals:
            opts["rootGoals"] = True
        if self.config.infotree:
            opts["infotree"] = self.config.infotree
        elif self.config.infoview != "off":
            opts["infotree"] = "full"
        return opts

    def _raw_request_with_recovery(
        self,
        payload: dict[str, Any],
        recover_before: bool = True,
    ) -> tuple[REPLRawResult, list[str]]:
        notes: list[str] = []
        if recover_before and (
            self._needs_recovery
            or (self.current_env is not None and not self.repl.is_alive)
        ):
            rec_note = self._recover_after_crash()
            if rec_note:
                notes.append(rec_note)
            # Update payload env if it pointed to the pre-recovery current_env
            if "env" in payload and self.current_env is not None:
                payload = {**payload, "env": self.current_env}
            elif "env" in payload and self.current_env is None:
                payload = {k: v for k, v in payload.items() if k != "env"}

        try:
            res = self.repl.request(payload, timeout=self.config.timeout)
            return res, notes
        except KeyboardInterrupt:
            self._needs_recovery = True
            raise
        except (REPLTimeoutError, REPLProcessCrashedError):
            self._needs_recovery = True
            raise

    def _recover_after_crash(self) -> str | None:
        """Respawn REPL and replay active lineage after an interrupt or timeout."""
        self._needs_recovery = False
        lineage = self.active_lineage()
        self.env_nodes.clear()
        self.proof_nodes.clear()
        self.active_proof_state = None
        self._undo_stack.clear()

        self.repl.start()
        if not lineage:
            self.current_env = None
            return None

        replayed_env: int | None = None
        for old_node in lineage:
            if old_node.unpickle_path:
                req: dict[str, Any] = {"unpickleEnvFrom": old_node.unpickle_path}
            elif old_node.file_path:
                req = {"path": old_node.file_path}
                if replayed_env is not None:
                    req["env"] = replayed_env
            elif old_node.cmd is not None:
                req = {"cmd": old_node.cmd}
                if replayed_env is not None:
                    req["env"] = replayed_env
            else:  # pragma: no cover
                continue

            raw = self.repl.request(req, timeout=self.config.timeout)
            if "env" not in raw.data or self._has_error_messages(raw.data):
                break
            new_id = int(raw.data["env"])
            self.env_nodes[new_id] = EnvNode(
                env_id=new_id,
                parent_env=replayed_env,
                cmd=old_node.cmd,
                file_path=old_node.file_path,
                unpickle_path=old_node.unpickle_path,
                cell_id=old_node.cell_id,
                declared_names=old_node.declared_names,
            )
            replayed_env = new_id

        self.current_env = replayed_env
        if replayed_env is not None:
            return f"Recovered Lean environment snapshot (env #{replayed_env}) after interrupt/restart."
        return "Restarted Lean REPL with a fresh environment."

    @staticmethod
    def _has_error_messages(data: dict[str, Any]) -> bool:
        if "message" in data and "env" not in data and "proofState" not in data:
            return True
        for msg in data.get("messages", []):
            if msg.get("severity") == "error":
                return True
        return False

    @staticmethod
    def _extract_redeclared_ident(data: dict[str, Any]) -> str | None:
        for msg in data.get("messages", []):
            if msg.get("severity") == "error":
                m = _REDECLARED_RE.search(msg.get("data", ""))
                if m:
                    return m.group(1)
        return None

    def _filter_messages(
        self,
        messages: list[dict[str, Any]],
        sorries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.config.show_sorry_warnings or not sorries:
            return messages
        return [
            m
            for m in messages
            if not (
                m.get("severity") == "warning"
                and (
                    _SORRY_WARNING_SUBSTR in m.get("data", "")
                    or _SORRY_WARNING_SUBSTR_ALT in m.get("data", "")
                    or _SORRY_WARNING_SUBSTR_UNICODE in m.get("data", "")
                )
            )
        ]

    def _register_sorries(
        self,
        sorries: list[dict[str, Any]],
        source_env: int | None,
        parent_ps: int | None = None,
    ) -> int | None:
        first_ps: int | None = None
        for item in sorries:
            ps = item.get("proofState")
            if ps is not None:
                ps_id = int(ps)
                goal = item.get("goal", "")
                self.proof_nodes[ps_id] = ProofNode(
                    proof_state=ps_id,
                    goals=[goal] if goal else [],
                    parent_proof_state=parent_ps,
                    source_env=source_env,
                )
                if first_ps is None:
                    first_ps = ps_id
        return first_ps

    def execute_command(
        self,
        code: str,
        cell_id: str | None = None,
    ) -> ExecutionOutcome:
        """Execute Lean 4 declaration/command code in the current session."""
        if not code.strip() or not strip_leading_lean_comments(code).strip():
            return ExecutionOutcome(ok=True, mode="command", env=self.current_env)

        notes: list[str] = []
        base_env = self.current_env

        # In Lean REPL, `import` can only be evaluated at the root (`env` omitted).
        if starts_with_header_import(code) and base_env is not None:
            notes.append(
                f"Started a new root environment for `import` header "
                f"(previous env #{base_env} is still accessible via `%env {base_env}`)."
            )
            base_env = None

        payload: dict[str, Any] = {"cmd": code, **self._command_options_payload()}
        if base_env is not None:
            payload["env"] = base_env

        raw, rec_notes = self._raw_request_with_recovery(payload)
        notes.extend(rec_notes)
        # Re-sync base_env if recovery happened
        if rec_notes and "env" in payload:
            base_env = self.current_env

        data = raw.data
        aux_stdout = raw.aux_stdout

        # Auto-backtrack when redefining an existing declaration in notebook cells
        redeclared = self._extract_redeclared_ident(data)
        if (
            redeclared is not None
            and self.config.auto_backtrack
            and base_env is not None
        ):
            bt_result = self._try_auto_backtrack(code, base_env, redeclared)
            if bt_result is not None:
                raw, base_env, bt_note = bt_result
                data = raw.data
                aux_stdout = raw.aux_stdout
                notes.append(bt_note)

        # Top-level REPL Error (`{"message": "..."}`)
        if "message" in data and "env" not in data:
            err_msg = str(data["message"])
            return ExecutionOutcome(
                ok=False,
                mode="command",
                env=self.current_env,
                messages=[{"severity": "error", "data": err_msg}],
                aux_stdout=aux_stdout,
                notes=notes,
                error_summary=err_msg,
                raw_response=data,
            )

        has_errors = self._has_error_messages(data)
        raw_messages = list(data.get("messages", []))
        sorries = list(data.get("sorries", []))
        messages = self._filter_messages(raw_messages, sorries)
        tactics = list(data.get("tactics", []))
        infotree = data.get("infotree")

        if has_errors:
            first_err = next(
                (m.get("data", "Lean error") for m in raw_messages if m.get("severity") == "error"),
                "Lean error",
            )
            # Keep current_env unchanged so broken declarations do not pollute session!
            outcome = ExecutionOutcome(
                ok=False,
                mode="command",
                env=self.current_env,
                messages=messages,
                sorries=sorries,
                tactics=tactics,
                infotree=infotree,
                show_raw_infotree=bool(self.config.infotree),
                infoview_mode=self.config.infoview,
                source_code=code,
                aux_stdout=aux_stdout,
                notes=notes,
                error_summary=first_err,
                raw_response=data,
            )
            self.last_outcome = outcome
            return outcome

        # Advance environment snapshot cleanly
        new_env = int(data["env"])
        self._undo_stack.append((self.current_env, self.active_proof_state))
        self.env_nodes[new_env] = EnvNode(
            env_id=new_env,
            parent_env=base_env,
            cmd=code,
            cell_id=cell_id,
            declared_names=extract_declared_names(code),
        )
        self.current_env = new_env

        first_ps = self._register_sorries(sorries, source_env=new_env)
        if first_ps is not None:
            self.active_proof_state = first_ps

        outcome = ExecutionOutcome(
            ok=True,
            mode="command",
            env=new_env,
            proof_state=first_ps,
            messages=messages,
            sorries=sorries,
            tactics=tactics,
            infotree=infotree,
            show_raw_infotree=bool(self.config.infotree),
            infoview_mode=self.config.infoview,
            source_code=code,
            aux_stdout=aux_stdout,
            notes=notes,
            raw_response=data,
        )
        self.last_outcome = outcome
        return outcome

    def _try_auto_backtrack(
        self,
        code: str,
        from_env: int,
        redeclared_ident: str,
    ) -> tuple[REPLRawResult, int | None, str] | None:
        """Find the ancestor snapshot before ``redeclared_ident`` and re-evaluate ``code``."""
        lineage = self.active_lineage(from_env)
        if not lineage:
            return None

        short_ident = redeclared_ident.split(".")[-1]
        candidate_parents: list[int | None] = []

        # Fast path: match recorded declaration names in active lineage
        for node in reversed(lineage):
            if redeclared_ident in node.declared_names or short_ident in node.declared_names:
                candidate_parents.append(node.parent_env)

        # Fallback path: walk remaining ancestor parent_envs from newest to oldest
        for node in reversed(lineage):
            if node.parent_env not in candidate_parents:
                candidate_parents.append(node.parent_env)

        for cand_env in candidate_parents:
            payload: dict[str, Any] = {"cmd": code, **self._command_options_payload()}
            if cand_env is not None:
                payload["env"] = cand_env
            raw = self.repl.request(payload, timeout=self.config.timeout)
            # If still redeclared on this ancestor, keep walking further back
            if self._extract_redeclared_ident(raw.data) is not None:
                continue
            # Commit ONLY if the command compiles cleanly without errors!
            if "env" in raw.data and not self._has_error_messages(raw.data):
                new_env = raw.data["env"]
                origin_desc = f"env #{cand_env}" if cand_env is not None else "root"
                note = (
                    f"Redefined `{redeclared_ident}` by branching from {origin_desc} → env #{new_env}."
                )
                return raw, cand_env, note
            return None

        return None

    def execute_tactic(
        self,
        tactic_code: str,
        proof_state: int | None = None,
    ) -> ExecutionOutcome:
        """Execute a tactic (or multi-line tactic block) on ``proof_state``."""
        target_ps = self.active_proof_state if proof_state is None else proof_state
        if target_ps is None:
            msg = (
                "No active proofState. Declare a theorem/lemma containing `by sorry` "
                "first, or pass an explicit proofState ID (`%%tactic <id>`)."
            )
            return ExecutionOutcome(
                ok=False,
                mode="tactic",
                env=self.current_env,
                messages=[{"severity": "error", "data": msg}],
                error_summary=msg,
            )

        formatted = format_tactic_input(tactic_code)
        if not formatted:
            node = self.proof_nodes.get(target_ps)
            return ExecutionOutcome(
                ok=True,
                mode="tactic",
                env=self.current_env,
                proof_state=target_ps,
                goals=list(node.goals) if node else [],
                proof_status=node.proof_status if node else None,
            )

        payload = {"tactic": formatted, "proofState": target_ps}
        raw, notes = self._raw_request_with_recovery(payload, recover_before=False)
        data = raw.data

        if "message" in data and "proofState" not in data:
            err_msg = str(data["message"])
            return ExecutionOutcome(
                ok=False,
                mode="tactic",
                env=self.current_env,
                proof_state=target_ps,
                messages=[{"severity": "error", "data": err_msg}],
                aux_stdout=raw.aux_stdout,
                notes=notes,
                error_summary=err_msg,
                raw_response=data,
            )

        raw_messages = list(data.get("messages", []))
        sorries = list(data.get("sorries", []))
        messages = self._filter_messages(raw_messages, sorries)
        traces = list(data.get("traces", []))
        goals = list(data.get("goals", []))
        proof_status = str(data.get("proofStatus", ""))

        has_errors = self._has_error_messages(data) or proof_status.startswith("Error")
        if has_errors:
            first_err = next(
                (m.get("data", proof_status) for m in raw_messages if m.get("severity") == "error"),
                proof_status or "Tactic failed",
            )
            # Preserve active_proof_state at target_ps so user can retry cleanly
            return ExecutionOutcome(
                ok=False,
                mode="tactic",
                env=self.current_env,
                proof_state=target_ps,
                messages=messages,
                sorries=sorries,
                goals=goals,
                proof_status=proof_status,
                traces=traces,
                aux_stdout=raw.aux_stdout,
                notes=notes,
                error_summary=first_err,
                raw_response=data,
            )

        new_ps = int(data["proofState"])
        self._undo_stack.append((self.current_env, self.active_proof_state))
        self.proof_nodes[new_ps] = ProofNode(
            proof_state=new_ps,
            goals=goals,
            parent_proof_state=target_ps,
            source_env=self.current_env,
            tactic=tactic_code.strip(),
            proof_status=proof_status,
        )
        sorry_ps = self._register_sorries(
            sorries,
            source_env=self.current_env,
            parent_ps=new_ps,
        )
        self.active_proof_state = sorry_ps if (not goals and sorry_ps is not None) else new_ps

        return ExecutionOutcome(
            ok=True,
            mode="tactic",
            env=self.current_env,
            proof_state=new_ps,
            messages=messages,
            sorries=sorries,
            goals=goals,
            proof_status=proof_status,
            traces=traces,
            aux_stdout=raw.aux_stdout,
            notes=notes,
            raw_response=data,
        )

    def execute_file(self, file_path: str | Path, use_current_env: bool = True) -> ExecutionOutcome:
        """Process a ``.lean`` file via REPL File Mode."""
        resolved = Path(file_path).expanduser()
        if not resolved.is_absolute():
            base = self.repl.project_dir or self.repl.working_dir
            resolved = (base / resolved).resolve()

        if not resolved.is_file():
            msg = f"File not found: {resolved}"
            return ExecutionOutcome(
                ok=False,
                mode="file",
                env=self.current_env,
                messages=[{"severity": "error", "data": msg}],
                error_summary=msg,
            )

        file_text = resolved.read_text(encoding="utf-8")
        base_env = self.current_env if use_current_env else None
        notes: list[str] = []
        if starts_with_header_import(file_text) and base_env is not None:
            notes.append(
                f"Loaded `{resolved.name}` in a fresh root environment because it contains `import` headers."
            )
            base_env = None

        payload: dict[str, Any] = {"path": str(resolved), **self._command_options_payload()}
        if base_env is not None:
            payload["env"] = base_env

        raw, rec_notes = self._raw_request_with_recovery(payload)
        notes.extend(rec_notes)
        data = raw.data

        if "message" in data and "env" not in data:
            err = str(data["message"])
            return ExecutionOutcome(
                ok=False,
                mode="file",
                env=self.current_env,
                messages=[{"severity": "error", "data": err}],
                notes=notes,
                error_summary=err,
                raw_response=data,
            )

        has_errors = self._has_error_messages(data)
        raw_messages = list(data.get("messages", []))
        sorries = list(data.get("sorries", []))
        messages = self._filter_messages(raw_messages, sorries)
        tactics = list(data.get("tactics", []))

        if has_errors:
            first_err = next(
                (m.get("data", "File load error") for m in raw_messages if m.get("severity") == "error"),
                "File load error",
            )
            return ExecutionOutcome(
                ok=False,
                mode="file",
                env=self.current_env,
                messages=messages,
                sorries=sorries,
                tactics=tactics,
                notes=notes,
                error_summary=first_err,
                raw_response=data,
            )

        new_env = int(data["env"])
        self._undo_stack.append((self.current_env, self.active_proof_state))
        self.env_nodes[new_env] = EnvNode(
            env_id=new_env,
            parent_env=base_env,
            file_path=str(resolved),
            declared_names=extract_declared_names(file_text),
        )
        self.current_env = new_env
        first_ps = self._register_sorries(sorries, source_env=new_env)
        if first_ps is not None:
            self.active_proof_state = first_ps
        notes.append(f"Loaded `{resolved}` → env #{new_env}.")

        return ExecutionOutcome(
            ok=True,
            mode="file",
            env=new_env,
            proof_state=first_ps,
            messages=messages,
            sorries=sorries,
            tactics=tactics,
            notes=notes,
            raw_response=data,
        )

    def pickle(
        self,
        dest_path: str | Path,
        kind: str = "env",
        state_id: int | None = None,
    ) -> ExecutionOutcome:
        """Pickle an environment or proofState snapshot to ``dest_path`` (.olean)."""
        resolved = Path(dest_path).expanduser()
        if not resolved.is_absolute():
            resolved = (self.repl.working_dir / resolved).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)

        if kind == "proof":
            target = self.active_proof_state if state_id is None else state_id
            if target is None:
                msg = "No active proofState to pickle."
                return ExecutionOutcome(
                    ok=False,
                    mode="pickle",
                    messages=[{"severity": "error", "data": msg}],
                    error_summary=msg,
                )
            payload: dict[str, Any] = {"pickleTo": str(resolved), "proofState": target}
        else:
            target = self.current_env if state_id is None else state_id
            if target is None:
                msg = "No active environment to pickle. Run at least one command first."
                return ExecutionOutcome(
                    ok=False,
                    mode="pickle",
                    messages=[{"severity": "error", "data": msg}],
                    error_summary=msg,
                )
            payload = {"pickleTo": str(resolved), "env": target}

        raw, notes = self._raw_request_with_recovery(payload)
        data = raw.data
        if "message" in data and "env" not in data and "proofState" not in data:
            err = str(data["message"])
            return ExecutionOutcome(
                ok=False,
                mode="pickle",
                messages=[{"severity": "error", "data": err}],
                notes=notes,
                error_summary=err,
                raw_response=data,
            )

        notes.append(f"Pickled {kind} #{target} to `{resolved}`.")
        return ExecutionOutcome(
            ok=True,
            mode="pickle",
            env=self.current_env,
            proof_state=self.active_proof_state,
            notes=notes,
            raw_response=data,
        )

    def unpickle(
        self,
        src_path: str | Path,
        kind: str = "env",
        env_id: int | None = None,
    ) -> ExecutionOutcome:
        """Restore a pickled environment or proofState snapshot from ``src_path``."""
        resolved = Path(src_path).expanduser()
        if not resolved.is_absolute():
            resolved = (self.repl.working_dir / resolved).resolve()
        if not resolved.is_file():
            msg = f"Pickle file not found: {resolved}"
            return ExecutionOutcome(
                ok=False,
                mode="pickle",
                messages=[{"severity": "error", "data": msg}],
                error_summary=msg,
            )

        if kind == "proof":
            payload: dict[str, Any] = {"unpickleProofStateFrom": str(resolved)}
            ctx_env = self.current_env if env_id is None else env_id
            if ctx_env is not None:
                payload["env"] = ctx_env
        else:
            payload = {"unpickleEnvFrom": str(resolved)}

        raw, notes = self._raw_request_with_recovery(payload)
        data = raw.data
        if "message" in data and "env" not in data and "proofState" not in data:
            err = str(data["message"])
            return ExecutionOutcome(
                ok=False,
                mode="pickle",
                messages=[{"severity": "error", "data": err}],
                notes=notes,
                error_summary=err,
                raw_response=data,
            )

        self._undo_stack.append((self.current_env, self.active_proof_state))
        if kind == "proof":
            new_ps = int(data["proofState"])
            goals = list(data.get("goals", []))
            status = str(data.get("proofStatus", ""))
            self.proof_nodes[new_ps] = ProofNode(
                proof_state=new_ps,
                goals=goals,
                source_env=self.current_env,
                proof_status=status,
            )
            self.active_proof_state = new_ps
            notes.append(f"Unpickled proofState #{new_ps} from `{resolved}`.")
            return ExecutionOutcome(
                ok=True,
                mode="tactic",
                env=self.current_env,
                proof_state=new_ps,
                goals=goals,
                proof_status=status,
                notes=notes,
                raw_response=data,
            )

        new_env = int(data["env"])
        self.env_nodes[new_env] = EnvNode(
            env_id=new_env,
            parent_env=None,
            unpickle_path=str(resolved),
        )
        self.current_env = new_env
        notes.append(f"Unpickled environment #{new_env} from `{resolved}`.")
        return ExecutionOutcome(
            ok=True,
            mode="pickle",
            env=new_env,
            notes=notes,
            raw_response=data,
        )

    def execute_raw_json(self, raw_json_text: str) -> ExecutionOutcome:
        """Send raw JSON directly to ``repl`` and return the parsed response."""
        import json

        try:
            payload = json.loads(raw_json_text)
        except json.JSONDecodeError as exc:
            msg = f"Invalid JSON payload: {exc}"
            return ExecutionOutcome(
                ok=False,
                mode="raw",
                messages=[{"severity": "error", "data": msg}],
                error_summary=msg,
            )
        if not isinstance(payload, dict):
            msg = "REPL payload must be a JSON object."
            return ExecutionOutcome(
                ok=False,
                mode="raw",
                messages=[{"severity": "error", "data": msg}],
                error_summary=msg,
            )

        raw, notes = self._raw_request_with_recovery(payload, recover_before=False)
        return ExecutionOutcome(
            ok=True,
            mode="raw",
            env=self.current_env,
            proof_state=self.active_proof_state,
            aux_stdout=raw.aux_stdout,
            notes=notes,
            raw_response=raw.data,
        )

    def inspect_symbol(self, symbol: str, detail_level: int = 0) -> str | None:
        """Run a non-mutating ``#check`` (and ``#print`` at detail_level>=1) for hover inspection."""
        if not re.match(
            r"^[A-Za-z_\u0391-\u03c9\u03ca-\u03fb][A-Za-z0-9_.'\u0391-\u03c9\u03ca-\u03fb]*$",
            symbol,
        ):
            return None

        cmd = f"#check {symbol}" if detail_level == 0 else f"#check {symbol}\n#print {symbol}"
        payload: dict[str, Any] = {"cmd": cmd}
        if self.current_env is not None:
            payload["env"] = self.current_env

        try:
            raw = self.repl.request(payload, timeout=5.0)
        except Exception:
            return None

        if self._has_error_messages(raw.data):
            return None

        infos = [
            m.get("data", "")
            for m in raw.data.get("messages", [])
            if m.get("severity") == "info" and m.get("data")
        ]
        return "\n\n".join(infos) if infos else None

    def undo(self) -> str:
        """Step back to the previous ``(current_env, active_proof_state)`` state."""
        if not self._undo_stack:
            return "Nothing to undo."
        prev_env, prev_ps = self._undo_stack.pop()
        self.current_env = prev_env
        self.active_proof_state = prev_ps
        return f"Reverted to env={prev_env}, proofState={prev_ps}."

    def reset(self, hard: bool = False) -> str:
        """Reset session state (and optionally restart the REPL subprocess)."""
        self.current_env = None
        self.active_proof_state = None
        self._undo_stack.clear()
        self._needs_recovery = False
        if hard:
            self.env_nodes.clear()
            self.proof_nodes.clear()
            self.repl.kill()
            return "Session reset and Lean 4 REPL process restarted."
        return "Reset active environment to root (None). Earlier snapshots remain accessible via `%env <id>`."

    def switch_project(self, project_path: str | Path) -> str:
        """Switch the REPL to run inside a Lake project (``lake env repl``) or directory."""
        resolved = Path(project_path).expanduser().resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"Directory does not exist: {resolved}")

        lake_root = find_lake_project(
            resolved,
            exclude_dirs={self.repl.binary_manager.vendor_dir},
        )
        self.repl.kill()
        self.repl.working_dir = resolved
        self.repl.project_dir = lake_root
        self._lean_version = None
        self.reset(hard=True)
        self.repl.start()

        if lake_root is not None:
            return f"Switched to Lake project `{lake_root}` (`lake env repl`)."
        return f"Switched working directory to `{resolved}` (standalone Lean 4 mode)."

    def close(self) -> None:
        """Terminate the underlying REPL process."""
        self.repl.kill()

