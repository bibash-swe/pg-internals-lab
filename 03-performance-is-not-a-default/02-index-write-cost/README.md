# 02 — The Write Cost of Indexes

## The problem

Adding an index is usually framed as a pure tradeoff: faster reads,
slower writes. This experiment asks a sharper question: **slower how,
and is the obvious metric (throughput) even where the real cost shows up?**

Two structurally identical transaction tables, modeled on a real
payments ledger. One has zero secondary indexes. One has five — account
lookup, status filter, date range, a composite query index, and a
UNIQUE idempotency key — mirroring a realistic production access
pattern.

## What this lab proves

Raw UPDATE throughput barely changes (5.4% loss). That number is a
trap. The real cost is a **32x collapse in PostgreSQL's HOT (Heap-Only
Tuple) update optimization** — from 92.7% to 2.9% — caused specifically
by indexing the `status` column that gets updated on every write. This
produces 13.7x more dead tuples and 83% more disk usage for the same
workload, entirely invisible in throughput metrics.

| Metric | No index | Five indexes |
|--------|----------|---------------|
| Update throughput | 1,207/sec | 1,142/sec (-5.4%) |
| HOT update rate | 92.7% | 2.9% |
| Dead tuples (100k updates) | 7,135 | 97,643 |
| Table size | 64 MB | 117 MB |

Full real terminal output and analysis: [results.md](./results.md) and
[result_analysis.md](./result_analysis.md)

## Files

- `migration.sql` — creates both tables (one bare, one indexed post-load)
- `seed.py` — runs the full 4-phase benchmark: bulk insert, index build,
  individual updates, MVCC stats comparison
- `results.md` — real terminal output from every phase
- `result_analysis.md` — written analysis, including the HOT update
  mechanism and production fixes (separating volatile state, partial
  index limitations)

## How to run

```bash
# From 03-performance-is-not-a-default/, with the Docker container running
psql postgresql://postgres:postgres@localhost:5433/lab3 \
  -f 02-index-write-cost/migration.sql

cd 02-index-write-cost
python seed.py
```

Runtime: ~3 minutes (100,000 individual UPDATE statements per table,
intentionally not batched, to measure real per-statement overhead).

To inspect HOT update rates directly afterward:

```sql
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(100.0 * n_tup_hot_upd / NULLIF(n_tup_upd, 0), 1) AS hot_update_pct
FROM pg_stat_user_tables
WHERE relname IN ('transactions_no_index', 'transactions_five_indexes');
```