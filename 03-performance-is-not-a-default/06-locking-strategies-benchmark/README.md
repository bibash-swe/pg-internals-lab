# 06 — Locking Strategies Benchmark

## The problem

Same correctness requirement as [Lab 01](../01-idempotency-unique-constraint):
prevent duplicate payment processing under concurrency. This time, five
distinct PostgreSQL concurrency-control mechanisms are benchmarked
against each other, all solving the identical problem, under matched
concurrency (15 workers, pool size fixed to exactly 15 so connection
contention from Experiment 04 never leaks into these results).

## What this lab proves

Every strategy achieved correctness. Throughput and fairness diverged
dramatically, and not always in the direction textbook intuition
would predict.

| # | Strategy | Throughput | Notable finding |
|---|----------|------------|-------------------|
| 1 | `FOR UPDATE` | 635 rows/sec | 4 of 15 workers claimed **zero** rows — no fairness guarantee |
| 2 | `FOR UPDATE SKIP LOCKED` | 2,350 rows/sec | Fastest *and* most fair — near-perfectly even worker distribution |
| 3 | Advisory lock | 3,988 attempts/sec | Blocking, real coordination overhead per attempt |
| 4 | UNIQUE constraint | 6,395 attempts/sec | 1.6x faster than advisory — zero coordination overhead |
| 5 | Optimistic (version column) | 383 rows/sec | **Slowest of all five** — worse than blocking, under this benchmark's high, narrow contention |

Headline finding: optimistic locking — often assumed to be the
"modern," lock-free default — was the single slowest strategy tested,
because this benchmark deliberately concentrates all workers onto the
same contended row, close to worst-case for optimistic concurrency
control. Full mechanism, and when each strategy actually wins, in
[result_analysis.md](./result_analysis.md).

## Files

- `migration.sql` — five structurally isolated payment tables, one per
  strategy, plus a note on why indexes are declared inline here
  (unlike Experiments 01/02) given this experiment's row counts
- `seed.py` — seeds job-queue-shaped tables with pending rows; leaves
  insert-race tables empty by design (see module docstring)
- `benchmark.py` — five worker implementations, one per strategy,
  orchestrated with matched concurrency and correctness verification
- `results.md` — real terminal output, all five strategies
- `result_analysis.md` — the fairness finding, the optimistic-locking
  result explained, and a production decision guide derived from the
  data

## How to run

```bash
# From 03-performance-is-not-a-default/, with the Docker container running
psql postgresql://postgres:postgres@localhost:5433/lab3 \
  -f 06-locking-strategies-benchmark/migration.sql

cd 06-locking-strategies-benchmark
python seed.py
python benchmark.py
```

Runtime: a few seconds total — this benchmark measures contention
behavior, not bulk throughput at scale, so it completes quickly by
design.