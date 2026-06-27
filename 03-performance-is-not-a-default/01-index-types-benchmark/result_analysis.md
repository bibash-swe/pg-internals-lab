# The Index Lie: What a 1M-Row PostgreSQL Benchmark Actually Proves

There is an implicit contract in database engineering. You add an index, queries get faster. Everyone learns this on day one. It is repeated in every tutorial, every onboarding doc, every "PostgreSQL performance tips" blog post written since 2008.

The contract is a lie.

Not always. Not even usually. But often enough — under memory pressure, at the wrong selectivity, on the wrong data distribution — that treating it as a law is how you end up shipping a production incident with an `EXPLAIN ANALYZE` printout in your hand wondering why your index made things *worse*.

This post is a record of a benchmark I ran against a 1-million-row `job_listings` table in a constrained PostgreSQL 16 container: 256MB `shared_buffers`, 1GB RAM, 1 CPU. The constraints were deliberate. Production databases are not unlimited. This setup was tuned to expose the gaps between theory and reality at a scale where the numbers are still readable.

Every finding below has a measurement behind it. No assertions without evidence.

---

## The Setup

The table is `job_listings`. One million rows. Five index types tested: B-tree, Hash, GIN, BRIN, and a covering index.

Cold cache was enforced via LRU eviction — a 300MB temp table was written before each cold test to thrash the buffer pool. Warm cache results are p50/p95/p99 across 20 runs with the first excluded.

The complete summary:

| Scenario | Cold | p50 | p95 | p99 | Rows returned |
|---|---|---|---|---|---|
| Sequential Scan (no index) | 178ms | 254ms | 280ms | 282ms | 337,458 |
| B-tree (salary_min range) | 260ms | 228ms | 311ms | 361ms | 337,458 |
| Hash (company_id equality) | 0.35ms | 0.44ms | 1.97ms | 2.99ms | 182 |
| B-tree (same equality query) | 0.31ms | 0.37ms | 0.51ms | 0.53ms | 182 |
| GIN (tags array contains) | 19ms | 19ms | 22ms | 22ms | 15,902 |
| Seq scan (tags, no GIN) | 152ms | 198ms | 283ms | 291ms | 15,902 |
| BRIN (created_at range) | 42ms | 145ms | 168ms | 171ms | 200,000 |
| Covering index (index-only scan) | 47ms | 222ms | 280ms | 289ms | 375,168 |

Keep this table open in a tab. Every section below circles back to it.

---

## Proof 1: The B-Tree That Made Things Worse

**The assumption:** An index on `salary_min` will speed up a range query on `salary_min`.

**The query:**

```sql
SELECT id, title, salary_min, company_id
FROM job_listings
WHERE salary_min BETWEEN 100000 AND 140000
  AND is_active = true;
```

**The reality:** Sequential scan — 178ms cold. B-tree index — 260ms cold. The index was 46% *slower*.

This is the most counter-intuitive result in the benchmark, and it is the most important one to internalize.

PostgreSQL chose a Bitmap Index Scan — the correct strategy for this selectivity. The index scan found the right range in the B-tree, assembled a bitmap of matching row locations, then executed a Bitmap Heap Scan to fetch those rows from the heap. That Heap Scan touched 30,467 pages. The sequential scan touched 27,868. Same data volume, but the B-tree path accessed those pages in non-sequential order — random I/O — while the sequential scan read them in physical disk order — sequential I/O.

Sequential I/O on a spinning disk, or even on a page cache under memory pressure, is faster than random I/O. The index did not reduce disk work. It reorganised it in a way that made it slower.

Why did the planner choose the index? The cost model predicted it would win. It was wrong. The planner's estimates are based on statistics, not clairvoyance.

The second problem is selectivity. The query returned 337,458 rows out of 1,000,000. That is 33.7% of the table. And `salary_min` has only 8 distinct values. A B-tree on a column with 8 distinct values is navigating a structure that immediately collapses into massive bucket sizes. The index found the range boundary in microseconds — then had to scatter-gather 337,000 rows across 30,000 heap pages.

