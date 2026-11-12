"""Live integration tests for ``RocqSession`` and ``RocqKernel`` against ``coqidetop``."""

from __future__ import annotations

from rocq_kernel.driver import RocqBinaryNotFoundError, find_coqidetop_binary
from rocq_kernel.kernel import RocqKernel
from rocq_kernel.session import RocqSession
import pytest


def _has_coqidetop() -> bool:
    try:
        find_coqidetop_binary()
        return True
    except RocqBinaryNotFoundError:
        return False


has_rocq = _has_coqidetop()


def test_rocq_unicode_and_keyword_completion() -> None:
    kernel = RocqKernel.__new__(RocqKernel)
    kernel.rocq_session = RocqSession()

    res_uni = kernel.do_complete(r"Lemma t : \forall", len(r"Lemma t : \forall"))
    assert res_uni["status"] == "ok"
    assert "∀" in res_uni["matches"]

    res_kw = kernel.do_complete("induc", len("induc"))
    assert "induction" in res_kw["matches"]


@pytest.mark.skipif(not has_rocq, reason="Requires runnable coqidetop")
def test_live_rocq_session_definitions_proofs_rollback_and_backtrack() -> None:
    session = RocqSession()
    try:
        # 1. Definition
        out_def = session.execute_command("Definition plus2 (n : nat) : nat := n + 2.", cell_id="cell-def")
        assert out_def.ok is True
        assert "plus2" in session.user_identifiers()

        # 2. Non-mutating symbol inspection
        sig = session.inspect_symbol("plus2")
        assert sig is not None
        assert "nat" in sig

        # 3. Inductive proof step-by-step
        out_lem = session.execute_command(
            "Lemma plus_O_n : forall n : nat, 0 + n = n.\nProof.\n  intros n.",
            cell_id="cell-proof-1",
        )
        assert out_lem.ok is True
        assert out_lem.goals is not None
        assert len(out_lem.goals.focused) == 1
        assert "0 + n = n" in out_lem.goals.focused[0].conclusion
        state_after_intros = session.current_state_id

        # 4. Invalid tactic rolls back cleanly to state_after_intros
        out_bad = session.execute_command("exact 0.", cell_id="cell-bad")
        assert out_bad.ok is False
        assert session.current_state_id == state_after_intros

        # 5. Complete proof from preserved state
        out_qed = session.execute_command("simpl. reflexivity. Qed.", cell_id="cell-proof-2")
        assert out_qed.ok is True
        assert out_qed.steps[-1].badge == "✓"

        # 6. Auto-backtrack when re-evaluating earlier cell or redefining plus2
        out_redef = session.execute_command("Definition plus2 (n : nat) : nat := S (S n).", cell_id="cell-def")
        assert out_redef.ok is True
        assert out_redef.backtracked_from is not None

        # 7. Load external .v file
        out_load = session.load_file("examples/Arithmetic.v", fresh=True)
        assert out_load.ok is True
        assert "add_assoc" in session.user_identifiers()
    finally:
        session.close()
