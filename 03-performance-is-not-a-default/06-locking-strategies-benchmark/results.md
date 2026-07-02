# Results: Locking Strategies Benchmark

**Problem:** prevent duplicate payment processing under concurrency —
the same correctness requirement as Lab 01, benchmarked across five
distinct concurrency-control strategies.

**Concurrency:** 15 workers, pool size matched exactly to worker count
(see `benchmark.py` module docstring for why this equality matters —
it isolates lock contention from Experiment 04's connection-pool
contention).

**Two distinct benchmark shapes, measuring two different things:**
- Job-queue strategies (1, 2, 5): 500 pre-seeded rows, workers race to
  claim and complete all of them. Throughput = **rows of real work
  completed per second.**
- Insert-race strategies (3, 4): 8 shared idempotency keys, 15 workers
  × 20 rounds = 2,400 total attempts, of which exactly 8 can ever
  succeed. Throughput = **attempts absorbed per second under a
  retry-storm workload**, not useful-work throughput.

These two throughput numbers are **not directly comparable** to each
other — see `result_analysis.md`.

---

## Real terminal output

```
Experiment 06: Locking Strategies Benchmark
Database: postgresql://postgres:postgres@localhost:5433/lab3
Worker count: 15 (pool size matched exactly)
Job-queue rows per strategy: 500
Shared keys for insert-race strategies: 8
Rounds per worker (insert-race): 20

============================================================
STRATEGY: 1. SELECT ... FOR UPDATE (pessimistic, blocking)
============================================================
  Elapsed: 0.787s
  Total claimed (summed from workers): 500
  Rows marked 'completed' in DB: 500 (expected 500)
  Rows still 'pending': 0 (expected 0)
  Throughput: 635.0 claims/sec
  Correctness: PASS
  Per-worker claim distribution: [0, 0, 0, 0, 13, 21, 26, 26, 44, 51, 52, 55, 56, 74, 82]

============================================================
STRATEGY: 2. SELECT ... FOR UPDATE SKIP LOCKED (pessimistic, non-blocking)
============================================================
  Elapsed: 0.213s
  Total claimed (summed from workers): 500
  Rows marked 'completed' in DB: 500 (expected 500)
  Rows still 'pending': 0 (expected 0)
  Throughput: 2,350.3 claims/sec
  Correctness: PASS
  Per-worker claim distribution: [31, 32, 32, 33, 33, 33, 33, 33, 33, 34, 34, 34, 35, 35, 35]

============================================================
STRATEGY: 3. pg_advisory_xact_lock (advisory, blocking, row-independent)
============================================================
  Elapsed: 0.602s
  Total attempts: 2,400
  Total successful claims (summed from workers): 8 (expected 8)
  Rows in DB: 8 (expected 8)
  Throughput: 3,988.3 attempts/sec
  Correctness: PASS

============================================================
STRATEGY: 4. UNIQUE constraint (no explicit lock, Lab 01's pattern)
============================================================
  Elapsed: 0.375s
  Total attempts: 2,400
  Total successful claims (summed from workers): 8 (expected 8)
  Rows in DB: 8 (expected 8)
  Throughput: 6,395.2 attempts/sec
  Correctness: PASS

============================================================
STRATEGY: 5. Optimistic locking via version column (no lock, retry on conflict)
============================================================
  Elapsed: 1.306s
  Total claimed (summed from workers): 500
  Rows marked 'completed' in DB: 500 (expected 500)
  Rows still 'pending': 0 (expected 0)
  Throughput: 382.8 claims/sec
  Correctness: PASS
  Per-worker claim distribution: [20, 28, 29, 30, 30, 32, 33, 34, 34, 35, 35, 37, 39, 41, 43]

============================================================
SUMMARY
============================================================
  [PASS] 1. SELECT ... FOR UPDATE (pessimistic, blocking)
         0.787s, 635.0/sec
  [PASS] 2. SELECT ... FOR UPDATE SKIP LOCKED (pessimistic, non-blocking)
         0.213s, 2,350.3/sec
  [PASS] 3. pg_advisory_xact_lock (advisory, blocking, row-independent)
         0.602s, 3,988.3/sec
  [PASS] 4. UNIQUE constraint (no explicit lock, Lab 01's pattern)
         0.375s, 6,395.2/sec
  [PASS] 5. Optimistic locking via version column (no lock, retry on conflict)
         1.306s, 382.8/sec
```

---

## Summary Table

| # | Strategy | Elapsed | Throughput | Correct | Notable |
|---|----------|---------|------------|---------|---------|
| 1 | FOR UPDATE | 0.787s | 635.0 rows/sec | PASS | **4 of 15 workers claimed 0 rows** |
| 2 | FOR UPDATE SKIP LOCKED | 0.213s | 2,350.3 rows/sec | PASS | Near-perfectly even distribution |
| 3 | Advisory lock | 0.602s | 3,988.3 attempts/sec | PASS | Blocking, lighter than row locks |
| 4 | UNIQUE constraint | 0.375s | 6,395.2 attempts/sec | PASS | Zero coordination overhead |
| 5 | Optimistic (version) | 1.306s | 382.8 rows/sec | PASS | **Slowest strategy of all five** |

**Job-queue throughput ranking (real work, directly comparable):**
SKIP LOCKED (2,350) > FOR UPDATE (635) > Optimistic (383) — a **6.1x**
spread between fastest and slowest.

**Insert-race throughput ranking (attempt absorption, directly comparable
to each other only):** UNIQUE constraint (6,395) > Advisory lock (3,988)
— a **1.6x** difference.