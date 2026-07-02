"""
Experiment 06: Locking Strategies Benchmark

Five concurrency-control strategies, all solving the same problem:
prevent duplicate payment processing under concurrent access. Each
strategy is benchmarked with WORKER_COUNT concurrent asyncio tasks,
all sharing a connection pool sized to EXACTLY WORKER_COUNT.

WHY POOL SIZE MUST EQUAL WORKER COUNT EXACTLY:
    Experiment 04 proved what happens when a pool is SMALLER than
    concurrent demand: requests queue for a connection. If this
    benchmark used a smaller pool, some workers would queue for a
    CONNECTION before ever reaching a database LOCK -- contaminating
    this experiment's measurement with Experiment 04's variable
    instead of isolating this experiment's actual variable, which is
    contention at the lock level. Pool size = worker count means
    every worker holds its own connection for the entire benchmark;
    any blocking observed is attributable purely to the locking
    strategy under test, nothing else.

THE TWO SHAPES OF STRATEGY, AND HOW EACH IS MEASURED:

    Job-queue shape (pessimistic, skip_locked, optimistic):
        Each worker loops, claiming the lowest available pending row
        via `ORDER BY id LIMIT 1`, until no pending rows remain.
        Correctness check: exactly ROWS_TO_SEED rows end up
        'completed', with zero duplicates (checked via total count,
        since two workers claiming the SAME row is structurally
        impossible if the strategy is correct -- the WHERE
        status='pending' filter combined with row-level locking or
        version-checking prevents it by construction).

    Insert-race shape (advisory, unique constraint):
        All 15 workers repeatedly attempt to be the one who
        successfully creates a row for each of 8 SHARED idempotency
        keys, across 20 rounds -- 2,400 total attempts, of which
        exactly 8 should ever succeed. This deliberately mimics a
        retry storm hitting the same handful of idempotency keys
        repeatedly, the same production shape proven in Lab 01.

Run:
    python benchmark.py
"""
import asyncio
import os
import time

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

WORKER_COUNT = 15  # 75% of max_connections=20, matching Experiment 04's
                    # established sizing principle. Pool max_size below
                    # is set to exactly this number -- see module
                    # docstring for why that equality matters.

ROWS_TO_SEED = 500  # must match seed.py

SHARED_IDEMPOTENCY_KEYS = [f"shared_key_{i}" for i in range(8)]
ROUNDS_PER_WORKER = 20  # 15 workers * 8 keys * 20 rounds = 2,400 total
                          # attempts against 8 keys -- a sustained retry
                          # storm, not a single-shot collision test.


# STRATEGY 1: SELECT ... FOR UPDATE (pessimistic, blocking)
async def pessimistic_worker(pool: asyncpg.Pool, worker_id: int) -> int:
    """
    Claims rows one at a time via a blocking row lock. If another
    worker already holds the lock on the lowest pending row, this
    worker WAITS until that lock is released (commit or rollback)
    before proceeding. Under PostgreSQL's MVCC rules, once the wait
    ends, the WHERE clause is re-evaluated against the row's current
    state -- if it's no longer 'pending' (the lock holder already
    completed it), this worker's query transparently moves on to the
    next lowest pending row instead of ever seeing a stale result.
    """
    claimed = 0
    async with pool.acquire() as conn:
        while True:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id FROM payments_pessimistic "
                    "WHERE status = 'pending' "
                    "ORDER BY id LIMIT 1 FOR UPDATE"
                )
                if row is None:
                    break
                await conn.execute(
                    "UPDATE payments_pessimistic "
                    "SET status = 'completed', processed_by = $1, "
                    "processed_at = now() WHERE id = $2",
                    f"worker_{worker_id}", row["id"]
                )
                claimed += 1
    return claimed


# STRATEGY 2: SELECT ... FOR UPDATE SKIP LOCKED (pessimistic, non-blocking)
async def skip_locked_worker(pool: asyncpg.Pool, worker_id: int) -> int:
    """
    Identical to the pessimistic worker, except SKIP LOCKED means
    this worker never waits on a lock held by someone else -- it
    immediately moves to the next unlocked row instead. Correctness
    is preserved by the same mechanism (WHERE status='pending'), but
    throughput should be dramatically higher since no worker ever
    blocks on another.
    """
    claimed = 0
    async with pool.acquire() as conn:
        while True:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id FROM payments_skip_locked "
                    "WHERE status = 'pending' "
                    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
                if row is None:
                    break
                await conn.execute(
                    "UPDATE payments_skip_locked "
                    "SET status = 'completed', processed_by = $1, "
                    "processed_at = now() WHERE id = $2",
                    f"worker_{worker_id}", row["id"]
                )
                claimed += 1
    return claimed


