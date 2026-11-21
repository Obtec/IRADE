#!/bin/sh
# Streaming Read Replica: clone the primary with pg_basebackup on first start, then run in standby mode.
set -eu
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h postgres -U "$POSTGRES_USER"; do sleep 2; done
  rm -rf "$PGDATA"/*
  PGPASSWORD="$PG_REPLICATION_PASSWORD" pg_basebackup -h postgres -U replicator -D "$PGDATA" \
    -X stream -R -S agdd_replica
  chmod 0700 "$PGDATA"
fi
exec postgres -c hot_standby=on -c max_connections=200 -c max_wal_senders=5 -c max_replication_slots=5