**The 10% rule:** If a range query returns more than roughly 10% of the table, a B-tree index is likely to lose to a sequential scan on a cold cache. The planner tries to model this, but it does not always get it right. Before indexing any column for a range query, estimate selectivity first.

Run this before you create the index:

```sql
SELECT
  count(*) FILTER (WHERE salary_min BETWEEN 100000 AND 140000) AS matches,
  count(*) AS total,
  round(
    100.0 * count(*) FILTER (WHERE salary_min BETWEEN 100000 AND 140000) / count(*), 2
  ) AS selectivity_pct
FROM job_listings;
```

If `selectivity_pct` comes back above 10, stop. Ask whether the query can be made more selective. Ask whether the query pattern even needs an index. A sequential scan that PostgreSQL expects to run infrequently is often the right answer.

If you still add the index anyway, verify with real execution:

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT id, title, salary_min, company_id
FROM job_listings
WHERE salary_min BETWEEN 100000 AND 140000
  AND is_active = true;
```

Look at `Buffers: shared read=N` in the output. If that number is close to the heap's total page count, you have a low-selectivity problem no index can solve.

---

## Proof 2: Hash Indexes Are a Museum Exhibit

**The assumption:** Hash indexes are O(1) equality lookups. For an equality query on `company_id`, Hash should beat B-tree.

**The query:**

```sql
SELECT id, title, salary_min
FROM job_listings
WHERE company_id = 42;
```

**The reality:**

| Metric | Hash | B-tree |
|---|---|---|
| Cold | 0.35ms | 0.31ms |
| p50 | 0.44ms | 0.37ms |
| p95 | 1.97ms | 0.51ms |
| p99 | 2.99ms | 0.53ms |
| Planning time | 1.51ms | 0.19ms |

B-tree won on every single metric. Faster cold. Faster p50. Dramatically more consistent at p95 and p99.

The p99 gap is the story: 2.99ms vs 0.53ms. That is a 5.6x difference in your worst-case latency for a simple equality lookup. The theoretical O(1) never materialised.

Here is why. Hash indexes suffer from bucket overflow chains. When multiple keys hash to the same bucket — and with 1M rows, they will — the index must traverse a linked list of overflow pages to find the actual match. That overflow chain traversal is unpredictable. Most lookups skip it. Some do not. The ones that do not are your p99 spikes.

B-tree does not have this problem. Its balanced tree structure gives you O(log n) with a bounded constant. Every lookup traverses the same depth. The variance is near-zero.

Hash also paid a higher planning penalty: 1.51ms of query planning vs 0.19ms for B-tree. Hash statistics are less rich than B-tree's, so the planner does more work at planning time.

The final nail: Hash cannot serve range queries, ORDER BY optimisations, or multi-column indexes. B-tree does all of these. You give up capability for zero measurable gain.

This is a PostgreSQL-specific finding. Other databases implement hash indexes with different tradeoffs. In PostgreSQL, as of 2026, there is no practical production use case where Hash beats B-tree. It is a historical artifact that was not even crash-safe before PostgreSQL 10.

**The rule:** Default to B-tree for every equality and range lookup. If someone suggests Hash in a code review, ask them to show you a benchmark where it wins at p99. They will not find one.

---

## Proof 3: GIN Is Not Optional for Array Columns

**The assumption:** An array column can be queried with a sequential scan. It is slow but it works.

**The query:**

```sql
SELECT id, title, company_id
FROM job_listings
WHERE tags @> ARRAY['python', 'postgresql'];
```

**The reality:**

| Scenario | Cold | p50 | p95 |
|---|---|---|---|
| GIN index | 19ms | 19ms | 22ms |
| Sequential scan | 152ms | 198ms | 283ms |

8x improvement. The largest delta in the benchmark.

But the raw numbers undersell the actual finding. Look at the p95 spread: GIN goes from 19ms to 22ms — a 16% variance. Sequential scan goes from 198ms to 283ms — a 43% variance. GIN is not just faster; it is *stable*. The sequential scan's results blow up under memory pressure because it has to read the entire heap every time. GIN's 5.5MB index fits in `shared_buffers` and stays there.

The GIN execution path is:
1. Look up two posting lists in the 5.5MB inverted index: 6.6ms.
2. Intersect the posting lists to find row locations matching both `'python'` AND `'postgresql'`.
3. Visit 12,456 heap pages to fetch the 15,902 result rows: ~12ms.

Total: 19ms. Every time. Because step 1 is always a 5.5MB in-memory lookup.

The sequential scan with three parallel workers touched 29,720 + 792 buffer pages. It cannot be made faster without adding more hardware or changing the query.

There is no B-tree workaround for `@>`. B-tree cannot express array containment. If you have a table with an array column and no GIN index, you are doing a full table scan on every `@>` query, always, forever, no matter how many B-trees you add. This is not a tradeoff — it is a missing capability.

The same applies to `JSONB` columns queried with `@>` or `?`, and to full-text search on `tsvector` columns.

The rule is simple: any column queried with `@>`, `?`, `@@`, or `@?` needs a GIN index. Add it before the table hits 100,000 rows so you never feel the pain of rebuilding it under load.

```sql
CREATE INDEX CONCURRENTLY idx_tags_gin ON job_listings USING GIN (tags);
```

`CONCURRENTLY` is not optional in production. It lets the build proceed without holding a table lock that blocks writes.

---

## Proof 4: BRIN Is a Skip Hint, Not a Lookup

**The assumption:** BRIN is a lightweight index for range queries on sequential columns. It should be fast.

**The query:**

```sql
SELECT id, title, created_at
FROM job_listings
WHERE created_at >= '2022-01-01'
  AND created_at < '2023-01-01';
