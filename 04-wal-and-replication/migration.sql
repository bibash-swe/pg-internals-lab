-- Run ONLY against the primary. The replica needs no migration of
-- its own -- that is the entire point of streaming replication: DDL
-- and DML executed on the primary arrive at the replica as WAL
-- records and are replayed automatically, never run there directly.

DROP TABLE IF EXISTS lag_test;

CREATE TABLE lag_test (
    id          BIGSERIAL PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE lag_test IS
    'Used by experiment_a_lag.py (trickle + burst replication lag
     measurement) and experiment_b_sync_vs_async.py (commit latency
     under synchronous_commit=on vs off). Always written to on the
     primary (port 5434) and read from the replica (port 5435) to
     measure real streaming replication behavior.';