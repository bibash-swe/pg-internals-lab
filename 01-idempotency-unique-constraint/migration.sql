-- Why: UNIQUE constraint on idempotency_key prevents duplicate payment rows at the database engine level,
-- enforced by a B-tree index page lock, not application-level checks.
DROP TABLE IF EXISTS payments;

CREATE TABLE payments(
    id SERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL
);

ALTER TABLE payments ADD CONSTRAINT payments_key_unique UNIQUE (idempotency_key);