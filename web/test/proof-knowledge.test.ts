import test from 'node:test';
import assert from 'node:assert/strict';
import {
  extractClientProofRelations,
  normalizeLeanCodeForExecution,
  renderProofDbInspectorHtml,
  renderProofDbModalHtml,
  type ProofKnowledgeSnapshot,
} from '../src/studio/proofKnowledge.ts';

test('normalizeLeanCodeForExecution rewrites top-level lemma to theorem without changing line count', () => {
  const input = [
    '-- header',
    'def idNat (n : Nat) : Nat := n',
    'lemma add_zero_lemma (n : Nat) : n + 0 = n := by',
    '  rfl',
    'private lemma helper (x : Nat) : x = x := rfl',
    'theorem add_zero_identity (n : Nat) : idNat n + 0 = n := by',
    '  simpa [idNat] using add_zero_lemma n',
  ].join('\n');

  const normalized = normalizeLeanCodeForExecution(input);
  assert.equal(normalized.split('\n').length, input.split('\n').length);
  assert.match(normalized, /^theorem add_zero_lemma/m);
  assert.match(normalized, /^private theorem helper/m);
  assert.match(normalized, /^theorem add_zero_identity/m);
});

test('extractClientProofRelations extracts definitions, lemmas, theorems, and usage roles for Lean 4', () => {
  const leanSource = `
    -- comment referencing unusedDef
    def idNat (n : Nat) : Nat := n

    lemma add_zero_lemma (n : Nat) : n + 0 = n := by
      rfl

    theorem add_zero_identity (n : Nat) : idNat n + 0 = n := by
      simpa [idNat] using add_zero_lemma n
  `;

  const parsed = extractClientProofRelations('LEAN4', leanSource);
  assert.deepEqual(parsed.definitions.map(d => d.name), ['idNat']);
  assert.deepEqual(parsed.theorems.map(t => `${t.kind}:${t.name}`), [
    'LEMMA:add_zero_lemma',
    'THEOREM:add_zero_identity',
  ]);
  assert.deepEqual(
    parsed.lemmaRelations.map(r => ({
      theoremName: r.theoremName,
      lemmaName: r.lemmaName,
      relationType: r.relationType,
    })),
    [
      {
        theoremName: 'add_zero_identity',
        lemmaName: 'add_zero_lemma',
        relationType: 'REWRITES_WITH',
      },
    ],
  );
  assert.deepEqual(
    parsed.definitionUsages.map(u => ({
      theoremName: u.theoremName,
      symbolName: u.symbolName,
      usageRole: u.usageRole,
    })),
    [
      {
        theoremName: 'add_zero_identity',
        symbolName: 'idNat',
        usageRole: 'BOTH',
      },
    ],
  );
});

test('extractClientProofRelations extracts Rocq Definitions, Lemmas, Theorems, and APPLIES relations', () => {
  const rocqSource = `
    (* Rocq sample *)
    Definition id_nat (n : nat) : nat := n.

    Lemma add_zero_lemma : forall n : nat, n + 0 = n.
    Proof.
      intros n. induction n; simpl; auto.
    Qed.

    Theorem add_zero_identity : forall n : nat, id_nat n + 0 = n.
    Proof.
      intros n. unfold id_nat. apply add_zero_lemma.
    Qed.
  `;

  const parsed = extractClientProofRelations('ROCQ', rocqSource);
  assert.deepEqual(parsed.definitions.map(d => d.name), ['id_nat']);
  assert.deepEqual(parsed.theorems.map(t => t.name), ['add_zero_lemma', 'add_zero_identity']);
  assert.equal(parsed.lemmaRelations[0].lemmaName, 'add_zero_lemma');
  assert.equal(parsed.lemmaRelations[0].relationType, 'APPLIES');
  assert.equal(parsed.definitionUsages[0].symbolName, 'id_nat');
  assert.equal(parsed.definitionUsages[0].usageRole, 'BOTH');
});

test('renderProofDbInspectorHtml and renderProofDbModalHtml render all relational tables safely', () => {
  const snapshot: ProofKnowledgeSnapshot = {
    workspaceKey: 'local-playground',
    theorems: [
      {
        theoremId: 10,
        workspaceId: null,
        workspaceKey: 'local-playground',
        proverEngine: 'LEAN4',
        theoremName: 'add_zero_identity',
        statementKind: 'THEOREM',
        statementText: 'theorem add_zero_identity (n : Nat) : idNat n + 0 = n',
        proofCode: 'theorem add_zero_identity (n : Nat) : idNat n + 0 = n := by simpa [idNat] using add_zero_lemma n',
        verificationStatus: 'VERIFIED',
        verifierOutput: 'Verified',
        sourceCellId: 'cell-1',
        verifiedAt: '2026-11-19T01:00:00',
        updatedAt: '2026-11-19T01:00:00',
      },
    ],
    definitions: [
      {
        definitionId: 3,
        workspaceId: null,
        workspaceKey: 'local-playground',
        proverEngine: 'LEAN4',
        symbolName: 'idNat',
        definitionKind: 'def',
        signatureText: 'def idNat (n : Nat) : Nat',
        sourceCode: 'def idNat (n : Nat) : Nat := n',
        sourceCellId: 'cell-1',
        updatedAt: '2026-11-19T01:00:00',
      },
    ],
    lemmaRelations: [
      {
        relationId: 7,
        theoremId: 10,
        theoremName: 'add_zero_identity',
        theoremKind: 'THEOREM',
        lemmaTheoremId: 9,
        lemmaName: 'add_zero_lemma',
        relationType: 'REWRITES_WITH',
        referenceContext: 'simpa [idNat] using add_zero_lemma n',
        lemmaVerifiedInDb: true,
        createdAt: '2026-11-19T01:00:00',
      },
    ],
    definitionRelations: [
      {
        relationId: 5,
        theoremId: 10,
        theoremName: 'add_zero_identity',
        definitionId: 3,
        definitionName: 'idNat',
        definitionKind: 'def',
        usageRole: 'BOTH',
        referenceContext: 'simpa [idNat]',
        createdAt: '2026-11-19T01:00:00',
      },
    ],
  };

  const inspector = renderProofDbInspectorHtml(snapshot, false);
  assert.match(inspector, /dbo\.proof_theorems/);
  assert.match(inspector, /dbo\.proof_theorem_lemma_relations/);
  assert.match(inspector, /add_zero_identity/);
  assert.match(inspector, /add_zero_lemma/);
  assert.match(inspector, /idNat/);
  assert.match(inspector, /명제\+증명/);

  const modal = renderProofDbModalHtml(snapshot);
  assert.match(modal, /dbo\.proof_theorem_definition_relations/);
  assert.match(modal, /data-delete-theorem-id="10"/);
});
