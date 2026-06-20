"""
Reproduces write skew: two concurrent transactions each read the same
"is someone else on call?" state, each independently conclude it's
safe to go off-call, and both try to commit.

Under SERIALIZABLE isolation, PostgreSQL detects the read/write
dependency cycle and aborts one transaction with a serialization
failure. Under READ COMMITTED or REPEATABLE READ, no such detection
happens — both transactions succeed, violating the "at least one
doctor on call" invariant.

This script deliberately controls the interleaving (rather than
relying on asyncio.gather's scheduling) so the anomaly is reproduced
reliably on every run:

    1. Both transactions BEGIN and run their SELECT.
    2. Transaction A updates and commits.
    3. Transaction B updates and attempts to commit.
    4. We check whether B succeeded or was rejected.

Run:
    python test_write_skew.py

Requires a running PostgreSQL instance and the `on_call` table from
migration.sql.
"""
import os
import asyncio
import asyncpg

DB_DSN = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)


async def reset_table(conn: asyncpg.Connection):
    await conn.execute("DELETE FROM on_call")
    await conn.execute(
        "INSERT INTO on_call (doctor, is_on_call) VALUES ('Alice', true), ('Bob', true)"
    )


async def run_doctor_transaction(
    conn: asyncpg.Connection,
    isolation_level: str,
    doctor: str,
    started_event: asyncio.Event,
    proceed_event: asyncio.Event,
):
    """
    One doctor's transaction: BEGIN, read the on-call count, signal
    that the read is done, then wait to be told it's safe to proceed
    with the UPDATE + COMMIT. This lets us force both transactions to
    do their SELECT before either does its UPDATE/COMMIT — the exact
    interleaving that produces write skew.
    """
    tx = conn.transaction(isolation=isolation_level)
    await tx.start()

    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM on_call WHERE is_on_call = true"
        )
        print(f"  [{doctor}] sees {count} doctor(s) on call")

        started_event.set()
        await proceed_event.wait()

        await conn.execute(
            "UPDATE on_call SET is_on_call = false WHERE doctor = $1", doctor
        )

        await tx.commit()
        print(f"  [{doctor}] COMMIT succeeded — went off-call")
        return True

    except asyncpg.SerializationError as e:
        print(f"  [{doctor}] COMMIT FAILED — serialization error: {e}")
        await tx.rollback()
        return False

    except Exception as e:
        print(f"  [{doctor}] COMMIT FAILED — unexpected error: {e}")
        await tx.rollback()
        raise


async def run_scenario(isolation_level: str):
    print(f"\n--- Isolation level: {isolation_level} ---")

    setup_conn = await asyncpg.connect(DB_DSN)
    try:
        await reset_table(setup_conn)
    finally:
        await setup_conn.close()

    conn_alice = await asyncpg.connect(DB_DSN)
    conn_bob = await asyncpg.connect(DB_DSN)

    try:
        alice_started = asyncio.Event()
        bob_started = asyncio.Event()
        alice_proceed = asyncio.Event()
        bob_proceed = asyncio.Event()

        async def alice_flow():
            await run_doctor_transaction(
                conn_alice, isolation_level, "Alice", alice_started, alice_proceed
            )

        async def bob_flow():
            await run_doctor_transaction(
                conn_bob, isolation_level, "Bob", bob_started, bob_proceed
            )

        alice_task = asyncio.create_task(alice_flow())
        bob_task = asyncio.create_task(bob_flow())

        # Wait for BOTH to have done their SELECT before either proceeds
        # to UPDATE/COMMIT. This is the critical interleaving: neither
        # has seen the other's decision yet.
        await alice_started.wait()
        await bob_started.wait()

        alice_proceed.set()
        await alice_task  # let Alice fully commit first

        bob_proceed.set()
        await bob_task  # then Bob attempts to commit

        final = await conn_alice.fetch("SELECT doctor, is_on_call FROM on_call")
        on_call_count = sum(1 for row in final if row["is_on_call"])
        print(f"  Final state: {[dict(r) for r in final]}")
        print(f"  Doctors still on call: {on_call_count}")

        return on_call_count

    finally:
        await conn_alice.close()
        await conn_bob.close()


async def main():
    rc_count = await run_scenario("read_committed")
    rr_count = await run_scenario("repeatable_read")
    sz_count = await run_scenario("serializable")

    print("\n=== SUMMARY ===")
    print(f"READ COMMITTED   -> {rc_count} doctor(s) on call at the end (expect 0 = BUG)")
    print(f"REPEATABLE READ  -> {rr_count} doctor(s) on call at the end (expect 0 = BUG, still)")
    print(f"SERIALIZABLE     -> {sz_count} doctor(s) on call at the end (expect 1 = CORRECT)")


if __name__ == "__main__":
    asyncio.run(main())