```

**The reality:**

| Metric | Value |
|---|---|
| Cold | 42ms |
| p50 (warm) | 145ms |
| p95 (warm) | 168ms |
| Index size | 24 KB |
| Rows returned | 200,000 |
| Heap Blocks (lossy) | 6,272 |
| Rows removed by recheck | 7,929 |

Warm p50 is 145ms. The equivalent B-tree would be faster at pinpoint lookups. So what is BRIN actually doing?

The two most important lines in the `EXPLAIN` output are:

```
Heap Blocks: lossy=6272
Rows Removed by Index Recheck: 7929
```

`lossy=6272` means BRIN identified 6,272 heap pages whose block range *summaries* overlap the 2022 date range. BRIN stores only the min and max value for every 128 consecutive heap pages — it cannot identify individual rows. It can only say: "check this entire 128-page block because its min/max range overlaps your predicate."

After PostgreSQL fetches those 6,272 pages, it must recheck every row against the actual timestamp predicate. That recheck discarded 7,929 rows — false positives from BRIN's approximation. The recheck is not optional. BRIN is *lossy by design*.

So why 145ms warm when the index is only 24KB? Because BRIN still required 6,272 heap page reads. The index eliminated roughly 80% of the table (from ~32,000 total pages to 6,272), but it could not be more precise than that. Those 6,272 pages had to be fetched and scanned.

The B-tree on the same column would be 6.7MB and would pinpoint exactly the rows in the 2022 range without lossy blocks. But it is 280x larger.

BRIN is the correct choice when three conditions are all true simultaneously:

1. The column correlates with physical storage order (rows are inserted in chronological order, so `created_at` rows are physically adjacent on disk).
2. The table is large enough that 280x index size reduction matters.
3. Queries scan wide date ranges rather than pinpoint rows.

Condition 1 is not automatic. Check it before creating any BRIN index:

```sql
SELECT correlation
FROM pg_stats
WHERE tablename = 'job_listings'
  AND attname = 'created_at';
