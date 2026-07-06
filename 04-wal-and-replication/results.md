# Results: WAL and Replication

**Setup:** primary/replica PostgreSQL 16 streaming replication via
Docker Compose. `synchronous_standby_names = 'replica1'` confirmed
active (`sync_state = sync`) before either experiment ran.

---

## Real terminal output — Experiment A (Replication Lag)

```
Experiment A: Replication Lag Under Write Load
Primary: postgresql://postgres:postgres@localhost:5434/wal_lab
Replica: postgresql://postgres:postgres@localhost:5435/wal_lab

============================================================
TRICKLE TEST: 200 individual writes, lag measured per write
============================================================
  50/200 writes measured (latest lag: 2.28ms)
  100/200 writes measured (latest lag: 1.93ms)
  150/200 writes measured (latest lag: 2.06ms)
  200/200 writes measured (latest lag: 2.05ms)

  Trickle lag distribution (ms):
    min=1.709  p50=2.168  p95=3.129  p99=5.681  max=9.33  mean=2.335

============================================================
BURST TEST: 5,000 rows in one batch, measuring replica catch-up time
============================================================
  Primary finished accepting burst in 14.0ms
  Replica fully caught up 3.8ms AFTER primary finished writing
  Total time (write + full replication): 17.8ms

============================================================
SUMMARY
============================================================
  Trickle (per-write lag, ms):
    p50=2.168  p95=3.129  p99=5.681  max=9.33
  Burst (5,000 rows):
    primary write time: 14.0ms
    replica catch-up after: 3.8ms
    total: 17.8ms
```

---

## Real terminal output — Experiment B (Sync vs Async Commit)

```
Experiment B: Synchronous vs Asynchronous Commit Latency
Primary: postgresql://postgres:postgres@localhost:5434/wal_lab
Samples per mode: 200
Confirmed: replica1 sync_state = sync -- proceeding.

============================================================
MODE: synchronous_commit = off
============================================================
  Commit latency (ms) under synchronous_commit=off:
    min=0.256  p50=0.395  p95=0.683  p99=1.247  max=1.466  mean=0.433

============================================================
MODE: synchronous_commit = on
============================================================
  Commit latency (ms) under synchronous_commit=on:
    min=1.444  p50=1.774  p95=2.73  p99=3.598  max=3.768  mean=1.88

============================================================
SUMMARY
============================================================
  Metric         async (off)       sync (on)     overhead
  ---------- --------------- --------------- ------------
  p50_ms               0.395           1.774      +1.379ms
  p95_ms               0.683           2.730      +2.047ms
  p99_ms               1.247           3.598      +2.351ms
  mean_ms              0.433           1.880      +1.447ms
```

---

## Summary Table

| Measurement | Value |
|-------------|-------|
| Trickle lag, per single row (p50) | 2.168ms |
| Trickle lag, per single row (p99) | 5.681ms |
| Burst total time, 5,000 rows | 17.8ms |
| Burst per-row equivalent lag | 0.00356ms |
| **Per-row cost ratio, trickle vs burst** | **~609x** |
| Async commit latency (p50) | 0.395ms |
| Sync commit latency (p50) | 1.774ms |
| Sync overhead (p50) | +1.379ms (~4.5x) |
| Sync overhead (p99) | +2.351ms (~2.9x) |