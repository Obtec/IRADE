"""Integration tests for LeanSession, magic commands, completion, and Lean4Kernel."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from lean4_kernel.infoview import extract_infoview_payload
from lean4_kernel.kernel import Lean4Kernel
from lean4_kernel.magics import dispatch_magic
from lean4_kernel.renderer import render_outcome
from lean4_kernel.session import LeanSession


def _has_runnable_lean() -> bool:
    if shutil.which("lean") is None:
        return False
    try:
        proc = subprocess.run(
            ["lean", "--print-libdir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except OSError:
        return False


has_lean = _has_runnable_lean()


def test_unicode_and_keyword_completion() -> None:
    kernel = Lean4Kernel()
    try:
        # Unique Unicode completion: `\forall` -> `∀`
        res = kernel.do_complete("theorem t : \\for", len("theorem t : \\for"))
        assert res["matches"] == ["∀"]
        assert res["cursor_start"] == len("theorem t : ")

        # Exact match comes first (`\le` -> `≤` first, before `\leftarrow` -> `←`)
        res_le = kernel.do_complete("x \\le", 5)
        assert res_le["matches"][0] == "≤"
        assert kernel.do_complete("x \\leq", 6)["matches"] == ["≤"]

        # Bullet completion: `\.` -> `·`
        res_dot = kernel.do_complete("  \\.", 4)
        assert "·" in res_dot["matches"]

        # Magic completion
        res_magic = kernel.do_complete("%%tac", 5)
        assert res_magic["matches"] == ["%%tactic"]
    finally:
        kernel.lean_session.close()


def test_is_complete_checks() -> None:
    kernel = object.__new__(Lean4Kernel)
    assert kernel.do_is_complete("def f := 2")["status"] == "complete"
    assert kernel.do_is_complete("theorem t : 1 = 1 := by")["status"] == "incomplete"
    assert kernel.do_is_complete("theorem t : 1 = 1 := by\n  rfl\n\n")["status"] == "complete"
    assert kernel.do_is_complete("/- unclosed comment")["status"] == "incomplete"
    assert kernel.do_is_complete("def f (x : Nat := 1")["status"] == "incomplete"
    assert kernel.do_is_complete("def f )")["status"] == "invalid"


@pytest.mark.skipif(not has_lean, reason="Requires runnable Lean 4 toolchain")
def test_live_session_commands_tactics_backtrack_pickle_and_recovery(tmp_path: Path) -> None:
    session = LeanSession()
    try:
        # 1. Define a function & verify `#eval`
        out1 = session.execute_command("def sq (n : Nat) : Nat := n * n")
        assert out1.ok
        assert out1.env == 0

        out2 = session.execute_command("#eval sq 9")
        assert out2.ok
        assert out2.messages[0]["data"] == "81"
        env_after_eval = out2.env
        assert env_after_eval is not None

        # 2. Error rollback: broken declaration must NOT advance current_env
        out_err = session.execute_command('def broken : Nat := "not a nat"')
        assert not out_err.ok
        assert session.current_env == env_after_eval

        # 3. Redeclaration auto-backtrack: redefining `sq` branches cleanly!
        out_redef = session.execute_command("def sq (n : Nat) : Nat := n * n + 1")
        assert out_redef.ok
        assert any("Redefined `sq`" in n for n in out_redef.notes)

        out_check_redef = session.execute_command("#eval sq 9")
        assert out_check_redef.ok
        assert out_check_redef.messages[0]["data"] == "82"

        # 4. Hover inspection (`#check`) without mutating current_env
        env_before_inspect = session.current_env
        sig = session.inspect_symbol("sq")
        assert sig is not None and "Nat" in sig
        assert session.current_env == env_before_inspect

        # 5. Interactive Tactic Mode (including multi-line tacticSeq!)
        out_sorry = session.execute_command(
            "theorem imp_trans_demo (P Q R : Prop) : (P → Q) → (Q → R) → P → R := by sorry"
        )
        assert out_sorry.ok
        assert session.active_proof_state is not None

        m_tac1 = dispatch_magic(session, "%%tactic\nintro hpq\nintro hqr")
        assert m_tac1.ok
        assert m_tac1.mime_bundle is not None
        assert "⊢ P → R" in m_tac1.mime_bundle["text/plain"]

        m_tac2 = dispatch_magic(session, "%tactic intro hp")
        assert m_tac2.ok

        m_tac3 = dispatch_magic(session, "%tactic exact hqr (hpq hp)")
        assert m_tac3.ok
        assert m_tac3.mime_bundle is not None
        assert "Goals accomplished!" in m_tac3.mime_bundle["text/plain"]

        # 6. Pickle & unpickle environment round-trip
        olean_file = tmp_path / "checkpoint.olean"
        m_pick = dispatch_magic(session, f"%pickle {olean_file}")
        assert m_pick.ok and olean_file.is_file()

        dispatch_magic(session, "%reset --hard")
        assert session.current_env is None

        m_unpick = dispatch_magic(session, f"%unpickle {olean_file}")
        assert m_unpick.ok
        out_after_unpick = session.execute_command("example : sq 5 = 26 := rfl\n#reduce sq 5")
        assert out_after_unpick.ok
        assert out_after_unpick.messages[0]["data"] == "26"

        # 7. File load mode (`%load`)
        lean_file = tmp_path / "SampleHelper.lean"
        lean_file.write_text(
            "def cube (n : Nat) : Nat := n * n * n\n"
            "theorem cube_two : cube 2 = 8 := rfl\n",
            encoding="utf-8",
        )
        m_load = dispatch_magic(session, f"%load {lean_file}")
        assert m_load.ok
        out_cube = session.execute_command("#eval cube 3")
        assert out_cube.ok and out_cube.messages[0]["data"] == "27"

        # 8. Crash/interrupt automatic lineage recovery
        session.repl.kill()
        assert not session.repl.is_alive
        out_recovered = session.execute_command("#eval cube 4")
        assert out_recovered.ok
        assert out_recovered.messages[0]["data"] == "64"
        assert any("Recovered Lean environment" in n for n in out_recovered.notes)

        # 9. Interactive InfoView extraction + rendering (Method 1 + Editor Cursor Sync Hybrid)
        proof_code = (
            "theorem currying_demo (P Q R : Prop) : (P ∧ Q → R) ↔ (P → Q → R) := by\n"
            "  constructor\n"
            "  · intro h hp hq\n"
            "    exact h ⟨hp, hq⟩\n"
            "  · intro h hpq\n"
            "    exact h hpq.1 hpq.2"
        )
        out_proof = session.execute_command(proof_code)
        assert out_proof.ok
        assert out_proof.infotree is not None

        iv_payload = extract_infoview_payload(
            proof_code,
            ok=out_proof.ok,
            env=out_proof.env,
            proof_state=out_proof.proof_state,
            infotree=out_proof.infotree,
        )
        assert len(iv_payload.tactic_steps) >= 5
        assert any( len(s.goals_after) == 2 for s in iv_payload.tactic_steps )
        assert len(iv_payload.term_items) > 0

        bundle = render_outcome(out_proof)
        assert bundle is not None
        assert "l4iv-wrap" in bundle["text/html"]
        assert "bindParentEditorCursor" in bundle["text/html"]

        # Test `%infoview` magic on the last cell and mode switching
        m_iv = dispatch_magic(session, "%infoview")
        assert m_iv.ok and m_iv.mime_bundle is not None
        assert "l4iv-wrap" in m_iv.mime_bundle["text/html"]

        assert dispatch_magic(session, "%infoview on").ok
        assert session.config.infoview == "on"
        assert dispatch_magic(session, "%infoview auto").ok
        assert session.config.infoview == "auto"
    finally:
        session.close()