```

A value close to `1.0` means physical storage order matches column order — BRIN will work well. A value below `0.5` means BRIN degrades toward a full scan. At that point, use B-tree.

An event log table that only ever appends rows will have a `created_at` correlation near 1.0. A `job_listings` table where jobs are backdated, edited, or randomly reordered will not. Check the number. Do not assume.

For audit logs, time-series tables, and append-only event streams: BRIN. For everything else queried by timestamp: B-tree.

---

## Proof 5: The Covering Index That Was Perfect and Still Slow

**The assumption:** An index-only scan eliminates heap access. Zero heap fetches means maximum speed.

**The query:**

```sql
SELECT id, title, salary_min, location
FROM job_listings
WHERE salary_min BETWEEN 80000 AND 120000
  AND is_active = true;
```

**The result:**

```
Index Only Scan using idx_salary_covering
Heap Fetches: 0
Buffers: shared hit=1 read=2652
```

`Heap Fetches: 0`. A perfect index-only scan. The query never touched the heap.

**Warm p50: 222ms.** Nearly identical to the sequential scan baseline.

The covering index is 55MB. Under 256MB `shared_buffers`, it must compete for buffer space with the 257MB heap table, other indexes, and everything else PostgreSQL has cached. Each warm run required reading 2,652 pages from the 55MB index — pages that had been partially evicted by the cache thrash and other benchmark activity. Zero heap fetches did not help when the index pages themselves kept getting evicted.

The algorithm was optimal. The hardware assumption was wrong.

A covering index stores every included column's data in every index entry. The `INCLUDE (title, location)` clause shoved large `TEXT` fields into an index that was already 6.8MB without them. Multiply 1M rows by average string sizes and you get 55MB of index. On an instance with 8GB+ `shared_buffers`, that 55MB sits in memory permanently and the same query returns in under 5ms warm. On 256MB, it does not fit, and the "zero heap fetches" win evaporates.

Before adding a covering index with large columns, estimate the projected size:

```sql
SELECT pg_size_pretty(
  sum(
    pg_column_size(salary_min) +
    pg_column_size(title) +
    pg_column_size(location)
  )
) AS estimated_index_entry_size
FROM job_listings
LIMIT 1000;
```

Multiply that per-row size by your row count. If the result exceeds 25% of your `shared_buffers`, the covering index may not deliver consistent warm-cache performance. It might still be worth it — if the query runs infrequently but needs consistent latency on cold cache — but you need to know the tradeoff going in.

Covering indexes work best on small, fixed-width columns: integers, UUIDs, short enums, booleans. If you are including `TEXT` columns in an `INCLUDE` clause, measure the resulting index size against your `shared_buffers` before shipping it.

---

## The p99 Is Your Real SLA

Every scenario above reported p50, p95, and p99. The gap between p50 and p99 is the most underread part of any benchmark:

| Scenario | p50 | p99 | Variance |
|---|---|---|---|
| Hash equality | 0.44ms | 2.99ms | 580% |
| B-tree range | 228ms | 361ms | 58% |
| B-tree equality | 0.37ms | 0.53ms | 43% |
| GIN tags | 19ms | 22ms | 16% |

Hash has a 0.44ms p50. Looks great on a dashboard. The p99 is 2.99ms — 6.8x higher. One in a hundred requests gets a response that is nearly 7x slower. In a UI, that shows up as jank. In an API that chains multiple database calls, it compounds.

GIN has a 16% spread from p50 to p99. That is the tightest of any non-trivial result in this benchmark. The consistency comes from the index's small size — 5.5MB stays hot in `shared_buffers` across runs. Stable cache residency produces stable latency.

Set your SLA targets at p99, not p50. Monitor p95 and p99 in production, not averages. An average that hides a 6x p99 spike is a dashboard lying to you about your user experience. The users on the tail of that distribution are not statistical abstractions — they are the ones writing the one-star reviews.

---

## Build Indexes After Bulk Loads, Not Before

One finding that belongs in every migration playbook.

In this benchmark, all indexes were built after the 1M-row `COPY` completed. Build times:

```
B-tree:   0.56s
Hash:     0.87s
GIN:      1.05s
BRIN:     0.16s
Covering: 0.94s
Total:    ~3.6 seconds
```

If those same indexes had existed before the bulk load, PostgreSQL would have maintained each index on every single `INSERT`. Five index updates per row, across 1M rows, with potential page splits on every B-tree and GIN insert. The total load time would have been 10–30x longer.

PostgreSQL builds an index in one pass: sort, allocate pages, write. Maintaining an index row-by-row is never the same operation. The sort phase alone makes batch construction dramatically cheaper than incremental maintenance.

Any migration that touches a large number of rows should follow this pattern:

```sql
-- Before the migration
DROP INDEX idx_salary_btree;
DROP INDEX idx_tags_gin;
-- ... drop all affected indexes

