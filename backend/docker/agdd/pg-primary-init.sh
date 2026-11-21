#!/bin/sh
# Runs once on first primary init: replication role + pg_hba entry for the streaming Read Replica.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '${PG_REPLICATION_PASSWORD}';
SELECT pg_create_physical_replication_slot('agdd_replica');
SQL
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
