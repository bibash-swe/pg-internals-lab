# 05 — N+1 Query Detection and Elimination

## The problem

Fetching a list of items and then fetching related data per item — a
transaction feed showing each account holder's name, for instance —
can be written two structurally different ways that return identical
data. One of them scales. One of them doesn't.

## What this lab proves

Two endpoints, same returned JSON, instrumented with `pg_stat_statements`
call-count deltas (a structural query count, not a timing) to prove
exactly how many database round-trips each pattern costs:

- **`/naive/{limit}`** — fetches the list, then loops fetching each
  item's related row individually: `query_count(n) = n + 1`
- **`/fixed/{limit}`** — a single JOIN: `query_count(n) = 1`, proven
  constant across a 1,000x range of `n` (100 to 100,000)

| `limit` | naive queries | fixed queries |
|---------|-----------------|-----------------|
| 20 | 21 | 1 |
| 100 | 101 | 1 |
| 1,000 | 1,001 (extrapolated) | 1 |
| 100,000 | 100,001 (extrapolated) | 1 |

This is an O(n) vs O(1) relationship in database round-trips, not just
a slow-vs-fast comparison — the naive endpoint's cost grows without
bound as the underlying dataset grows, invisible in code review and
invisible in a sparse dev/staging environment.

Full real output, the exact formula proof, and a documented
observer-effect bug (found and fixed in the measurement tooling
itself) are in [results.md](./results.md) and
[result_analysis.md](./result_analysis.md).

## Files

- `migration.sql` — creates `accounts` (50,000 rows), the table
  `transactions_five_indexes.account_id` logically references
- `seed.py` — seeds accounts via COPY, verifies zero orphaned
  foreign keys against Experiment 02's transaction data
- `app.py` — FastAPI app with both endpoints, instrumented via
  `pg_stat_statements` deltas
- `results.md` — real query-count output across six orders of magnitude
- `result_analysis.md` — the O(n) vs O(1) analysis, the observer-effect
  finding, and production implications

## How to run

```bash
# From 03-performance-is-not-a-default/, with the Docker container running
psql postgresql://postgres:postgres@localhost:5433/lab3 \
  -f 05-n-plus-one/migration.sql

cd 05-n-plus-one
python seed.py
uvicorn app:app --host 0.0.0.0 --port 8001
```

In another terminal, reset stats for a clean measurement baseline,
then compare query counts directly:

```bash
psql postgresql://postgres:postgres@localhost:5433/lab3 \
  -c "SELECT pg_stat_statements_reset();"

curl "http://localhost:8001/naive/100" | python3 -m json.tool
curl "http://localhost:8001/fixed/100" | python3 -m json.tool
```

Compare the `query_count` field in each response.