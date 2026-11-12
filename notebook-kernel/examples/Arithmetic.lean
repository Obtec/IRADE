/-
Example Lean 4 module for `%load` in Jupyter notebooks.
All theorems are proven without `sorry` or custom axioms.
-/

/-- Sum of natural numbers from `0` to `n`: $\sum_{k=0}^{n} k$. -/
def gaussSum : Nat → Nat
  | 0 => 0
  | n + 1 => gaussSum n + (n + 1)

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

#print axioms gaussSum_eq

