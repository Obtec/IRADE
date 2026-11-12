# Example Formal Proof Modules (`examples`)

This directory contains standalone Lean 4 and Rocq (Coq) source modules designed for formal verification demonstrations, testing, and `%load` file-mode importing inside Jupyter notebooks.

---

## 1. File Manifest

- **`Arithmetic.lean`**: Formal specification and proof of the closed-form summation formula for natural numbers in Lean 4.
- **`Arithmetic.v`**: Formal specification and inductive proofs of arithmetic properties (addition identity and associativity) in Rocq (Coq).

---

## 2. Lean 4 Module: `Arithmetic.lean`

The module establishes the classical Gauss summation identity:
$$2 \sum_{k=0}^{n} k = n(n + 1)$$

### 2.1 Definitions
```lean
/-- Sum of natural numbers from `0` to `n`: $\sum_{k=0}^{n} k$. -/
def gaussSum : Nat → Nat
  | 0 => 0
  | n + 1 => gaussSum n + (n + 1)
```

### 2.2 Formal Proof
```lean
/-- Closed-form identity: $2 \sum_{k=0}^{n} k = n(n + 1)$. -/
theorem gaussSum_eq (n : Nat) : 2 * gaussSum n = n * (n + 1) := by
  induction n with
  | zero => rfl
  | succ k ih =>
    calc 2 * gaussSum (k + 1)
      _ = 2 * gaussSum k + 2 * (k + 1) := by simp [gaussSum, Nat.mul_add]
      _ = k * (k + 1) + 2 * (k + 1) := by rw [ih]
      _ = (k + 2) * (k + 1) := by rw [← Nat.add_mul]
      _ = (k + 1) * (k + 2) := by rw [Nat.mul_comm]
```

### 2.3 Foundational Axiom Verification
The theorem concludes with:
```lean
#print axioms gaussSum_eq
```
This confirms that the proof depends exclusively on standard Lean 4 core axioms and contains **no custom axioms or unresolved `sorryAx` references**.

### 2.4 Loading in Lean 4 Notebooks
```lean
%load examples/Arithmetic.lean
#eval gaussSum 100
```

---

## 3. Rocq (Coq) Module: `Arithmetic.v`

The module proves inductive properties of natural number addition without `Admitted`:

### 3.1 Definitions & Proofs
```coq
Definition double (n : nat) : nat := n + n.

Lemma add_0_r : forall n : nat, n + 0 = n.
Proof.
  intros n.
  induction n as [| n' IHn].
  - simpl. reflexivity.
  - simpl. rewrite IHn. reflexivity.
Qed.

Theorem add_assoc : forall n m p : nat, n + (m + p) = (n + m) + p.
Proof.
  intros n m p.
  induction n as [| n' IHn].
  - simpl. reflexivity.
  - simpl. rewrite IHn. reflexivity.
Qed.
```

### 3.2 Loading in Rocq Notebooks
```coq
%load examples/Arithmetic.v
Check add_assoc.
```
