# Interactive Formal Verification Notebooks (`notebooks`)

This directory hosts pre-executed, production-quality Jupyter notebooks demonstrating the interactive capabilities of the Lean 4 and Rocq (Coq) Jupyter Kernels.

---

## 1. File Manifest

- **`lean4_demo.ipynb`**: Complete feature demonstration notebook for Lean 4 showcasing Command Mode, Induction Proofs, Interactive InfoView, Multi-line Tactic Mode (`%%tactic`), and Module Loading (`%load`).
- **`rocq_demo.ipynb`**: Complete feature demonstration notebook for Rocq (Coq) showcasing Vernacular definitions, Inductive Proofs with Split-Pane InfoViews, STM Auto-Backtracking, and Module Loading (`%load`).

---

## 2. Lean 4 Notebook Walkthrough (`lean4_demo.ipynb`)

| Section | Topic | Key Lean 4 Commands & Kernel Magics |
| :--- | :--- | :--- |
| **Section 1** | Basic Declarations & Computation | `def fib`, structural recursion, `#eval`, `#check` |
| **Section 2** | Formal Proof & Axiom Check | Inductive proof `fib_pos_succ`, `#print axioms` |
| **Section 3** | Interactive Tactic Mode (`%%tactic`) | Theorem declaration with `by sorry`, open goal card display, and step-by-step resolution via `%%tactic` and `·` bullets |
| **Section 4** | File Loading & State Inspection | `%load ../examples/Arithmetic.lean`, evaluating Gauss sum, and `%history` inspection |

---

## 3. Rocq (Coq) Notebook Walkthrough (`rocq_demo.ipynb`)

| Section | Topic | Key Rocq Commands & Kernel Magics |
| :--- | :--- | :--- |
| **Section 1** | Basic Definitions & Type Checking | `Definition double`, `Check double`, `Compute (double 21)` |
| **Section 2** | Inductive Proofs & Split-Pane InfoView | `Lemma add_0_r`, `induction n as [\| n' IHn]`, bullets `-`, `rewrite IHn`, and interactive turnstile `⊢` goal rendering |
| **Section 3** | STM State Rollback & Auto-Backtracking | Redefining `Definition double` without collision, branching ancestor STM states cleanly |
| **Section 4** | External `.v` Module Loading | `%load examples/Arithmetic.v` importing arithmetic properties |

---

## 4. Launching & Viewing

### Option A: Via the Web Proof Studio (Recommended)
```bash
.venv/bin/lean4-kernel serve --host 127.0.0.1 --port 8890
```
Open your browser at `http://127.0.0.1:8890` and import or select notebooks.

### Option B: Via Standard JupyterLab / Classic Notebook
```bash
.venv/bin/jupyter lab notebooks/
```

### Option C: In Visual Studio Code
Open either notebook in VS Code and select the corresponding **Lean 4** or **Rocq (Coq)** kernel from the top-right kernel selector.
