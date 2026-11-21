#!/bin/sh
# PgBouncer in transaction pooling mode (DOC.md §4.1). Two pools: primary (rw) and replica (ro).
set -eu
umask 077
cat > /tmp/userlist.txt <<USERS
"${PG_USER}" "${PG_PASSWORD}"
USERS
cat > /tmp/pgbouncer.ini <<INI
[databases]
agdd = host=postgres port=5432 dbname=${PG_DB}
agdd_ro = host=postgres-replica port=5432 dbname=${PG_DB}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /tmp/userlist.txt
pool_mode = transaction
max_client_conn = ${PGB_MAX_CLIENT_CONN:-500}
default_pool_size = ${PGB_POOL_SIZE:-20}
server_reset_query =
ignore_startup_parameters = extra_float_digits,options
max_prepared_statements = 0
INI
exec pgbouncer /tmp/pgbouncer.ini
