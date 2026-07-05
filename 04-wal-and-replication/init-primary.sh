#!/bin/bash
# Runs automatically on the primary's FIRST startup (Postgres's
# docker-entrypoint-initdb.d mechanism only fires once, against a
# freshly initialized, empty data directory). Two things need to
# happen here before any replica can connect:
#
#   1. Create a dedicated replication role. This is NOT the same as
#      the postgres superuser -- a role needs the REPLICATION
#      privilege specifically to open a streaming replication
#      connection, separate from normal query privileges.
#
#   2. Permit that role to connect from the replica's network. By
#      default, pg_hba.conf only allows normal client connections --
#      a replication connection is a structurally different kind of
#      connection (it doesn't run SQL queries, it streams WAL bytes)
#      and needs its own explicit permission line.

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator_password';
EOSQL

# Append a pg_hba.conf rule permitting the replicator role to open a
# replication connection from anywhere on the Docker network. In a
# real production setup this CIDR would be scoped tightly to the
# replica's actual subnet, not left open -- this lab's Docker network
# is isolated and single-purpose, so a broad rule is acceptable here
# specifically, not as a general pattern to copy into production.
echo "host replication replicator 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"