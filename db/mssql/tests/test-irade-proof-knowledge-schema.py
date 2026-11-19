#!/usr/bin/env python3
"""SQL Server regression: proof knowledge relational schema, constraints, FKs, and idempotency."""
import argparse
from pathlib import Path
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='irade-mssql')
    args = parser.parse_args()
    migrations = Path(__file__).resolve().parents[1] / 'migrations'
    name = 'irade_proof_schema_' + uuid.uuid4().hex[:12]
    command = [
        'docker', 'exec', '-i', args.container, '/bin/bash', '-lc',
        'export SQLCMDPASSWORD="$MSSQL_SA_PASSWORD"; exec /opt/mssql-tools18/bin/sqlcmd "$@"',
        'sqlcmd', '-S', 'localhost', '-U', 'sa', '-C', '-b', '-I', '-h', '-1', '-W',
    ]

    def sql(text, database='master', expect_error=False):
        result = subprocess.run(command + ['-d', database], input=text, text=True, capture_output=True)
        if expect_error:
            if result.returncode == 0:
                raise RuntimeError('Expected SQL failure, but query succeeded.')
            return result.stdout + result.stderr
        if result.returncode:
            raise RuntimeError((result.stdout + result.stderr)[-2000:])
        return result.stdout

    migration_sql = (migrations / '2026-11-19-irade-proof-knowledge.sql').read_text()
    created = False
    try:
        sql(f"IF DB_ID(N'{name}') IS NOT NULL THROW 51000,'Test database exists',1; CREATE DATABASE [{name}];\nGO\n")
        created = True
        sql("CREATE ROLE [irade_runtime];\nGO\n", name)

        # Apply twice to verify idempotency.
        sql(migration_sql, name)
        sql(migration_sql, name)

        tables = sql(
            "SET NOCOUNT ON; SELECT name FROM sys.tables WHERE name LIKE 'proof_%' ORDER BY name;\nGO\n",
            name,
        ).strip().splitlines()
        expected = [
            'proof_definitions',
            'proof_theorem_definition_relations',
            'proof_theorem_lemma_relations',
            'proof_theorems',
        ]
        assert [t.strip() for t in tables] == expected, f'Unexpected tables: {tables}'

        # Insert definition + lemma + theorem + both relation tables.
        sql("""
INSERT INTO dbo.proof_definitions
    (workspace_key, prover_engine, symbol_name, definition_kind, signature_text, source_code, content_hash)
VALUES
    (N'ws-test', 'LEAN4', N'idNat', 'def', N'def idNat (n : Nat) : Nat', N'def idNat (n : Nat) : Nat := n', 'h-def');

INSERT INTO dbo.proof_theorems
    (workspace_key, prover_engine, theorem_name, statement_kind, statement_text, proof_code, verification_status, verifier_output, source_hash)
VALUES
    (N'ws-test', 'LEAN4', N'add_zero_lemma', 'LEMMA', N'lemma add_zero_lemma (n : Nat) : n + 0 = n', N'lemma add_zero_lemma (n : Nat) : n + 0 = n := by rfl', 'VERIFIED', N'Verified cleanly', 'h-lem'),
    (N'ws-test', 'LEAN4', N'add_zero_identity', 'THEOREM', N'theorem add_zero_identity (n : Nat) : idNat n + 0 = n', N'theorem add_zero_identity (n : Nat) : idNat n + 0 = n := by simpa [idNat] using add_zero_lemma n', 'VERIFIED', N'Verified cleanly', 'h-thm');

DECLARE @defId BIGINT = (SELECT definition_id FROM dbo.proof_definitions WHERE symbol_name = N'idNat');
DECLARE @lemId BIGINT = (SELECT theorem_id FROM dbo.proof_theorems WHERE theorem_name = N'add_zero_lemma');
DECLARE @thmId BIGINT = (SELECT theorem_id FROM dbo.proof_theorems WHERE theorem_name = N'add_zero_identity');

INSERT INTO dbo.proof_theorem_lemma_relations (theorem_id, lemma_theorem_id, lemma_name, relation_type, reference_context)
VALUES (@thmId, @lemId, N'add_zero_lemma', 'USES_LEMMA', N'using add_zero_lemma n');

INSERT INTO dbo.proof_theorem_definition_relations (theorem_id, definition_id, usage_role, reference_context)
VALUES (@thmId, @defId, 'BOTH', N'idNat in statement and simpa [idNat]');
GO
""", name)

        # Reject unverified proof status.
        err_status = sql("""
INSERT INTO dbo.proof_theorems
    (workspace_key, prover_engine, theorem_name, statement_kind, statement_text, proof_code, verification_status, verifier_output, source_hash)
VALUES
    (N'ws-test', 'LEAN4', N'bad_thm', 'THEOREM', N'theorem bad_thm : True', N'theorem bad_thm : True := sorry', 'FAILED', N'error', 'h-bad');
GO
""", name, expect_error=True)
        assert 'CK_proof_theorems_status' in err_status, err_status

        # Reject self-referential theorem -> lemma relation.
        err_self = sql("""
DECLARE @thmId BIGINT = (SELECT theorem_id FROM dbo.proof_theorems WHERE theorem_name = N'add_zero_identity');
INSERT INTO dbo.proof_theorem_lemma_relations (theorem_id, lemma_theorem_id, lemma_name, relation_type)
VALUES (@thmId, @thmId, N'add_zero_identity', 'USES_LEMMA');
GO
""", name, expect_error=True)
        assert 'CK_proof_theorem_lemma_no_self' in err_self, err_self

        # Verify cascading delete on source theorem removes its outgoing relations cleanly.
        counts = sql("""
SET NOCOUNT ON;
DELETE FROM dbo.proof_theorems WHERE theorem_name = N'add_zero_identity';
SELECT
    (SELECT COUNT(*) FROM dbo.proof_theorem_lemma_relations),
    (SELECT COUNT(*) FROM dbo.proof_theorem_definition_relations);
GO
""", name).strip().split()
        assert counts == ['0', '0'], f'Expected cascaded empty relations, got: {counts}'
        print(f'Verified MS SQL Server proof knowledge schema cleanly in {name}.')
    finally:
        if created:
            sql(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}];\nGO\n")


if __name__ == '__main__':
    main()
