-- experiment 06: locking strategies benchmark
--
-- Same problem as Lab 01: prevent duplicate payment processing under
-- concurrency. This time, five distinct concurrency control strategies
-- are benchmarked against each other, all solving the identical
-- correctness requirement:
--
--   1. SELECT ... FOR UPDATE              (pessimistic row lock)
--   2. SELECT ... FOR UPDATE SKIP LOCKED   (pessimistic, non-blocking)
--   3. pg_advisory_xact_lock               (advisory lock, not row-bound)
--   4. UNIQUE constraint                    (Lab 01's pattern, no lock held)
--   5. Optimistic locking via version column (read-compute-write-retry)
--
-- Each strategy is implemented against a structurally identical table
-- so no strategy gets an unfair schema advantage.

-- A note on index timing, since Experiments 01 and 02 established
-- "build indexes after bulk load" as the correct pattern at scale:
-- that rule's benefit is proportional to data volume. At the row
-- counts this experiment actually uses (500-1,000 rows, sized for
-- lock-contention testing, not throughput testing), incremental
-- index maintenance during INSERT costs low single-digit
-- milliseconds regardless of ordering -- there is no meaningful cost
-- to defer. The UNIQUE constraints below are therefore declared
-- inline, as normal, rather than added after a bulk load that
-- doesn't exist in this experiment's design.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

DROP TABLE IF EXISTS payments_pessimistic;
DROP TABLE IF EXISTS payments_skip_locked;
DROP TABLE IF EXISTS payments_advisory;
DROP TABLE IF EXISTS payments_unique_constraint;
DROP TABLE IF EXISTS payments_optimistic;

-- Strategies 1 and 2 (FOR UPDATE, FOR UPDATE SKIP LOCKED) both operate
-- on pre-existing rows representing pending payment jobs waiting to be
-- claimed and processed exactly once -- the classic "job queue" shape.
CREATE TABLE payments_pessimistic (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount          NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    processed_by    TEXT,
    processed_at    TIMESTAMPTZ
);

CREATE TABLE payments_skip_locked (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount          NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    processed_by    TEXT,
    processed_at    TIMESTAMPTZ
);

-- Strategy 3 (advisory locks) doesn't lock a row at all -- the lock
-- is keyed on an arbitrary integer derived from the idempotency key,
-- entirely separate from any row's physical existence. The table
-- still exists to record the eventual outcome.
CREATE TABLE payments_advisory (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount          NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    processed_by    TEXT,
    processed_at    TIMESTAMPTZ
);

-- Strategy 4: identical shape to Lab 01's payments table. No lock is
-- ever explicitly taken -- the UNIQUE constraint on idempotency_key
-- is the entire correctness mechanism, enforced by the B-tree index
-- at INSERT time, exactly as proven in Lab 01.
CREATE TABLE payments_unique_constraint (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount          NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    processed_by    TEXT,
    processed_at    TIMESTAMPTZ
);

-- Strategy 5: optimistic locking requires a version column. A writer
-- reads the row (capturing its current version), computes its update,
-- then writes conditionally on that exact version still matching --
-- WHERE version = $captured_version. If another writer won the race
-- in between, zero rows are affected and this writer must retry.
CREATE TABLE payments_optimistic (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount          NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    processed_by    TEXT,
    processed_at    TIMESTAMPTZ,
    version         INTEGER NOT NULL DEFAULT 0
);

COMMENT ON TABLE payments_pessimistic IS
    'Strategy 1: SELECT ... FOR UPDATE. Workers claim a pending row by
     locking it, blocking all other workers attempting the same row
     until the lock holder commits or rolls back.';

COMMENT ON TABLE payments_skip_locked IS
    'Strategy 2: SELECT ... FOR UPDATE SKIP LOCKED. Workers claim a
     pending row, but skip any row already locked by another worker
     instead of blocking -- correct for a work-queue where losing a
     race to claim one row just means claiming a different one.';

COMMENT ON TABLE payments_advisory IS
    'Strategy 3: pg_advisory_xact_lock keyed on a hash of
     idempotency_key. The lock exists independently of any row --
     it protects a LOGICAL identifier, not a physical tuple, which
     matters when the resource being protected does not correspond
     to exactly one row (or does not exist as a row yet at all).';

COMMENT ON TABLE payments_unique_constraint IS
    'Strategy 4: identical mechanism to Lab 01. No lock is ever taken
     explicitly -- correctness comes entirely from the UNIQUE
     constraint being enforced atomically by the B-tree index at
     INSERT time.';

COMMENT ON TABLE payments_optimistic IS
    'Strategy 5: version column. Writers read-compute-write
     conditionally on an unchanged version number, retrying on
     conflict rather than blocking or relying on a constraint.';