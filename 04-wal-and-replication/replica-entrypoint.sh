#!/bin/bash
# Why this exists at all: the standard postgres Docker image's
# entrypoint assumes it's either initializing a brand-new, independent
# database (fresh initdb) or starting up an already-initialized one.
# Neither case is what a replica needs on its very first boot. A
# replica's data directory must instead be a byte-for-byte COPY of
# the primary's current state at some point in time, obtained via
# pg_basebackup -- and only after that copy exists can postgres start
# up in standby mode and begin streaming subsequent changes.
#
# Sequence:
#   1. If PGDATA is empty (first boot), wait for the primary to be
#      reachable, then run pg_basebackup with the -R flag.
#   2. -R automatically writes postgresql.auto.conf with
#      primary_conninfo (how to reach the primary) and creates
#      standby.signal -- the file whose mere presence tells Postgres
#      "start up as a replica, not as an independent primary."
#
#      Note the explicit application_name=replica1 in the connection
#      string below. Without it, the primary has no reliable name
#      for this replica, and synchronous_standby_names (needed for
#      a synchronous-vs-asynchronous commit comparison) would have
#      nothing to reference -- synchronous_commit=on would then
#      silently behave exactly like async, with no error, because
#      no standby was ever actually named as the confirming party.
#   3. Hand off to the normal postgres entrypoint, which will now see
#      an already-initialized data directory with standby.signal
#      present, and correctly start in streaming replica mode.

set -e

if [ -z "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
    echo "Replica data directory is empty. Waiting for primary to be ready..."

    until pg_isready -h primary -U replicator -d wal_lab 2>/dev/null; do
        echo "  primary not ready yet, retrying in 2s..."
        sleep 2
    done

    echo "Primary is ready. Running pg_basebackup..."

    PGPASSWORD="replicator_password" pg_basebackup \
        -d "host=primary port=5432 user=replicator application_name=replica1" \
        -D "$PGDATA" \
        -Fp \
        -Xs \
        -P \
        -R

    echo "pg_basebackup complete. standby.signal and primary_conninfo written."
    chmod 700 "$PGDATA"
fi

echo "Starting postgres (will detect standby.signal and start as a replica)..."
exec docker-entrypoint.sh postgres