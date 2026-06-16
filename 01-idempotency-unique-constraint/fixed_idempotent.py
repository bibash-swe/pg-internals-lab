import asyncpg

async def create_payment(db: asyncpg.Connection, key: str, amount: float):
    """
    FIXED: Closes the TOCTOU window using the database's UNIQUE constraint as the source of truth, instead of
    an application-level check-then-act.

    Handles the MVCC edge case: a UniqueViolationError does not guarantee a committed row exists - the conflicting
    transaction may have rolled back. In that case, the SELECT returns None, and we retry the insert.
    """
    try:
        payment = await db.fetchrow(
            "INSERT INTO payments (idempotency_key, amount) VALUES ($1, $2) RETURNING *", key, amount
        )
        return payment

    except asyncpg.UniqueViolationError:
        existing = await db.fetchrow(
            "SELECT * FROM payments WHERE idempotency_key = $1", key
        )
        if existing is not None:
            return existing

        # The confliction transaction is rolled back.
        # We are now the only writer for this key, safe to retry.
        return await db.fetchrow(
            "INSERT INTO payments (idempotency_key, amount) VALUES ($1, $2) RETURNING *", key, amount
        )