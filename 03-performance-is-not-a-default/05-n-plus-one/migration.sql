-- experiment 05: N+1 query detection and elimination
--
-- Adds an `accounts` table representing account holders, and a real
-- foreign key relationship to transactions_five_indexes.account_id
-- (from Experiment 02). This models the exact shape of query that
-- produces N+1 in production: "list transactions, and for each one,
-- show who the account holder is" — the same pattern as showing a
-- payment feed with customer names attached.
--
-- The analogy worth holding onto: an N+1 query is structurally
-- identical to writing `for item in items { make_syscall(item) }`
-- in a hot loop instead of batching the work into one syscall. No
-- type system catches this — Python's ORM will happily compile and
-- run a loop that fires 50 queries instead of one JOIN, exactly as
-- Rust's compiler won't stop you from calling read() in a loop
-- instead of using a buffered reader. The cost is invisible until
-- measured.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

DROP TABLE IF EXISTS accounts CASCADE;

CREATE TABLE accounts (
    id          SERIAL PRIMARY KEY,
    holder_name TEXT NOT NULL,
    email       TEXT NOT NULL,
    country     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- No separate index needed on `id` -- PRIMARY KEY already creates one
-- implicitly (visible as accounts_pkey in \d accounts). This table's
-- lookups in this experiment are entirely by primary key, so no
-- additional indexing is required.

COMMENT ON TABLE accounts IS
    'Experiment 05: account holders. transactions_five_indexes.account_id
     is a logical foreign key into this table (not enforced with an
     actual FK constraint, matching how account_id was seeded in
     Experiment 02 as a random integer 1-50000 with no referential
     integrity check at the time).';