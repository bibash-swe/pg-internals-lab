# Result Analysis: Write Cost of Indexes

**Tables:** `transactions_no_index` vs `transactions_five_indexes` — 500,000 rows each
**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, 1GB RAM, 1 CPU)

---

## The headline number is misleading

```
Individual UPDATE throughput:
  No index:     1,207 updates/sec
  Five indexes: 1,142 updates/sec
  Throughput loss: 5.4%
```

Taken alone, this says "indexes barely cost anything on writes." That
conclusion is wrong, and the rest of this analysis exists to show why.

---

## The Real Cost: HOT Updates Disabled, Not Raw Throughput

| Table | n_tup_upd | n_tup_hot_upd | HOT % |
|-------|-----------|----------------|-------|
| transactions_no_index | 100,000 | 92,714 | 92.7% |
| transactions_five_indexes | 100,000 | 2,937 | 2.9% |

A **32x difference** in HOT (Heap-Only Tuple) update rate is the real
mechanism behind everything else measured in this experiment.

PostgreSQL's HOT optimization lets an UPDATE rewrite a row in place on
the same heap page — without touching *any* index — as long as the
UPDATE doesn't change a column that any index depends on, and there's
room on the page. `transactions_no_index` has no secondary indexes at
all, so 92.7% of its updates qualified for this fast path automatically.

`transactions_five_indexes` has `idx_txn_status` directly indexing the
`status` column — the exact column being updated on every single write
in this benchmark. That disqualifies the row from HOT almost every
time, forcing the full path: new heap tuple, new entries written to
all five indexes, old heap tuple and all five old index entries left
as dead weight for VACUUM to clean up later.

This produced:
- **13.7x more dead tuples** (7,135 vs 97,643) for the same 100,000 updates
- **1.8x more disk usage** (64MB vs 117MB) for the same 500,000 rows
- A cost that is **completely invisible** in the throughput metric most
  dashboards and alerts are built around

**Production rule:** never put a B-tree index directly on a column that
changes on every write of a hot table, if it can be avoided. Index
columns you filter or sort by; avoid indexing columns you mutate
constantly on the same table.

---

## Fixing Write Amplification in PostgreSQL

### The architectural fix: separate the volatile state

Moving a highly volatile column like `status` into a dedicated,
separate table is the most robust production fix.

- The main transaction ledger becomes effectively append-only —
  its heavier composite indexes (`account_id`, `created_at`,
  `idempotency_key`) are never touched by status churn.
- The smaller status table absorbs all the high-velocity UPDATEs and
  can maintain a high HOT update ratio itself, since it carries little
  or no indexing of its own beyond a foreign key to the ledger row.

This is the same pattern your resume's ATS state machine implicitly
needed: separating "what happened" (immutable ledger) from "what state
is it in right now" (small, frequently-mutated pointer) is a recurring
production pattern for exactly this reason.

### The PostgreSQL "gotcha": partial indexes are not a full fix

A partial index — `CREATE INDEX ... WHERE status = 'pending'` — is a
genuinely good technique for reducing index size and I/O, since it only
indexes the small, actively-queried subset of rows instead of the whole
table. But it has a hidden limitation that matters here:

**Modifying any column referenced anywhere in an index definition —
including just the `WHERE` clause of a partial index — disqualifies
that row from HOT updates, exactly like a full index would.**

So a partial index on `status` still breaks HOT for every row whose
`status` changes, even though the index itself stays small. It solves
*index bloat* (the index file size stays low) but does **not** solve
*table bloat* (dead tuples on the main ledger keep accumulating at the
same rate, because the heap-level mechanism that disqualifies HOT
doesn't care how selective the index is — only whether the column is
referenced).

**Correct takeaway:** partial indexes are the right tool when your
goal is reducing index size and read I/O on a narrow, frequently-queried
subset. They are not a substitute for the architectural fix above when
the actual problem is write amplification from updating an indexed
column at high frequency.

---

## A secondary finding: idempotency key index cost

```
idx_txn_idempotency_unique          28 MB
transactions_five_indexes_pkey      11 MB
idx_txn_account_created             5000 kB
idx_txn_account_id                  4552 kB
idx_txn_status                      3408 kB
idx_txn_created_at                  3408 kB
```

The UNIQUE index on `idempotency_key` is the largest secondary index
in the table — larger than the primary key itself — despite indexing
the same 500,000 rows. The reason is data width: the idempotency key
is stored as `TEXT` holding a UUID string (~37 bytes per entry: `idem_`
prefix + 32 hex characters), versus the primary key's native 8-byte
`bigint`.

**Production implication:** if an idempotency key column is expected
to be queried constantly (as in Lab 01's UNIQUE-constraint pattern),
consider storing it as a native `UUID` type (16 bytes) rather than
`TEXT`, or using a shorter fixed-width token. The functional behavior
is identical — the index is smaller and cheaper to maintain purely
because of the underlying type's width.

---

## Core Thesis, Extended

> Performance is not a default state but a measurable compromise; every
> database decision — whether adding an index, pooling connections, or
> structuring a query — extracts a specific architectural cost that
> must be explicitly benchmarked and proven under load, rather than
> blindly assumed.

This experiment adds a sharper version of that thesis: **the cost of an
index is not always visible in the metric you'd naturally think to
check.** Throughput looked almost unaffected (5.4% loss). The real cost
— a 32x collapse in HOT update rate, 13.7x more dead tuples, 83% more
disk usage — was invisible until `pg_stat_user_tables` was queried
directly. A team monitoring only request latency or updates/sec would
have shipped this exact schema to production, watched it work fine for
weeks, and then been blindsided by autovacuum struggling to keep up and
disk usage climbing for reasons that wouldn't show up in any APM
dashboard built around throughput alone.