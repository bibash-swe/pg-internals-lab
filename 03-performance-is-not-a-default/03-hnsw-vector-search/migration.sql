-- Schema: semantic search over support articles / past tickets --
-- a real production pattern (ticket deflection: suggest similar
-- resolved tickets or KB articles for a new incoming ticket before
-- a human agent ever sees it), distinct from the job_listings
-- schema used in Experiment 01.

-- vector(1024) is not a remembered or assumed number -- it was
-- confirmed empirically via get_embedding_dimension.py making a
-- real call to Mistral's mistral-embed model before this file was
-- written. If the embedding model ever changes, re-run that check
-- before trusting this column definition.

CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS support_articles;

CREATE TABLE support_articles (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    category    TEXT NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- No index yet, deliberately -- matching the pattern established in
-- every prior experiment in this lab: build the HNSW index (and its
-- tuning sweep across m / ef_construction) AFTER the bulk embedding
-- load completes, not before. seed.py loads the data; benchmark.py
-- builds and compares index configurations.

COMMENT ON TABLE support_articles IS
    'Semantic search over support articles for ticket deflection.
     Embeddings generated via a real Mistral mistral-embed API call
     per batch of rows (see seed.py) -- not synthetic vectors.';