-- Run your migration
COPY job_listings FROM '/tmp/new_data.csv' CSV;
-- or UPDATE, DELETE, etc.

-- After the migration, rebuild
CREATE INDEX idx_salary_btree ON job_listings (salary_min);
CREATE INDEX CONCURRENTLY idx_tags_gin ON job_listings USING GIN (tags);
```

The `CONCURRENTLY` on the rebuild lets it proceed without a write lock if you need zero-downtime maintenance. The non-concurrent build is faster but locks the table — choose based on whether writes need to continue during the rebuild window.

---

## What This Benchmark Actually Proves

Seven concrete proofs:

1. A B-tree index was **slower** than a sequential scan for the same query (260ms vs 178ms cold). "Index = faster" failed on a 33%-selectivity range query.
2. Hash delivered **no measurable advantage** over B-tree for equality lookups, and its p99 was 5.6x worse.
3. GIN delivered an **8x improvement** that B-tree is architecturally incapable of replicating on array containment queries.
4. BRIN's 24KB index is **280x smaller** than the equivalent B-tree, but warm p50 was 145ms because it still required 6,272 heap page reads.
5. A covering index with `Heap Fetches: 0` took **222ms warm** due to a 55MB index competing for 256MB `shared_buffers`. The algorithm was perfect; the environment was not.
6. Hash's p99 was **5.6x higher** than B-tree for identical queries. Average monitoring would have hidden this entirely.
7. Post-load index creation took **3.6 seconds** for five indexes on 1M rows. Pre-load row-by-row maintenance would have taken minutes.

None of these outcomes were predictable from theory alone.

---

## The Thesis

> Performance is not a default state but a measurable compromise.

Every index decision is an architectural trade. B-tree trades write amplification (one extra write per index per insert) for fast, consistent sorted access. GIN trades a more expensive write path and vacuum overhead for surgical array lookups that sequential scans cannot match. BRIN trades precision for a 280x size reduction, accepting lossy blocks and mandatory rechecks as the cost. Covering indexes trade index bloat for zero heap fetches — a trade that only pays off when the index fits comfortably in memory.

The "best practice" version of indexing — "always index your foreign keys," "index your WHERE columns," "use covering indexes for hot queries" — is not wrong exactly. It is just incomplete. It omits the costs. It omits the conditions under which the trade stops being profitable.

Indexing is not magic. It is bookkeeping. PostgreSQL maintains an auxiliary data structure that costs write time, storage, vacuum cycles, and memory to keep current, in exchange for faster reads on specific access patterns. When the pattern matches — low-selectivity equality lookup, array containment, append-ordered range scan — the trade wins. When it does not — high-selectivity range query, large index on constrained memory, wrong data distribution — the trade loses.

`EXPLAIN (ANALYZE, BUFFERS)` is not an optional debugging tool. It is the instrument you use to verify that a trade is paying off in your specific environment, against your specific data, under your specific memory constraints.

The numbers were required here. They are required in your production system too. Measure first. Ship the index second.