# STRATEGY 3: pg_advisory_xact_lock (advisory, blocking, row-independent)
async def advisory_worker(pool: asyncpg.Pool, worker_id: int) -> int:
    """
    For each of the 8 shared keys, across ROUNDS_PER_WORKER rounds,
    acquires a transaction-scoped advisory lock keyed on a hash of
    the idempotency key itself -- NOT on any row, since no row may
    exist yet. Like pessimistic locking, this BLOCKS: if another
    worker holds the lock for this exact key, this worker waits.
    Once acquired, checks whether a row already exists for this key;
    if not, inserts one. The lock releases automatically on
    transaction commit.

    Known limitation, worth stating explicitly: hashtext() can
    theoretically collide two different string keys onto the same
    lock id. With only 8 distinct keys the probability is
    negligible, but this is a real production caveat of hash-based
    advisory locking, not unique to this benchmark.
    """
    successes = 0
    async with pool.acquire() as conn:
        for _ in range(ROUNDS_PER_WORKER):
            for key in SHARED_IDEMPOTENCY_KEYS:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
                        key
                    )
                    exists = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM payments_advisory "
                        "WHERE idempotency_key = $1)",
                        key
                    )
                    if not exists:
                        await conn.execute(
                            "INSERT INTO payments_advisory "
                            "(idempotency_key, amount, status, processed_by, processed_at) "
                            "VALUES ($1, 100.00, 'completed', $2, now())",
                            key, f"worker_{worker_id}"
                        )
                        successes += 1
    return successes


# STRATEGY 4: UNIQUE constraint (no explicit lock, Lab 01's pattern)
async def unique_constraint_worker(pool: asyncpg.Pool, worker_id: int) -> int:
    """
    No lock is ever taken explicitly. Every worker simply attempts
    the INSERT directly; the B-tree index backing the UNIQUE
    constraint resolves the race atomically at commit time -- exactly
    the mechanism proven in Lab 01. A losing worker gets a
    UniqueViolationError, caught and treated as "someone else already
    processed this key," identical to a real idempotent payment
    handler's expected behavior on a retried request.
    """
    successes = 0
    async with pool.acquire() as conn:
        for _ in range(ROUNDS_PER_WORKER):
            for key in SHARED_IDEMPOTENCY_KEYS:
                try:
                    await conn.execute(
                        "INSERT INTO payments_unique_constraint "
                        "(idempotency_key, amount, status, processed_by, processed_at) "
                        "VALUES ($1, 100.00, 'completed', $2, now())",
                        key, f"worker_{worker_id}"
                    )
                    successes += 1
                except asyncpg.UniqueViolationError:
                    pass  # lost the race -- expected, not an error condition
    return successes


# STRATEGY 5: Optimistic locking via version column
async def optimistic_worker(pool: asyncpg.Pool, worker_id: int) -> int:
    """
    Never takes a lock at all. Reads the lowest pending row's current
    version (a plain SELECT, no FOR UPDATE -- multiple workers can
    read the SAME row simultaneously), then attempts to write
    conditionally: UPDATE ... WHERE id = $1 AND version = $2. If
    another worker won the race in between this worker's read and
    write, zero rows are affected -- this worker's write silently
    fails and it must loop back and try again with a fresh read.
    Every failed attempt here is wasted read+write work, in exchange
    for never blocking.
    """
    claimed = 0
    async with pool.acquire() as conn:
        while True:
            row = await conn.fetchrow(
                "SELECT id, version FROM payments_optimistic "
                "WHERE status = 'pending' ORDER BY id LIMIT 1"
            )
            if row is None:
                break

            result = await conn.execute(
                "UPDATE payments_optimistic "
                "SET status = 'completed', processed_by = $1, "
                "processed_at = now(), version = version + 1 "
                "WHERE id = $2 AND version = $3 AND status = 'pending'",
                f"worker_{worker_id}", row["id"], row["version"]
            )
            # asyncpg returns a string like "UPDATE 1" or "UPDATE 0"
            if result == "UPDATE 1":
                claimed += 1
            # else: lost the race, loop back and try a fresh read
    return claimed


