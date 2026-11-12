(*
  Example Rocq (Coq) module for `%load` in Jupyter notebooks.
  All lemmas and theorems are proven without `Admitted` or custom axioms.
*)

Definition double (n : nat) : nat := n + n.

Fixpoint gauss_sum (n : nat) : nat :=
  match n with
  | 0 => 0
  | S k => gauss_sum k + S k
  end.

Lemma plus_O_n : forall n : nat, 0 + n = n.
Proof.
  intros n.
  simpl.
  reflexivity.
Qed.

Lemma add_0_r : forall n : nat, n + 0 = n.
Proof.
  intros n.
  induction n as [| n' IHn].
  - simpl.
    reflexivity.
  - simpl.
    rewrite IHn.
    reflexivity.
Qed.

Theorem add_assoc : forall n m p : nat, n + (m + p) = (n + m) + p.
Proof.
  intros n m p.
  induction n as [| n' IHn].
  - simpl.
    reflexivity.
  - simpl.
    rewrite IHn.
    reflexivity.
Qed.

Check add_assoc.
