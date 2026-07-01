"""
app.py — Experiment 05: N+1 Query Detection and Elimination

Two endpoints returning identical data -- the last N transactions,
each annotated with its account holder's name -- built two different
ways:

  GET /naive/{limit}  -> 1 query for transactions, then 1 additional
                         query PER transaction to fetch its account's
                         holder_name. For limit=20, this fires 21
                         total queries.

  GET /fixed/{limit}  -> 1 single query with a JOIN. Always exactly
                         1 query, regardless of limit.

The Rust parallel worth holding onto: this is structurally identical
to the difference between calling a syscall once per item in a loop
versus batching that work into a single vectored syscall (e.g.
writev() instead of N separate write() calls). No compiler --
Python's or Rust's -- stops you from writing the N-calls version. The
cost is invisible in the source code and only shows up as measured
latency and, more importantly here, as a measured QUERY COUNT -- a
structural fact about the program's behavior, not a timing that can
vary by machine load.

Both endpoints are instrumented to report the actual number of
queries PostgreSQL executed, using pg_stat_statements deltas captured
before and after each request. This is a stronger proof than wall-
clock timing alone: query count is deterministic and reproducible,
whereas timing has natural variance.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8001

(Port 8001, not 8000, so this can run alongside app.py from
Experiment 04 if both are needed simultaneously.)

Requires the Docker container (lab3, port 5433) with:
  - transactions_five_indexes (Experiment 02)
  - accounts (Experiment 05, seed.py)
"""
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=10)
    print("Pool started.")
    yield
    await pool.close()
    print("Pool closed.")


app = FastAPI(
    title="Experiment 05: N+1 Query Detection",
    lifespan=lifespan,
)


async def get_query_count(conn) -> int:
    """
    Sum of calls across all statements currently tracked by
    pg_stat_statements. We snapshot this before and after a request
    and report the delta -- the number of queries THIS request
    actually caused PostgreSQL to execute.

    CRITICAL: this query excludes itself via the WHERE clause below.
    Without that exclusion, this is a textbook observer effect: the
    "before" call to this function gets recorded by pg_stat_statements
    as a completed statement (since pg_stat_statements only records a
    call AFTER it finishes), and the "after" call would then see that
    prior self-invocation as part of the delta -- silently adding a
    constant +1 to every measurement.
    """
    return await conn.fetchval(
        "SELECT COALESCE(SUM(calls), 0) FROM pg_stat_statements "
        "WHERE query NOT LIKE '%pg_stat_statements%'"
    )


@app.get("/naive/{limit}")
async def naive_transactions_with_account(limit: int):
    """
    THE N+1 PATTERN.

    Fetches `limit` transactions with one query, then loops over
    them firing one additional query per row to look up the account
    holder's name. This is the pattern that emerges naturally when
    an engineer writes "get the transactions, then for each one get
    its account" without thinking about it as a single relational
    query -- exactly how an ORM's lazy-loaded relationship attribute
    produces N+1 without the engineer writing an explicit loop at all.
    """
    async with pool.acquire() as conn:
        before = await get_query_count(conn)

        transactions = await conn.fetch(
            "SELECT id, account_id, amount, status "
            "FROM transactions_five_indexes "
            "ORDER BY id DESC LIMIT $1",
            limit
        )

        results = []
        for txn in transactions:
            # ONE QUERY PER ROW. This is the N in N+1.
            account = await conn.fetchrow(
                "SELECT holder_name FROM accounts WHERE id = $1",
                txn["account_id"]
            )
            results.append({
                "id": txn["id"],
                "account_id": txn["account_id"],
                "amount": float(txn["amount"]),
                "status": txn["status"],
                "holder_name": account["holder_name"] if account else None,
            })

        after = await get_query_count(conn)

    return {
        "pattern": "naive_n_plus_1",
        "limit": limit,
        "query_count": after - before,
        "results": results,
    }


@app.get("/fixed/{limit}")
async def fixed_transactions_with_account(limit: int):
    """
    THE JOIN FIX.

    One query. PostgreSQL's planner handles the relational lookup
    internally, using accounts' primary key index (accounts_pkey) to
    resolve each account_id -- the same B-tree lookup mechanism
    studied in Experiment 01, just invoked once by the query planner
    instead of `limit` times by application code.
    """
    async with pool.acquire() as conn:
        before = await get_query_count(conn)

        rows = await conn.fetch("""
            SELECT t.id, t.account_id, t.amount, t.status, a.holder_name
            FROM transactions_five_indexes t
            JOIN accounts a ON a.id = t.account_id
            ORDER BY t.id DESC
            LIMIT $1
        """, limit)

        after = await get_query_count(conn)

    results = [
        {
            "id": r["id"],
            "account_id": r["account_id"],
            "amount": float(r["amount"]),
            "status": r["status"],
            "holder_name": r["holder_name"],
        }
        for r in rows
    ]

    return {
        "pattern": "fixed_join",
        "limit": limit,
        "query_count": after - before,
        "results": results,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "pool_size": pool.get_size() if pool else None}