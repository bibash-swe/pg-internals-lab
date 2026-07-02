"""
Experiment 06: Locking Strategies Benchmark

Each of the five strategies requires different starting data, because
each one solves the "prevent duplicate payment processing" problem at
a different point in the row's lifecycle.

WHAT ACTUALLY PRODUCES CONTENTION HERE (read this before changing
ROWS_TO_SEED or WORKER_COUNT):

    Total row count is NOT the variable that creates contention. The
    CLAIM QUERY is. For the job-queue strategies (pessimistic,
    skip_locked), benchmark.py will have every concurrent worker run:

        SELECT id FROM <table> WHERE status = 'pending'
        ORDER BY id LIMIT 1 FOR UPDATE [SKIP LOCKED]

    ORDER BY id LIMIT 1 deterministically targets the SAME lowest
    unclaimed row for every worker, every time -- this is what forces
    genuine collision, regardless of whether the table holds 100 rows
    or 100,000. Row count only needs to be large enough that the
    benchmark doesn't drain the whole table in the first few
    milliseconds of a run with WORKER_COUNT concurrent workers.

  Pessimistic (FOR UPDATE) and SKIP LOCKED:
      Need pre-existing 'pending' rows for workers to CLAIM via the
      query above.

  Advisory locks:
      No pre-existing rows. The lock is keyed on an integer derived
      from a SHARED, small set of idempotency keys (see
      SHARED_IDEMPOTENCY_KEYS below) -- many workers repeatedly race
      to be the one who successfully processes each shared key.

  UNIQUE constraint:
      Same shared-key setup as advisory locks, no pre-existing rows --
      identical setup to Lab 01, just with deliberately repeated keys
      across many attempts to generate real contention instead of
      each worker inserting a unique key with zero collision.

  Optimistic locking:
      Needs pre-existing rows with version=0, claimed via the same
      ORDER BY id LIMIT 1 pattern, then updated conditionally on
      version match.

CONCURRENCY LEVEL: 15 workers (see benchmark.py), chosen to stay at
75% of the container's max_connections=20 -- the same sizing
principle established in Experiment 04's result_analysis.md (size
pool usage to 50-70% of max_connections, leaving headroom for other
connections). This keeps connection exhaustion (Experiment 04's
variable) out of this experiment's results entirely, so any
contention observed here is attributable to locking strategy alone.

Run:
    python seed.py
"""
import asyncio
import os
import time
import uuid

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

# 500 pre-existing rows for the job-queue-shaped strategies. This
# number is sized against WORKER_COUNT (15, in benchmark.py) so that
# a full benchmark run drains the table over a measurable number of
# claim cycles -- not instantly, not so slowly the run takes minutes.
# The CLAIM QUERY (ORDER BY id LIMIT 1), not this count, is what
# produces real per-row contention -- see module docstring above.
ROWS_TO_SEED = 500

# For the two INSERT-race strategies (advisory, unique constraint),
# contention requires many workers repeatedly targeting the SAME
# small set of idempotency keys -- not each worker using a unique
# key, which would produce zero collisions and defeat the point of
# the benchmark entirely.
SHARED_IDEMPOTENCY_KEYS = [f"shared_key_{i}" for i in range(8)]


async def generate_pending_rows(total: int):
    """
    Async generator yielding (idempotency_key, amount) pairs for the
    two job-queue-shaped strategies (pessimistic, skip_locked) and
    the optimistic-locking strategy, all of which need pre-existing
    'pending' rows before the benchmark begins.
    """
    for i in range(total):
        yield (
            f"idem_{uuid.uuid4().hex}",
            round(100.0 + (i % 5000), 2),
        )


async def seed_job_queue_table(conn, table_name: str, total: int):
    """
    Seeds a table with pre-existing pending rows -- used for the
    pessimistic, skip_locked, and optimistic strategies.
    """
    count = await conn.fetchval(f"SELECT COUNT(*) FROM {table_name}")
    if count > 0:
        print(f"  {table_name} already has {count:,} rows. Truncating.")
        await conn.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY")

    start = time.perf_counter()
    await conn.copy_records_to_table(
        table_name,
        records=generate_pending_rows(total),
        columns=["idempotency_key", "amount"],
    )
    elapsed = time.perf_counter() - start
    print(f"  {table_name}: seeded {total:,} pending rows in {elapsed:.2f}s")


async def seed():
    print(f"Connecting to {DATABASE_URL}")

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5) as pool:
        async with pool.acquire() as conn:

            required_tables = [
                "payments_pessimistic",
                "payments_skip_locked",
                "payments_advisory",
                "payments_unique_constraint",
                "payments_optimistic",
            ]
            for table in required_tables:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = $1)", table
                )
                if not exists:
                    print(f"ERROR: {table} not found. Run migration.sql first.")
                    return

            print(f"\nSeeding {ROWS_TO_SEED:,} pending rows into "
                  f"job-queue-shaped tables...")

            # Pessimistic and SKIP LOCKED both need pre-existing pending
            # rows for workers to claim via a lock.
            await seed_job_queue_table(conn, "payments_pessimistic", ROWS_TO_SEED)
            await seed_job_queue_table(conn, "payments_skip_locked", ROWS_TO_SEED)

            # Optimistic locking also needs pre-existing rows (with
            # version=0, which the table's DEFAULT already provides).
            await seed_job_queue_table(conn, "payments_optimistic", ROWS_TO_SEED)

            # Advisory locks and the UNIQUE constraint strategy stay
            # EMPTY of rows here -- both test concurrent INSERT-time
            # races, not claiming pre-existing rows. benchmark.py will
            # have all 15 workers repeatedly attempt to insert against
            # SHARED_IDEMPOTENCY_KEYS (defined above, 8 shared keys)
            # rather than unique keys per worker, so real collisions
            # occur on every attempt instead of never happening.
            print(f"\n  payments_advisory: left empty (workers will race "
                  f"INSERTs against {len(SHARED_IDEMPOTENCY_KEYS)} shared keys)")
            print(f"  payments_unique_constraint: left empty (same shared-key "
                  f"race pattern, matching Lab 01's mechanism)")

            # Sanity check: confirm counts match expectations before
            # any benchmark runs against this data.
            print("\nFinal row counts:")
            for table in required_tables:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                print(f"  {table:<30} {count:,} rows")


if __name__ == "__main__":
    asyncio.run(seed())