# Orchestration
async def run_job_queue_strategy(pool, worker_fn, table_name: str, label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"STRATEGY: {label}")
    print(f"{'='*60}")

    start = time.perf_counter()
    claim_counts = await asyncio.gather(
        *[worker_fn(pool, i) for i in range(WORKER_COUNT)]
    )
    elapsed = time.perf_counter() - start

    total_claimed = sum(claim_counts)

    async with pool.acquire() as conn:
        completed_count = await conn.fetchval(
            f"SELECT COUNT(*) FROM {table_name} WHERE status = 'completed'"
        )
        pending_remaining = await conn.fetchval(
            f"SELECT COUNT(*) FROM {table_name} WHERE status = 'pending'"
        )

    throughput = total_claimed / elapsed if elapsed > 0 else 0
    correct = (completed_count == ROWS_TO_SEED and pending_remaining == 0)

    print(f"  Elapsed: {elapsed:.3f}s")
    print(f"  Total claimed (summed from workers): {total_claimed}")
    print(f"  Rows marked 'completed' in DB: {completed_count} "
          f"(expected {ROWS_TO_SEED})")
    print(f"  Rows still 'pending': {pending_remaining} (expected 0)")
    print(f"  Throughput: {throughput:,.1f} claims/sec")
    print(f"  Correctness: {'PASS' if correct else 'FAIL'}")
    print(f"  Per-worker claim distribution: {sorted(claim_counts)}")

    return {
        "label": label,
        "elapsed_sec": elapsed,
        "total_claimed": total_claimed,
        "completed_in_db": completed_count,
        "throughput_per_sec": throughput,
        "correct": correct,
        "per_worker_claims": claim_counts,
    }


async def run_insert_race_strategy(pool, worker_fn, table_name: str, label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"STRATEGY: {label}")
    print(f"{'='*60}")

    start = time.perf_counter()
    success_counts = await asyncio.gather(
        *[worker_fn(pool, i) for i in range(WORKER_COUNT)]
    )
    elapsed = time.perf_counter() - start

    total_successes = sum(success_counts)
    total_attempts = WORKER_COUNT * len(SHARED_IDEMPOTENCY_KEYS) * ROUNDS_PER_WORKER

    async with pool.acquire() as conn:
        row_count = await conn.fetchval(f"SELECT COUNT(*) FROM {table_name}")

    throughput = total_attempts / elapsed if elapsed > 0 else 0
    correct = (row_count == len(SHARED_IDEMPOTENCY_KEYS) and
               total_successes == len(SHARED_IDEMPOTENCY_KEYS))

    print(f"  Elapsed: {elapsed:.3f}s")
    print(f"  Total attempts: {total_attempts:,}")
    print(f"  Total successful claims (summed from workers): {total_successes} "
          f"(expected {len(SHARED_IDEMPOTENCY_KEYS)})")
    print(f"  Rows in DB: {row_count} (expected {len(SHARED_IDEMPOTENCY_KEYS)})")
    print(f"  Throughput: {throughput:,.1f} attempts/sec")
    print(f"  Correctness: {'PASS' if correct else 'FAIL'}")

    return {
        "label": label,
        "elapsed_sec": elapsed,
        "total_attempts": total_attempts,
        "total_successes": total_successes,
        "throughput_per_sec": throughput,
        "correct": correct,
    }


async def main():
    print("Experiment 06: Locking Strategies Benchmark")
    print(f"Database: {DATABASE_URL}")
    print(f"Worker count: {WORKER_COUNT} (pool size matched exactly)")
    print(f"Job-queue rows per strategy: {ROWS_TO_SEED}")
    print(f"Shared keys for insert-race strategies: {len(SHARED_IDEMPOTENCY_KEYS)}")
    print(f"Rounds per worker (insert-race): {ROUNDS_PER_WORKER}")

    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=WORKER_COUNT, max_size=WORKER_COUNT
    )

    try:
        results = []

        results.append(await run_job_queue_strategy(
            pool, pessimistic_worker, "payments_pessimistic",
            "1. SELECT ... FOR UPDATE (pessimistic, blocking)"
        ))

        results.append(await run_job_queue_strategy(
            pool, skip_locked_worker, "payments_skip_locked",
            "2. SELECT ... FOR UPDATE SKIP LOCKED (pessimistic, non-blocking)"
        ))

        results.append(await run_insert_race_strategy(
            pool, advisory_worker, "payments_advisory",
            "3. pg_advisory_xact_lock (advisory, blocking, row-independent)"
        ))

        results.append(await run_insert_race_strategy(
            pool, unique_constraint_worker, "payments_unique_constraint",
            "4. UNIQUE constraint (no explicit lock, Lab 01's pattern)"
        ))

        results.append(await run_job_queue_strategy(
            pool, optimistic_worker, "payments_optimistic",
            "5. Optimistic locking via version column (no lock, retry on conflict)"
        ))

        print(f"\n\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        for r in results:
            status = "PASS" if r["correct"] else "FAIL"
            print(f"  [{status}] {r['label']}")
            print(f"         {r['elapsed_sec']:.3f}s, "
                  f"{r['throughput_per_sec']:,.1f}/sec")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())