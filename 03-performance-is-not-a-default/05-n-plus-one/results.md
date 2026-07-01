# Results: N+1 Query Detection and Elimination

**Endpoints:** `/naive/{limit}` (N+1 pattern) vs `/fixed/{limit}` (single JOIN)
**Tables:** `transactions_five_indexes` (500,000 rows, Experiment 02) JOINed
against `accounts` (50,000 rows, this experiment)
**Instrumentation:** `pg_stat_statements` call-count delta, captured
before and after each request — a structural query count, not a timing.

---

## A methodology bug found and fixed before any results were trusted

The first version of `get_query_count()` read:

```sql
SELECT COALESCE(SUM(calls), 0) FROM pg_stat_statements
```

This produced a constant **+1 offset** on every measurement: naive/20
returned `22` instead of the expected `21`; fixed/20 returned `2`
instead of `1`.

**Root cause:** this is a textbook observer effect. PostgreSQL's
`pg_stat_statements` only records a statement's call count *after*
that statement completes. The "before" snapshot call to
`get_query_count()` is itself a SQL statement — call it Query A. When
Query A runs the first time (the "before" snapshot), its own
execution hasn't been recorded yet, so it doesn't see itself. But by
the time Query A runs a *second* time (the "after" snapshot), its
first invocation has completed and been logged — so the "after" call
sees the real application queries **plus its own prior self**,
inflating every delta by exactly one.

**Fix:** exclude the instrumentation query from its own count by
filtering on its own query text:

```sql
SELECT COALESCE(SUM(calls), 0) FROM pg_stat_statements
WHERE query NOT LIKE '%pg_stat_statements%'
```

After the fix, every measurement below was exact, with zero deviation
across every repeated run and every scale tested. This is the same
category of correction a CPU performance counter needs (excluding
the instruction that reads the counter) or a benchmarking harness
needs (subtracting its own sampling overhead) — self-referential
instrumentation always requires this kind of exclusion, in any
language or tool.

---

## Full dataset: query count vs result size

| `limit` | naive `query_count` | fixed `query_count` | naive formula check |
|---------|----------------------|------------------------|------------------------|
| 20 | 21 | 1 | 20 + 1 = 21 ✓ |
| 100 | 101 | 1 | 100 + 1 = 101 ✓ |
| 200 | 201 | — | 200 + 1 = 201 ✓ |
| 1,000 | — | 1 | — |
| 2,000 | 2,001 | — | 2,000 + 1 = 2,001 ✓ |
| 10,000 | — | 1 | — |
| 100,000 | — | 1 | — |

Every naive measurement matches `limit + 1` exactly. Every fixed
measurement is `1`, independent of `limit`, tested across a
1,000x range (100 to 100,000) with zero deviation.

```
❯ curl "http://localhost:8001/naive/20"
  "query_count": 21

❯ curl "http://localhost:8001/naive/100"
  "query_count": 101

❯ curl "http://localhost:8001/naive/200"
  "query_count": 201

❯ curl "http://localhost:8001/naive/2000"
  "query_count": 2001

❯ curl "http://localhost:8001/fixed/20"
  "query_count": 1

❯ curl "http://localhost:8001/fixed/100"
  "query_count": 1

❯ curl "http://localhost:8001/fixed/1000"
  "query_count": 1

❯ curl "http://localhost:8001/fixed/10000"
  "query_count": 1

❯ curl "http://localhost:8001/fixed/100000"
  "query_count": 1
```

---

## The mathematical relationship, stated precisely

**Naive:** `query_count(n) = n + 1` — linear, **O(n)** in the number of
results requested.

**Fixed:** `query_count(n) = 1` — constant, **O(1)**, independent of
result size, confirmed across a 1,000x range of `n`.

This is not an approximation or a trend line fitted to noisy data —
every single measurement matched the formula exactly, on every repeat.