# Results: Write Cost of Indexes

**Tables:** `transactions_no_index` vs `transactions_five_indexes` — 500,000 rows each
**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, 1GB RAM, 1 CPU)
**Updates per table:** 100,000 individual UPDATE statements (not bulk)

> `job_listings` (361MB, from Experiment 01) remained present in the same
> database during this run, contributing ambient memory pressure consistent
> with a real production instance hosting multiple tables.

---

## Phase 1 — Bulk INSERT (before any secondary indexes exist)

```
Inserting 500,000 rows into transactions_no_index...
  500,000 rows in 2.71s (184,315 rows/sec)

Inserting 500,000 rows into transactions_five_indexes...
  500,000 rows in 2.81s (177,841 rows/sec)
```

Both tables had only their primary key during this phase — the five
secondary indexes are built afterward (Phase 2), matching the correct
production pattern proven in Experiment 01 (build indexes post-load,
not maintain them during bulk load).

---

## Phase 2 — Build five indexes on `transactions_five_indexes`

```
idx_txn_account_id:          0.25s
idx_txn_status:               0.12s
idx_txn_created_at:           0.14s
idx_txn_account_created:      0.23s
idx_txn_idempotency_unique:   1.25s
```

Total: 1.99s to build all five indexes on 500,000 rows in a single
sorted pass each — confirming the same finding from Experiment 01.

---

## Phase 3 — Individual UPDATEs (100,000 per table, post-index)

```
Running 100,000 individual UPDATEs on transactions_no_index...
  20,000 / 100,000 updates (1,175 updates/sec)
  40,000 / 100,000 updates (1,204 updates/sec)
  60,000 / 100,000 updates (1,203 updates/sec)
  80,000 / 100,000 updates (1,204 updates/sec)
  100,000 / 100,000 updates (1,207 updates/sec)
  100,000 updates in 82.84s (1,207 updates/sec)

Running 100,000 individual UPDATEs on transactions_five_indexes...
  20,000 / 100,000 updates (1,150 updates/sec)
  40,000 / 100,000 updates (1,163 updates/sec)
  60,000 / 100,000 updates (1,155 updates/sec)
  80,000 / 100,000 updates (1,151 updates/sec)
  100,000 / 100,000 updates (1,142 updates/sec)
  100,000 updates in 87.55s (1,142 updates/sec)
```

Raw throughput loss: 5.4% (1,207 → 1,142 updates/sec). See
`result_analysis.md` for why this number is misleading on its own.

---

## Phase 4 — MVCC stats (dead tuples and table size)

```
No index:     7,135 dead tuples, table size 64 MB
Five indexes: 97,643 dead tuples, table size 117 MB
```

13.7x more dead tuples, 1.8x more disk usage, for the same 100,000
updates against the same row count.

---

## Phase 5 — HOT update rate (the real finding)

```sql
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(100.0 * n_tup_hot_upd / NULLIF(n_tup_upd, 0), 1) AS hot_update_pct
FROM pg_stat_user_tables
WHERE relname IN ('transactions_no_index', 'transactions_five_indexes');
```

```
          relname          | n_tup_upd | n_tup_hot_upd | hot_update_pct
----------------------------+-----------+----------------+----------------
 transactions_no_index      |    100000 |          92714 |           92.7
 transactions_five_indexes  |    100000 |           2937 |            2.9
```

32x difference in HOT update rate. This is the actual mechanism behind
the dead tuple and disk size differences above — see `result_analysis.md`.

---

## Final schema confirmation

```
lab3=# \d transactions_no_index
                                         Table "public.transactions_no_index"
     Column      |           Type           | Collation | Nullable |                      Default
-----------------+--------------------------+-----------+----------+----------------------------------------------------
 id              | bigint                   |           | not null | nextval('transactions_no_index_id_seq'::regclass)
 account_id      | integer                  |           | not null |
 idempotency_key | text                     |           | not null |
 amount          | numeric(12,2)            |           | not null |
 currency        | character(3)             |           | not null |
 status          | text                     |           | not null |
 created_at      | timestamp with time zone |           | not null | now()
Indexes:
    "transactions_no_index_pkey" PRIMARY KEY, btree (id)

lab3=# \d transactions_five_indexes
                                         Table "public.transactions_five_indexes"
     Column      |           Type           | Collation | Nullable |                        Default
-----------------+--------------------------+-----------+----------+---------------------------------------------------------
 id              | bigint                   |           | not null | nextval('transactions_five_indexes_id_seq'::regclass)
 account_id      | integer                  |           | not null |
 idempotency_key | text                     |           | not null |
 amount          | numeric(12,2)            |           | not null |
 currency        | character(3)             |           | not null |
 status          | text                     |           | not null |
 created_at      | timestamp with time zone |           | not null | now()
Indexes:
    "transactions_five_indexes_pkey" PRIMARY KEY, btree (id)
    "idx_txn_account_created" btree (account_id, created_at)
    "idx_txn_account_id" btree (account_id)
    "idx_txn_created_at" btree (created_at)
    "idx_txn_idempotency_unique" UNIQUE, btree (idempotency_key)
    "idx_txn_status" btree (status)
```

---

## Index sizes

```
idx_txn_idempotency_unique          28 MB
transactions_five_indexes_pkey      11 MB
idx_txn_account_created             5000 kB
idx_txn_account_id                  4552 kB
idx_txn_status                      3408 kB
idx_txn_created_at                  3408 kB
```

`idx_txn_idempotency_unique` is the largest secondary index despite
indexing the same number of rows as the others — because it indexes a
`TEXT` UUID (~37 bytes/entry) rather than a narrow integer or timestamp
(4–8 bytes/entry). See `result_analysis.md` for the production
implication.

---

## Summary Table

| Metric | No index | Five indexes | Difference |
|--------|----------|---------------|------------|
| Bulk INSERT (500k rows) | 184,315 rows/sec | 177,841 rows/sec | -3.5% |
| Individual UPDATE (100k) | 1,207 updates/sec | 1,142 updates/sec | -5.4% |
| Dead tuples after updates | 7,135 | 97,643 | **+1,268%** |
| Table size after updates | 64 MB | 117 MB | **+83%** |
| HOT update rate | 92.7% | 2.9% | **-89.8 points** |