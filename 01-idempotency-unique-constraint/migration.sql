-- Why: UNIQUE constraint on idempotency_key prevents duplicate payment rows at the database engine level,
-- enforced by a B-tree index page lock, not application-level checks.
CREATE TABLE payments(
    id SERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    CONSTRAINT payments_key_unique UNIQUE (idempotency_key)
);