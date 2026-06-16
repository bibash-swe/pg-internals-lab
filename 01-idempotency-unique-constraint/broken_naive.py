"""
BROKEN: Naive check-then-act pattern.

This function contains a classic Time-of-Check to Time-of-Use (TOCTOU) race condition.
Under concurrent requests with the same idempotency key, both requests can pass the SELECT check
before either completes the INSERT, resulting in duplicate payment rows.

Reproduce it:
    Run two requests with same 'key' concurrently (e.g. via asyncio.gather or two separate processes
    hitting this function at the same time). Both will see 'existing = None' and both will proceed
    to INSERT.

See fixed_idempotent.py for the corrected version using a UNIQUE constraint and proper exception handling.
"""

import asyncpg

async def create_payment(db: asyncpg.Connection, key:str, amount:float):
    # Step 1: check if a payment with this key already exists.
    existing = await db.fetchrow(
        "SELECT id FROM payments WHERE idempotency_key = $1", key
    )
    # TOCTOU window
    # Two concurrent calls can both reach this point and both see 'existing = None',
    # because neither has committed an INSERT yet.
    if existing:
        return existing

    # Step 2: insert. Both concurrent requests can reach this line.
    # producing two payment rows for the same idempotency key.
    payment = await db.fetchrow(
        "INSERT INTO payments (idempotency_key, amount) VALUES ($1, $2) RETURNING *",
        key, amount
    )
    return payment



