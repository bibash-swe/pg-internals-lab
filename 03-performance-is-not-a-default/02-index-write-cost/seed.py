"""
Experiment 02: Write Cost of Indexes

Measures the real throughput cost of maintaining five indexes versus
zero indexes, across two distinct write patterns:

  Phase 1: Bulk INSERT 500,000 rows into each table (independently
           timed). This simulates an initial data load or migration.

  Phase 2: Build five indexes on transactions_five_indexes only,
           timed. (transactions_no_index never gets secondary indexes —
           it is the permanent zero-index baseline.)

  Phase 3: Run 100,000 individual UPDATE statements against each table
           (independently timed). This simulates the real production
           pattern from a payments system: status transitions
           (pending -> processing -> completed), one row at a time,
           not bulk.

  Phase 4: Capture pg_stat_user_tables before and after to expose
           dead tuple accumulation (n_dead_tup) — connecting this
           experiment's findings back to the MVCC bloat mechanism
           covered earlier in this curriculum.

Why bulk INSERT and individual UPDATE are measured separately:
    A bulk COPY-based load and a stream of individual UPDATEs stress
    index maintenance very differently. COPY can sometimes batch index
    page writes more efficiently; individual UPDATEs each trigger a
    full index update cycle plus the MVCC new-tuple-version overhead
    studied in earlier labs. Conflating the two into a single number
    would hide which write pattern actually drives the cost.

Run:
    python seed.py
"""
import asyncio
import os
import random
import time
import uuid

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

TOTAL_ROWS = 500_000
UPDATE_COUNT = 100_000

STATUSES_INITIAL = ["pending"]
STATUSES_NEXT = ["processing", "completed", "failed"]
CURRENCIES = ["USD", "EUR", "GBP", "NPR", "INR"]


async def generate_rows_stream(total: int):
    """
    Async generator yielding transaction rows with guaranteed-unique
    idempotency keys (required since transactions_five_indexes has a
    UNIQUE constraint on this column).
    """
    for i in range(total):
        yield (
            random.randint(1, 50_000),                  # account_id
            f"idem_{uuid.uuid4().hex}",                  # idempotency_key
            round(random.uniform(1.00, 5000.00), 2),     # amount
            random.choice(CURRENCIES),                   # currency
            "pending",                                   # status
        )


async def bulk_insert(conn, table_name: str, total: int) -> dict:
    """Bulk COPY insert, timed. Returns elapsed time and rows/sec."""
    print(f"\n  Inserting {total:,} rows into {table_name}...")
    start = time.perf_counter()

    await conn.copy_records_to_table(
        table_name,
        records=generate_rows_stream(total),
        columns=["account_id", "idempotency_key", "amount", "currency", "status"]
    )

    elapsed = time.perf_counter() - start
    rate = total / elapsed
    print(f"    {total:,} rows in {elapsed:.2f}s ({rate:,.0f} rows/sec)")
    return {"elapsed_sec": elapsed, "rows_per_sec": rate}


async def build_indexes(conn) -> dict:
    """
    Build the five indexes on transactions_five_indexes after bulk
    load. Mirrors experiment 01's correct pattern: build post-load,
    not maintain during load.
    """
    print("\n  Building 5 indexes on transactions_five_indexes...")

    indexes = [
        (
            "idx_txn_account_id",
            "CREATE INDEX IF NOT EXISTS idx_txn_account_id "
            "ON transactions_five_indexes (account_id)",
        ),
        (
            "idx_txn_status",
            "CREATE INDEX IF NOT EXISTS idx_txn_status "
            "ON transactions_five_indexes (status)",
        ),
        (
            "idx_txn_created_at",
            "CREATE INDEX IF NOT EXISTS idx_txn_created_at "
            "ON transactions_five_indexes (created_at)",
        ),
        (
            "idx_txn_account_created",
            "CREATE INDEX IF NOT EXISTS idx_txn_account_created "
            "ON transactions_five_indexes (account_id, created_at)",
        ),
        (
            "idx_txn_idempotency_unique",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_idempotency_unique "
            "ON transactions_five_indexes (idempotency_key)",
        ),
    ]

    timings = {}
    for name, sql in indexes:
        t0 = time.perf_counter()
        await conn.execute(sql)
        t1 = time.perf_counter()
        timings[name] = round(t1 - t0, 2)
        print(f"    {name}: {t1 - t0:.2f}s")

    return timings


async def run_updates(conn, table_name: str, count: int, total_rows: int) -> dict:
    """
    Run COUNT individual UPDATE statements, each transitioning one
    row's status from 'pending' to a random next status. This
    simulates the real production pattern: one status change per
    payment event, not a bulk operation.
    """
    print(f"\n  Running {count:,} individual UPDATEs on {table_name}...")
    start = time.perf_counter()

    for i in range(count):
        row_id = random.randint(1, total_rows)
        new_status = random.choice(STATUSES_NEXT)
        await conn.execute(
            f"UPDATE {table_name} SET status = $1 WHERE id = $2",
            new_status, row_id
        )
        if (i + 1) % 20_000 == 0:
            elapsed_so_far = time.perf_counter() - start
            rate_so_far = (i + 1) / elapsed_so_far
            print(f"    {i+1:,} / {count:,} updates "
                  f"({rate_so_far:,.0f} updates/sec)")

    elapsed = time.perf_counter() - start
    rate = count / elapsed
    print(f"    {count:,} updates in {elapsed:.2f}s ({rate:,.0f} updates/sec)")
    return {"elapsed_sec": elapsed, "updates_per_sec": rate}


