-- experiment 01: index types benchmark
-- Creates the job_listings table used across all index benchmarks.
-- Extensions are created here so this file is fully self-contained.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_prewarm;

-- Drop and recreate for idempotency
DROP TABLE IF EXISTS job_listings;

CREATE TABLE job_listings (
    id          BIGSERIAL PRIMARY KEY,
    company_id  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT NOT NULL,
    salary_min  INTEGER NOT NULL,
    salary_max  INTEGER NOT NULL,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT NOT NULL
);

-- Note: we create indexes AFTER seeding data.
-- Creating indexes on an empty table and then bulk-inserting
-- forces the index to be updated row-by-row during the insert,
-- which is dramatically slower than a bulk build after the fact.
-- seed.py handles the index creation after INSERT completes.

COMMENT ON TABLE job_listings IS
    'Benchmark table for experiment 01: index types.
     1M rows seeded by seed.py after table creation.
     Indexes created post-seed for realistic bulk-load performance.';