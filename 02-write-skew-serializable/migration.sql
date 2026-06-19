-- Why: demonstrates write skew — a concurrency anomaly that survives
-- both Read Committed and Repeatable Read isolation levels, and is
-- only caught by SERIALIZABLE.

-- Scenario: two doctors are on call. Business rule: at least one
-- doctor must remain on call at all times. Each doctor independently
-- checks "is someone else covering?" before going off-call. Under
-- Read Committed or Repeatable Read, both doctors can read the same
-- pre-change state concurrently, both conclude it's safe to leave,
-- and both commit — leaving zero doctors on call, even though each
-- individual transaction's logic was locally correct.

CREATE TABLE on_call (
    id SERIAL PRIMARY KEY,
    doctor TEXT NOT NULL,
    is_on_call BOOLEAN NOT NULL
);

INSERT INTO on_call (doctor, is_on_call) VALUES
    ('Alice', true),
    ('Bob', true);

-- Reproduce the anomaly (see experiment.md for full transcript):

-- Terminal A:
--   BEGIN ISOLATION LEVEL SERIALIZABLE;
--   SELECT count(*) FROM on_call WHERE is_on_call = true;  -- 2
--   UPDATE on_call SET is_on_call = false WHERE doctor = 'Alice';
--   COMMIT;

-- Terminal B (started before Terminal A's COMMIT):
--   BEGIN ISOLATION LEVEL SERIALIZABLE;
--   SELECT count(*) FROM on_call WHERE is_on_call = true;  -- also 2
--   UPDATE on_call SET is_on_call = false WHERE doctor = 'Bob';
--   COMMIT;  -- fails: could not serialize access due to
--            -- read/write dependencies among transactions

-- At READ COMMITTED or REPEATABLE READ, Terminal B's COMMIT would
-- succeed instead, leaving zero doctors on call.
