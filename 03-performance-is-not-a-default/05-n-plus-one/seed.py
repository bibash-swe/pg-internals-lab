"""
Experiment 05: N+1 Query Detection and Elimination

Seeds 50,000 account rows, matching the account_id range (1-50,000)
already used by transactions_five_indexes from Experiment 02. Every
transaction's account_id will resolve to a real account after this
runs -- no orphaned foreign keys, keeping the N+1 measurement clean.

Why COPY protocol again, same as every prior seed script:
    The same reasoning applies here as in a Rust ingestion pipeline
    using tokio-postgres's `copy_in` -- bulk-loading through
    individual prepared INSERT statements pays a full protocol
    round-trip (parse, bind, execute, sync) per row. COPY streams
    rows as a single continuous binary/text stream with one
    round-trip total. This is not a Python-specific optimization;
    it is a property of the PostgreSQL wire protocol itself, and
    the same COPY path is what tokio-postgres's `copy_in()` and
    Rust's `postgres` crate both use under the hood for exactly
    this reason.

Run:
    python seed.py
"""
import asyncio
import os
import random
import time

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

TOTAL_ACCOUNTS = 50_000

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Anita",
    "Bibash", "Sanjay", "Priya", "Wei", "Yuki", "Chen", "Hiroshi",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Bist", "Sharma", "Patel", "Kumar",
    "Wang", "Li", "Zhang", "Tanaka", "Kim", "Park", "Nguyen", "Chen",
]

COUNTRIES = [
    "United States", "United Kingdom", "Germany", "Nepal", "India",
    "Singapore", "Australia", "Canada", "Netherlands", "Poland", "Ireland",
]


async def generate_account_rows(total: int):
    """
    Async generator, same streaming pattern as every prior seed
    script in this lab. Near-zero memory footprint regardless of
    row count -- the equivalent Rust pattern would be an async
    Stream<Item = AccountRow> fed directly into copy_in().
    """
    for i in range(total):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        yield (
            f"{first} {last}",
            f"{first.lower()}.{last.lower()}{i}@example.com",
            random.choice(COUNTRIES),
        )


async def seed():
    print(f"Connecting to {DATABASE_URL}")

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5) as pool:
        async with pool.acquire() as conn:

            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'accounts')"
            )
            if not exists:
                print("ERROR: accounts table not found. Run migration.sql first.")
                return

            count = await conn.fetchval("SELECT COUNT(*) FROM accounts")
            if count > 0:
                print(f"accounts already has {count:,} rows. Truncating for a clean run.")
                await conn.execute("TRUNCATE TABLE accounts RESTART IDENTITY")

            print(f"Seeding {TOTAL_ACCOUNTS:,} accounts...")
            start = time.perf_counter()

            await conn.copy_records_to_table(
                "accounts",
                records=generate_account_rows(TOTAL_ACCOUNTS),
                columns=["holder_name", "email", "country"],
            )

            elapsed = time.perf_counter() - start
            final_count = await conn.fetchval("SELECT COUNT(*) FROM accounts")
            print(f"Seeded {final_count:,} accounts in {elapsed:.2f}s "
                  f"({final_count / elapsed:,.0f} rows/sec)")

            # Sanity check: confirm every transaction's account_id
            # resolves to a real account row. This is what makes the
            # JOIN in the "fixed" endpoint clean -- no NULL-handling
            # noise muddying the query count comparison.
            orphaned = await conn.fetchval("""
                SELECT COUNT(*)
                FROM transactions_five_indexes t
                LEFT JOIN accounts a ON a.id = t.account_id
                WHERE a.id IS NULL
            """)
            print(f"Orphaned transactions (account_id with no matching "
                  f"account): {orphaned:,}")
            if orphaned > 0:
                print("WARNING: some transactions reference accounts that "
                      "don't exist. This will affect JOIN result counts.")

            size = await conn.fetchval(
                "SELECT pg_size_pretty(pg_total_relation_size('accounts'))"
            )
            print(f"accounts table size: {size}")


if __name__ == "__main__":
    asyncio.run(seed())