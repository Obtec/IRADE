"""End-to-end Jupyter protocol test using nbclient and the installed `lean4` kernel."""

from __future__ import annotations

import shutil
import subprocess

from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient
import nbformat
import pytest


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


def _has_lean4_kernelspec() -> bool:
    try:
        return "lean4" in KernelSpecManager().find_kernel_specs()
    except OSError:
        return False


has_lean = _has_runnable_lean()
has_kernel = _has_lean4_kernelspec()


@pytest.mark.skipif(
    not (has_lean and has_kernel),
    reason="Requires Lean 4 and installed 'lean4' kernelspec",
)
def test_execute_notebook_end_to_end() -> None:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "display_name": "Lean 4",
        "language": "lean4",
        "name": "lean4",
    }
    nb.cells = [
        nbformat.v4.new_code_cell(
            "def sumTo : Nat → Nat\n"
            "  | 0 => 0\n"
            "  | n + 1 => sumTo n + (n + 1)\n\n"
            "#eval sumTo 10"
        ),
        nbformat.v4.new_code_cell(
            "theorem sumTo_formula (n : Nat) : 2 * sumTo n = n * (n + 1) := by\n"
            "  induction n with\n"
            "  | zero => rfl\n"
            "  | succ k ih =>\n"
            "    calc 2 * sumTo (k + 1)\n"
            "      _ = 2 * sumTo k + 2 * (k + 1) := by simp [sumTo, Nat.mul_add]\n"
            "      _ = k * (k + 1) + 2 * (k + 1) := by rw [ih]\n"
            "      _ = (k + 2) * (k + 1) := by rw [← Nat.add_mul]\n"
            "      _ = (k + 1) * (k + 2) := by rw [Nat.mul_comm]\n\n"
            "#print axioms sumTo_formula"
        ),
        nbformat.v4.new_code_cell("%env"),
    ]

    client = NotebookClient(nb, timeout=60, kernel_name="lean4")
    executed = client.execute()

    cell0_plain = executed.cells[0].outputs[0]["data"]["text/plain"]
    assert cell0_plain.strip() == "55"

    cell1_plain = executed.cells[1].outputs[0]["data"]["text/plain"]
    assert "sumTo_formula" in cell1_plain
    assert "sorryAx" not in cell1_plain

    cell2_plain = executed.cells[2].outputs[0]["data"]["text/plain"]
    assert "Current env" in cell2_plain
