"""
Proves two things experimentally:
1. broken_naive.create_payment() produces DUPLICATE rows under concurrent calls with the
   same idempotency key.
2. fixed_idempotent.create_payment produces exactly ONE row under the same concurrent conditions.\

Requires a running PostgreSQL instance with connection details, and the 'payments' table created via
migration.sql

"""
import asyncio
import asyncpg

import broken_naive
import fixed_idempotent

# Postgres setup
DB_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"

async def reset_table(conn: asyncpg.Connection):
    """Wipe the payments table so each test run starts clean"""
    await conn.execute("DELETE FROM payments")


async def run_concurrent(create_payment_fn, key:str, label:str):
    """
    Open TWO separate connections (simulating two different application server instance handling
    concurrent requests) and fires create_payment_fn at the same time using asyncio.gather.

    asyncio.gather(*tasks) runs all given coroutines concurrently and waits for all of them to
    finish. This is how we simulate "two requests arriving at the same moment".
    """
    conn_a = await asyncpg.connect(DB_DSN)
    conn_b = await asyncpg.connect(DB_DSN)

    print(f"\n --{label}--")
    try:
        results = await asyncio.gather(
            create_payment_fn(conn_a, key, 100.00),
            create_payment_fn(conn_b, key, 100.00),
            return_exceptions = True, # don't crash if one side raises
        )
        for i, r in enumerate(results, start=1):
            print(f"Request {i} result: {r}")
    finally:
        await conn_a.close()
        await conn_b.close()


async def count_rows(key: str) -> int:
    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(
            "SELECT * FROM payments WHERE idempotency_key = $1", key
        )
        print(f"Rows in DB for key '{key}': {len(rows)}")
        for row in rows:
            print(f" -> {dict(row)}")
        return len(rows)
    finally:
        await conn.close()


async def main():
    setup_conn = await asyncpg.connect(DB_DSN)
    await reset_table(setup_conn)
    await setup_conn.close()

    # Test 1: the broken version
    await run_concurrent(
        broken_naive.create_payment, "pay_race_test", "BROKEN naive version"
    )
    broken_count = await count_rows("pay_race_test")

    setup_conn = await asyncpg.connect(DB_DSN)
    await reset_table(setup_conn)
    await setup_conn.close()


    # Test 2: the fixed version
    await run_concurrent(
        fixed_idempotent.create_payment, "pay_race_test", "FIXED idempotent version"
    )
    fixed_count = await count_rows("pay_race_test")

    print("\n --SUMMARY--")
    print(f"Broken version produced {broken_count} row(s) for the same key (expect 2 = BUG)")
    print(f"Fixed version produced {fixed_count} row(s) for the same key (expect 1 = CORRECT)")



if __name__ == "__main__":
    asyncio.run(main())