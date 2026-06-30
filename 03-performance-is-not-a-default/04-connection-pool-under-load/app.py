"""
Experiment 04: Connection Pooling Under Load

Two endpoints, identical query, different connection strategy:

  GET /raw/{txn_id}    -> asyncpg.connect() fresh per request, closed
                          after. No queueing: PostgreSQL rejects the
                          connection outright once max_connections (20
                          in our constrained container) is exceeded.

  GET /pooled/{txn_id} -> borrows a connection from a shared
                          asyncpg.Pool (max_size=10). Excess requests
                          QUEUE for an available connection instead of
                          failing immediately, converting concurrency
                          pressure into latency rather than errors --
                          up to the pool's acquire timeout.

Both endpoints run the same real query against transactions_five_indexes
(from Experiment 02, 500,000 rows, primary key indexed) so the
comparison reflects actual work, not a no-op.

The pool's acquire timeout is set explicitly and deliberately low
(POOL_ACQUIRE_TIMEOUT_SECONDS) so that under heavy enough load, the
pooled endpoint can ALSO fail -- this is intentional. A connection
pool is not a magic fix; it is a different, better-behaved failure
mode, not an absence of failure.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000

Requires the Docker container (lab3, port 5433) running with
transactions_five_indexes already seeded from Experiment 02.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

# Pool sized to stay well under the container's max_connections=20,
# leaving headroom for psql sessions and other connections during
# the test.
POOL_MIN_SIZE = 5
POOL_MAX_SIZE = 10

# Deliberately explicit, not left as asyncpg's default of None
# (unbounded wait). A production pool should always set this --
# an unbounded acquire timeout means a slow query anywhere can cause
# unbounded request pileup elsewhere in the system.
POOL_ACQUIRE_TIMEOUT_SECONDS = 5.0

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Everything before yield = startup.
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        timeout=POOL_ACQUIRE_TIMEOUT_SECONDS,
        command_timeout=10.0,
    )
    print(f"Pool started: min={POOL_MIN_SIZE} max={POOL_MAX_SIZE} "
          f"acquire_timeout={POOL_ACQUIRE_TIMEOUT_SECONDS}s")

    yield  # the app runs while suspended here

    # Everything after yield = shutdown. Guaranteed to run on
    # graceful shutdown, the same way a `finally` block guarantees
    # cleanup -- this is the actual reason lifespan replaced
    # on_event, not just a deprecation formality.
    await pool.close()
    print("Pool closed.")


app = FastAPI(
    title="Experiment 04: Connection Pooling Under Load",
    lifespan=lifespan,
)


@app.get("/raw/{txn_id}")
async def get_txn_raw(txn_id: int):
    """
    Opens a brand new connection for this request alone, runs one
    query, closes it. No reuse, no queueing. This is the pattern
    every one of our earlier lab scripts used (test_race.py,
    test_write_skew.py) -- correct for a controlled two-connection
    experiment, but this endpoint exists specifically to prove why
    it does NOT scale under real concurrent request volume.
    """
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        # This is the failure mode we expect once concurrent raw
        # connections exceed max_connections: PostgreSQL rejects the
        # connection outright (e.g. "too many clients already").
        raise HTTPException(
            status_code=503,
            detail=f"connection_failed: {type(e).__name__}: {e}"
        )

    try:
        row = await conn.fetchrow(
            "SELECT id, account_id, amount, status FROM transactions_five_indexes WHERE id = $1",
            txn_id
        )
    finally:
        await conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="transaction not found")

    return dict(row)


@app.get("/pooled/{txn_id}")
async def get_txn_pooled(txn_id: int):
    """
    Borrows a connection from the shared pool instead of opening a
    new one. If all POOL_MAX_SIZE connections are busy, this request
    waits in the pool's internal queue for up to
    POOL_ACQUIRE_TIMEOUT_SECONDS before raising a timeout error.

    Expected behavior under load: increased latency as requests queue,
    NOT immediate connection errors -- up until the acquire timeout
    itself is exceeded, at which point this endpoint can also start
    returning errors. That crossover point is itself a finding worth
    capturing, not something to hide.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, account_id, amount, status FROM transactions_five_indexes WHERE id = $1",
                txn_id
            )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="pool_acquire_timeout: no connection became available "
                   f"within {POOL_ACQUIRE_TIMEOUT_SECONDS}s"
        )

    if row is None:
        raise HTTPException(status_code=404, detail="transaction not found")

    return dict(row)


@app.get("/health")
async def health():
    return {"status": "ok", "pool_size": pool.get_size() if pool else None}