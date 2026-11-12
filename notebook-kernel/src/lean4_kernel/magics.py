"""Line (`%...`) and cell (`%%...`) magic commands for the Lean 4 Jupyter kernel."""

from __future__ import annotations

from dataclasses import dataclass
import shlex

from .renderer import render_outcome, render_text_card
from .session import ExecutionOutcome, LeanSession

MAGIC_NAMES: tuple[str, ...] = (
    "%help",
    "%lsmagic",
    "%env",
    "%history",
    "%infoview",
    "%goals",
    "%tactic",
    "%%tactic",
    "%undo",
    "%reset",
    "%load",
    "%pickle",
    "%unpickle",
    "%project",
    "%config",
    "%repl_json",
    "%%repl_json",
)

HELP_TEXT = """\
Lean 4 Jupyter Kernel Magic Commands (powered by leanprover-community/repl):

  %help, %lsmagic                  Show this help message
  %env                             Show active env, proofState, Lean version & project info
  %env <id | none>                 Switch/backtrack current_env to snapshot <id> (or root)
  %history                         Show recorded environment & proofState snapshots
  %infoview [auto | on | off]      Open Interactive InfoView for last cell or set InfoView mode
  %goals [<id>]                    Show open goals (and optionally select proofState <id>)
  %tactic [<id>] <tactic>          Run a tactic against active proofState (or <id>)
  %%tactic [<id>]                  Cell magic: run multi-line tactic script in tactic mode
  %undo                            Step back to previous (env, proofState)
  %reset [--hard]                  Reset to empty root env (--hard restarts REPL process)
  %load [--fresh] <file.lean>      Load a .lean file via REPL File Mode
  %pickle <file.olean> [--proof]   Save current env (or proofState with --proof) to disk
  %unpickle <file.olean> [--proof] Restore pickled env (or proofState with --proof)
  %project [<dir>]                 Show or switch Lake project directory (`lake env repl`)
  %config [<key> [<val>]]          View or set kernel options:
                                     all_tactics, root_goals, auto_backtrack,
                                     show_sorry_warnings (true/false),
                                     infotree (none/full/tactics/original/substantive),
                                     timeout (<seconds>/none)
  %%repl_json / %repl_json <json>  Send raw JSON payload directly to `repl`
""".rstrip()


@dataclass
class MagicResult:
    """Result returned by magic command execution."""

    ok: bool
    mime_bundle: dict[str, str] | None
    error_summary: str | None = None


def is_magic_cell(code: str) -> bool:
    """Return True if ``code`` starts with a ``%`` or ``%%`` magic directive."""
    stripped = code.lstrip()
    return stripped.startswith("%")


def _parse_bool(val: str) -> bool:
    low = val.strip().lower()
    if low in {"1", "true", "on", "yes"}:
        return True
    if low in {"0", "false", "off", "no"}:
        return False
    raise ValueError(f"Expected boolean (true/false/on/off), got: {val!r}")


def _outcome_to_magic_result(outcome: ExecutionOutcome) -> MagicResult:
    return MagicResult(
        ok=outcome.ok,
        mime_bundle=render_outcome(outcome),
        error_summary=outcome.error_summary,
    )


def dispatch_magic(session: LeanSession, code: str) -> MagicResult:
    """Parse and execute a ``%`` line magic or ``%%`` cell magic."""
    stripped = code.lstrip()
    first_nl = stripped.find("\n")
    if first_nl == -1:
        header_line = stripped
        body = ""
    else:
        header_line = stripped[:first_nl].strip()
        body = stripped[first_nl + 1 :]

    is_cell = header_line.startswith("%%")
    parts = header_line.split(None, 1)
    cmd_token = parts[0]
    arg_str = parts[1].strip() if len(parts) > 1 else ""

    if is_cell:
        return _handle_cell_magic(session, cmd_token, arg_str, body)

    # If a line magic is followed by remaining Lean lines (only for %env/%config? No: keep clean)
    return _handle_line_magic(session, cmd_token, arg_str, body)


def _handle_cell_magic(
    session: LeanSession,
    cmd_token: str,
    arg_str: str,
    body: str,
) -> MagicResult:
    if cmd_token == "%%tactic":
        ps_id: int | None = None
        if arg_str:
            if not arg_str.isdigit():
                msg = f"Usage: `%%tactic [<proofState_id>]` (got {arg_str!r})"
                return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
            ps_id = int(arg_str)
        outcome = session.execute_tactic(body, proof_state=ps_id)
        return _outcome_to_magic_result(outcome)

    if cmd_token == "%%repl_json":
        outcome = session.execute_raw_json(body)
        return _outcome_to_magic_result(outcome)

    msg = f"Unknown cell magic `{cmd_token}`. Run `%help` to list available magics."
    return MagicResult(False, render_text_card("magic error", msg, "err"), msg)


