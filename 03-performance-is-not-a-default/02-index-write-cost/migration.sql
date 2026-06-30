-- experiment 02: write cost of indexes

-- Two structurally identical tables modeling a payments transaction
-- ledger. One has zero indexes. One has five indexes mirroring a
-- realistic production access pattern:
--   - lookup transactions by account
--   - filter by status (pending/processing/completed)
--   - range queries by date
--   - the most common real query: "this account's recent transactions"
--   - a UNIQUE index simulating an idempotency key

-- We measure the cost of maintaining these five indexes under two
-- distinct write patterns: bulk INSERT (initial load) and individual
-- UPDATE (steady-state status transitions, exactly like the
-- pending -> processing -> completed flow from production payment
-- systems).

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

DROP TABLE IF EXISTS transactions_no_index;
DROP TABLE IF EXISTS transactions_five_indexes;

CREATE TABLE transactions_no_index (
    id              BIGSERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    amount          NUMERIC(12,2) NOT NULL,
    currency        CHAR(3) NOT NULL,
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE transactions_five_indexes (
    id              BIGSERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    amount          NUMERIC(12,2) NOT NULL,
    currency        CHAR(3) NOT NULL,
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Note: as in experiment 01, indexes are
-- created AFTER the bulk load in seed.py — building an index in one
-- sorted pass is far faster than maintaining it row-by-row during COPY.
-- The five indexes created post-load are:
--   1. B-tree on account_id            (lookup by account)
--   2. B-tree on status                (filter pending/processing/completed)
--   3. B-tree on created_at            (date range reports)
--   4. Composite B-tree (account_id, created_at)  (most common real query)
--   5. UNIQUE on idempotency_key       (duplicate prevention, as in lab 01)
--
-- transactions_no_index intentionally has ONLY its primary key (required
-- by Postgres for BIGSERIAL) and nothing else — this is the true zero-index
-- baseline for comparison.

COMMENT ON TABLE transactions_no_index IS
    'Experiment 02 baseline: zero secondary indexes. Only the implicit
     primary key index exists. Used to measure write throughput with
     no index maintenance overhead.';

COMMENT ON TABLE transactions_five_indexes IS
    'Experiment 02 comparison: five secondary indexes mirroring a
     realistic payments table access pattern. Used to measure the
     write throughput cost of maintaining those indexes on every
     INSERT and UPDATE.';