async def get_table_stats(conn, table_name: str) -> dict:
    """
    Pull live MVCC stats: live tuples, dead tuples, table size.
    Connects this experiment back to the MVCC bloat mechanism from
    earlier in the curriculum — every UPDATE creates a new tuple
    version, and the old one becomes a dead tuple until VACUUM.
    """
    row = await conn.fetchrow("""
        SELECT n_live_tup, n_dead_tup, n_tup_ins, n_tup_upd
        FROM pg_stat_user_tables
        WHERE relname = $1
    """, table_name)

    size = await conn.fetchval(
        "SELECT pg_size_pretty(pg_total_relation_size($1))", table_name
    )

    return {
        "n_live_tup": row["n_live_tup"],
        "n_dead_tup": row["n_dead_tup"],
        "n_tup_ins": row["n_tup_ins"],
        "n_tup_upd": row["n_tup_upd"],
        "size": size,
    }


async def index_sizes(conn) -> list:
    rows = await conn.fetch("""
        SELECT indexname,
               pg_size_pretty(pg_relation_size(indexname::regclass)) AS size
        FROM pg_indexes
        WHERE tablename = 'transactions_five_indexes'
        ORDER BY pg_relation_size(indexname::regclass) DESC
    """)
    return [(r["indexname"], r["size"]) for r in rows]


async def main():
    print("Experiment 02: Write Cost of Indexes")
    print(f"Database: {DATABASE_URL}")
    print(f"Rows per table: {TOTAL_ROWS:,}")
    print(f"Updates per table: {UPDATE_COUNT:,}")

    results = {}

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5) as pool:
        async with pool.acquire() as conn:

            # Verify tables exist and are empty
            for table in ["transactions_no_index", "transactions_five_indexes"]:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = $1)", table
                )
                if not exists:
                    print(f"ERROR: {table} not found. Run migration.sql first.")
                    return
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                if count > 0:
                    print(f"WARNING: {table} already has {count:,} rows. "
                          f"Truncating for a clean run.")
                    await conn.execute(f"TRUNCATE TABLE {table}")

            print("\n" + "=" * 60)
            print("PHASE 1: Bulk INSERT (no indexes)")
            print("=" * 60)
            results["insert_no_index"] = await bulk_insert(
                conn, "transactions_no_index", TOTAL_ROWS
            )

            print("\n" + "=" * 60)
            print("PHASE 1b: Bulk INSERT (five indexes, built AFTER load)")
            print("=" * 60)
            results["insert_five_index_before_indexes_exist"] = await bulk_insert(
                conn, "transactions_five_indexes", TOTAL_ROWS
            )

            print("\n" + "=" * 60)
            print("PHASE 2: Build five indexes")
            print("=" * 60)
            results["index_build_times"] = await build_indexes(conn)
            results["index_sizes"] = await index_sizes(conn)

            print("\n" + "=" * 60)
            print("PHASE 3: Individual UPDATEs (no indexes)")
            print("=" * 60)
            results["update_no_index"] = await run_updates(
                conn, "transactions_no_index", UPDATE_COUNT, TOTAL_ROWS
            )

            print("\n" + "=" * 60)
            print("PHASE 3b: Individual UPDATEs (five indexes)")
            print("=" * 60)
            results["update_five_index"] = await run_updates(
                conn, "transactions_five_indexes", UPDATE_COUNT, TOTAL_ROWS
            )

            print("\n" + "=" * 60)
            print("PHASE 4: MVCC stats (live/dead tuples)")
            print("=" * 60)
            results["stats_no_index"] = await get_table_stats(
                conn, "transactions_no_index"
            )
            results["stats_five_index"] = await get_table_stats(
                conn, "transactions_five_indexes"
            )

            # ── Summary ──────────────────────────────────────────────
            print("\n\n" + "=" * 60)
            print("SUMMARY")
            print("=" * 60)

            ins_no = results["insert_no_index"]["rows_per_sec"]
            ins_five = results["insert_five_index_before_indexes_exist"]["rows_per_sec"]
            print(f"\nBulk INSERT throughput:")
            print(f"  No index:     {ins_no:,.0f} rows/sec")
            print(f"  Five indexes: {ins_five:,.0f} rows/sec  "
                  f"(NOTE: indexes built AFTER this insert, see Phase 2 below)")

            upd_no = results["update_no_index"]["updates_per_sec"]
            upd_five = results["update_five_index"]["updates_per_sec"]
            loss_pct = (1 - upd_five / upd_no) * 100
            print(f"\nIndividual UPDATE throughput:")
            print(f"  No index:     {upd_no:,.0f} updates/sec")
            print(f"  Five indexes: {upd_five:,.0f} updates/sec")
            print(f"  Throughput loss from 5 indexes: {loss_pct:.1f}%")

            print(f"\nMVCC dead tuples after {UPDATE_COUNT:,} updates:")
            print(f"  No index:     {results['stats_no_index']['n_dead_tup']:,} dead tuples, "
                  f"table size {results['stats_no_index']['size']}")
            print(f"  Five indexes: {results['stats_five_index']['n_dead_tup']:,} dead tuples, "
                  f"table size {results['stats_five_index']['size']}")

            print(f"\nIndex sizes:")
            for name, size in results["index_sizes"]:
                print(f"  {name:<35} {size}")

    return results


if __name__ == "__main__":
    asyncio.run(main())