def _handle_line_magic(
    session: LeanSession,
    cmd_token: str,
    arg_str: str,
    body: str,
) -> MagicResult:
    if cmd_token in {"%help", "%lsmagic"}:
        return MagicResult(True, render_text_card("Lean 4 Kernel Help", HELP_TEXT, "info"))

    if cmd_token == "%env":
        if not arg_str:
            proj = str(session.repl.project_dir) if session.repl.project_dir else "(standalone)"
            repl_bin = str(session.repl.repl_bin or session.repl.binary_manager.resolve_binary(auto_build=False))
            info = (
                f"Lean version      : {session.lean_version}\n"
                f"Current env       : {session.current_env}\n"
                f"Active proofState : {session.active_proof_state}\n"
                f"Recorded envs     : {sorted(session.env_nodes.keys())}\n"
                f"Recorded proofs   : {sorted(session.proof_nodes.keys())}\n"
                f"Lake project      : {proj}\n"
                f"Working dir       : {session.repl.working_dir}\n"
                f"REPL binary       : {repl_bin}"
            )
            return MagicResult(True, render_text_card("Session Status", info, "info"))

        target = arg_str.lower()
        if target in {"none", "null", "root"}:
            session._undo_stack.append((session.current_env, session.active_proof_state))
            session.current_env = None
            return MagicResult(
                True,
                render_text_card("Environment", "Switched current_env to root (None).", "ok"),
            )
        if arg_str.isdigit():
            env_id = int(arg_str)
            if env_id not in session.env_nodes:
                msg = f"Unknown environment #{env_id}. Known environments: {sorted(session.env_nodes.keys())}"
                return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
            session._undo_stack.append((session.current_env, session.active_proof_state))
            session.current_env = env_id
            return MagicResult(
                True,
                render_text_card("Environment", f"Switched current_env to #{env_id}.", "ok"),
            )
        msg = f"Usage: `%env` or `%env <id | none>` (got {arg_str!r})"
        return MagicResult(False, render_text_card("magic error", msg, "err"), msg)

    if cmd_token == "%history":
        lines: list[str] = []
        if not session.env_nodes and not session.proof_nodes:
            lines.append("(No snapshots recorded yet.)")
        if session.env_nodes:
            lines.append("Environment Snapshots:")
            for eid in sorted(session.env_nodes):
                node = session.env_nodes[eid]
                marker = "  * " if eid == session.current_env else "    "
                parent = f"#{node.parent_env}" if node.parent_env is not None else "root"
                if node.file_path:
                    desc = f"file: {node.file_path}"
                elif node.unpickle_path:
                    desc = f"unpickle: {node.unpickle_path}"
                else:
                    first_line = (node.cmd or "").strip().splitlines()[0] if node.cmd else ""
                    if len(first_line) > 64:
                        first_line = first_line[:61] + "..."
                    desc = first_line
                lines.append(f"{marker}env #{eid} (from {parent}): {desc}")
        if session.proof_nodes:
            lines.append("\nProof State Snapshots:")
            for pid in sorted(session.proof_nodes):
                pnode = session.proof_nodes[pid]
                marker = "  * " if pid == session.active_proof_state else "    "
                parent_ps = f"#{pnode.parent_proof_state}" if pnode.parent_proof_state is not None else f"env #{pnode.source_env}"
                tac_desc = pnode.tactic or "(sorry)"
                if "\n" in tac_desc:
                    tac_desc = tac_desc.splitlines()[0] + " ..."
                lines.append(
                    f"{marker}proofState #{pid} (from {parent_ps}) [{len(pnode.goals)} goal(s), {pnode.proof_status}]: {tac_desc}"
                )
        return MagicResult(True, render_text_card("Snapshot History", "\n".join(lines), "info"))

    if cmd_token == "%infoview":
        if not arg_str:
            if session.last_outcome is not None and session.last_outcome.source_code.strip():
                forced = ExecutionOutcome(
                    ok=session.last_outcome.ok,
                    mode="command",
                    env=session.last_outcome.env,
                    proof_state=session.last_outcome.proof_state,
                    messages=session.last_outcome.messages,
                    sorries=session.last_outcome.sorries,
                    tactics=session.last_outcome.tactics,
                    infotree=session.last_outcome.infotree,
                    show_raw_infotree=False,
                    infoview_mode="on",
                    source_code=session.last_outcome.source_code,
                    notes=session.last_outcome.notes,
                )
                return MagicResult(True, render_outcome(forced))
            return MagicResult(
                True,
                render_text_card(
                    "InfoView",
                    f"InfoView mode is `{session.config.infoview}` (auto | on | off). "
                    "Execute a Lean cell first to inspect its InfoTree.",
                    "info",
                ),
            )
        mode_val = arg_str.strip().lower()
        if mode_val in {"auto", "on", "off"}:
            session.config.infoview = mode_val
            return MagicResult(
                True,
                render_text_card("InfoView", f"InfoView mode set to `{mode_val}`.", "ok"),
            )
        msg = f"Usage: `%infoview [auto | on | off]` (got {arg_str!r})"
        return MagicResult(False, render_text_card("magic error", msg, "err"), msg)

    if cmd_token == "%goals":
        target_ps = session.active_proof_state
        if arg_str:
            if not arg_str.isdigit():
                msg = f"Usage: `%goals [<proofState_id>]` (got {arg_str!r})"
                return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
            target_ps = int(arg_str)
            if target_ps not in session.proof_nodes:
                msg = f"Unknown proofState #{target_ps}. Known: {sorted(session.proof_nodes.keys())}"
                return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
            session.active_proof_state = target_ps

        if target_ps is None or target_ps not in session.proof_nodes:
            return MagicResult(
                True,
                render_text_card("Goals", "No active open proofState.", "info"),
            )
        pnode = session.proof_nodes[target_ps]
        outcome = ExecutionOutcome(
            ok=True,
            mode="tactic",
            env=session.current_env,
            proof_state=target_ps,
            goals=list(pnode.goals),
            proof_status=pnode.proof_status,
        )
        return _outcome_to_magic_result(outcome)

    if cmd_token == "%tactic":
        full_arg = f"{arg_str}\n{body}".strip() if body.strip() else arg_str
        if not full_arg:
            msg = "Usage: `%tactic [<proofState_id>] <tactic>`"
            return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
        ps_id: int | None = None
        first_parts = full_arg.split(None, 1)
        if len(first_parts) == 2 and first_parts[0].isdigit():
            ps_id = int(first_parts[0])
            tac_text = first_parts[1]
        else:
            tac_text = full_arg
        outcome = session.execute_tactic(tac_text, proof_state=ps_id)
        return _outcome_to_magic_result(outcome)

    if cmd_token == "%undo":
        msg = session.undo()
        return MagicResult(True, render_text_card("Undo", msg, "ok"))

    if cmd_token == "%reset":
        hard = arg_str.strip() in {"--hard", "-f", "hard"}
        msg = session.reset(hard=hard)
        return MagicResult(True, render_text_card("Reset", msg, "ok"))

    if cmd_token == "%load":
        tokens = shlex.split(arg_str)
        fresh = False
        paths: list[str] = []
        for tok in tokens:
            if tok == "--fresh":
                fresh = True
            else:
                paths.append(tok)
        if len(paths) != 1:
            msg = "Usage: `%load [--fresh] <path/to/file.lean>`"
            return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
        outcome = session.execute_file(paths[0], use_current_env=not fresh)
        return _outcome_to_magic_result(outcome)

    if cmd_token == "%pickle":
        tokens = shlex.split(arg_str)
        if not tokens:
            msg = "Usage: `%pickle <file.olean> [--proof [<id>] | --env [<id>]]`"
            return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
        dest = tokens[0]
        kind = "env"
        state_id: int | None = None
        idx = 1
        while idx < len(tokens):
            t = tokens[idx]
            if t == "--proof":
                kind = "proof"
                if idx + 1 < len(tokens) and tokens[idx + 1].isdigit():
                    state_id = int(tokens[idx + 1])
                    idx += 1
            elif t == "--env":
                kind = "env"
                if idx + 1 < len(tokens) and tokens[idx + 1].isdigit():
                    state_id = int(tokens[idx + 1])
                    idx += 1
            elif t.isdigit():
                state_id = int(t)
            idx += 1
        outcome = session.pickle(dest, kind=kind, state_id=state_id)
        return _outcome_to_magic_result(outcome)

    if cmd_token == "%unpickle":
        tokens = shlex.split(arg_str)
        if not tokens:
            msg = "Usage: `%unpickle <file.olean> [--proof [--env <id>]]`"
            return MagicResult(False, render_text_card("magic error", msg, "err"), msg)
        src = tokens[0]
        kind = "env"
        env_id: int | None = None
        idx = 1
        while idx < len(tokens):
            t = tokens[idx]
            if t == "--proof":
                kind = "proof"
            elif t == "--env" and idx + 1 < len(tokens) and tokens[idx + 1].isdigit():
                env_id = int(tokens[idx + 1])
                idx += 1
            idx += 1
        outcome = session.unpickle(src, kind=kind, env_id=env_id)
        return _outcome_to_magic_result(outcome)

    if cmd_token == "%project":
        if not arg_str:
            proj = str(session.repl.project_dir) if session.repl.project_dir else "(standalone)"
            body_txt = f"Lake project : {proj}\nWorking dir  : {session.repl.working_dir}"
            return MagicResult(True, render_text_card("Project Context", body_txt, "info"))
        tokens = shlex.split(arg_str)
        target_dir = tokens[0] if tokens else arg_str
        try:
            msg = session.switch_project(target_dir)
            return MagicResult(True, render_text_card("Project Switched", msg, "ok"))
        except Exception as exc:
            err = str(exc)
            return MagicResult(False, render_text_card("Project Error", err, "err"), err)

    if cmd_token == "%config":
        cfg = session.config
        if not arg_str:
            txt = (
                f"infoview            = {cfg.infoview}\n"
                f"all_tactics         = {cfg.all_tactics}\n"
                f"root_goals          = {cfg.root_goals}\n"
                f"infotree            = {cfg.infotree}\n"
                f"auto_backtrack      = {cfg.auto_backtrack}\n"
                f"show_sorry_warnings = {cfg.show_sorry_warnings}\n"
                f"timeout             = {cfg.timeout}"
            )
            return MagicResult(True, render_text_card("Kernel Configuration", txt, "info"))
        tokens = shlex.split(arg_str)
        key = tokens[0]
        if len(tokens) == 1:
            if not hasattr(cfg, key):
                msg = f"Unknown config key `{key}`."
                return MagicResult(False, render_text_card("Config Error", msg, "err"), msg)
            return MagicResult(
                True,
                render_text_card("Kernel Configuration", f"{key} = {getattr(cfg, key)}", "info"),
            )
        val_str = tokens[1]
        try:
            if key in {"all_tactics", "root_goals", "auto_backtrack", "show_sorry_warnings"}:
                setattr(cfg, key, _parse_bool(val_str))
            elif key == "infoview":
                low_iv = val_str.strip().lower()
                if low_iv in {"on", "true", "full", "always"}:
                    cfg.infoview = "on"
                elif low_iv in {"off", "false", "none"}:
                    cfg.infoview = "off"
                elif low_iv == "auto":
                    cfg.infoview = "auto"
                else:
                    raise ValueError("infoview must be one of: auto, on, off")
            elif key == "infotree":
                if val_str.lower() in {"none", "null", "off", "false"}:
                    cfg.infotree = None
                elif val_str in {"full", "tactics", "original", "substantive"}:
                    cfg.infotree = val_str
                else:
                    raise ValueError(
                        "infotree must be one of: none, full, tactics, original, substantive"
                    )
            elif key == "timeout":
                if val_str.lower() in {"none", "null", "off", "0"}:
                    cfg.timeout = None
                else:
                    t_val = float(val_str)
                    if t_val <= 0:
                        raise ValueError("timeout must be positive or `none`")
                    cfg.timeout = t_val
            else:
                raise KeyError(f"Unknown config key `{key}`.")
        except Exception as exc:
            err = str(exc)
            return MagicResult(False, render_text_card("Config Error", err, "err"), err)

        return MagicResult(
            True,
            render_text_card("Kernel Configuration", f"{key} = {getattr(cfg, key)}", "ok"),
        )

    if cmd_token == "%repl_json":
        payload_txt = f"{arg_str}\n{body}".strip()
        outcome = session.execute_raw_json(payload_txt)
        return _outcome_to_magic_result(outcome)

    msg = f"Unknown magic command `{cmd_token}`. Run `%help` to list available magics."
    return MagicResult(False, render_text_card("magic error", msg, "err"), msg)

