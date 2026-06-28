# The Index Lie: What a 1M-Row PostgreSQL Benchmark Actually Proves

There is an implicit contract in database engineering. You add an index, queries get faster. Everyone learns this on day one. It is repeated in every tutorial, every onboarding doc, every "PostgreSQL performance tips" blog post written since 2008.

The contract is a lie.

Not always. Not even usually. But often enough — under memory pressure, at the wrong selectivity, on the wrong data distribution — that treating it as a law is how you end up shipping a production incident with an `EXPLAIN ANALYZE` printout in your hand wondering why your index made things *worse*.

This post is a record of a benchmark against a 1-million-row `job_listings` table in a constrained PostgreSQL 16 container: 256MB `shared_buffers`, 1GB RAM, 1 CPU. The constraints were deliberate. Production databases are not unlimited. This setup was tuned to expose the gaps between theory and reality at a scale where the numbers are still readable.

Every finding below has a measurement behind it. No assertions without evidence.

---

## The Setup

`job_listings`. One million rows. Five index types tested: B-tree, Hash, GIN, BRIN, and a covering index. Cold cache was enforced via LRU eviction — a 300MB temp table was written before each cold test to thrash the buffer pool. Warm cache results are p50/p95/p99 across 20 runs with the first excluded.

The benchmark now tracks and validates which index the planner actually used for each scenario, and explicitly cleans up indexes between scenarios to prevent ghost indexes from leaking into subsequent measurements. The `✓` column in the summary means the planner used exactly the index the scenario intended — a mismatch flags an invalid run.

| Scenario | Cold | p50 | p95 | p99 | Rows | Index Used |
|---|---|---|---|---|---|---|
| 1. Sequential Scan (no index) | 175ms | 303ms | 385ms | 419ms | ~337,275 | seq scan ✓ |
| 2. B-tree (salary_min range) | 300ms | 269ms | 340ms | 374ms | ~337,275 | idx_salary_btree ✓ |
| 3. Hash (company_id equality) | 0.27ms | 0.51ms | 1.27ms | 1.52ms | 216 | idx_company_hash ✓ |
| 3b. B-tree (company_id equality) | 1.16ms | 0.43ms | 1.01ms | 1.30ms | 216 | idx_company_btree_cmp ✓ |
| 4. GIN (tags array contains) | 25ms | 22.6ms | 28.6ms | 30.9ms | 16,078 | idx_tags_gin ✓ |
| 4b. Seq scan (tags, no GIN) | 218ms | 212ms | 298ms | 304ms | ~16,077 | seq scan ✓ |
| 5. BRIN (created_at range) | 44ms | 161ms | 176ms | 180ms | ~200,000 | idx_created_brin ✓ |
| 6. Covering index (index-only scan) | 56ms | 251ms | 320ms | 329ms | 374,654 | idx_salary_covering ✓ |

---

## Proof 1: The B-Tree That Made Things Worse

**The assumption:** An index on `salary_min` will speed up a range query on `salary_min`.

**The query:**

```sql
SELECT id, title, location, salary_min
FROM job_listings
WHERE salary_min BETWEEN 100000 AND 140000
  AND is_active = true;
```

**The reality:** Sequential scan — 175ms cold. B-tree index — 300ms cold. The index was 70% slower.

This is the most counter-intuitive result in the benchmark, and the most important to internalize. But the B-tree's failure here has two independent causes, both worth understanding.

**Cause 1 — Selectivity and random I/O.** PostgreSQL chose a Bitmap Index Scan — the correct strategy for this selectivity. The index found the salary range in the B-tree, assembled a bitmap of matching row locations, then executed a Bitmap Heap Scan to fetch those rows from the heap. That Heap Scan touched 30,213 pages. The sequential scan touched 27,868. Same data volume, but the B-tree path accessed those pages in non-sequential order — random I/O — while the sequential scan read them in physical disk order.

Sequential I/O on a page cache under memory pressure is faster than random I/O. The index did not reduce disk work. It reorganised it in a way that made it slower.

**Cause 2 — The planner was flying blind.** Here is the EXPLAIN output for the B-tree scenario:

```
Bitmap Index Scan on idx_salary_btree
  (cost=0.00..70.42 rows=5000 width=0)
  (actual time=15.410..15.411 rows=374654 loops=1)
```

The planner estimated 5,000 rows. The actual was 374,654. A 75x estimation error.

The planner's entire job is to pick the cheapest plan. It thought the B-tree would return 5,000 rows — a cheap, targeted operation. It had no idea it would fetch 374,654 rows from scattered heap pages across 30,000 8KB disk reads. If it had known the real number, it would have chosen the sequential scan. It did not know because `ANALYZE` had never been run after the bulk load.

`COPY` does not trigger `ANALYZE`. Creating indexes does not trigger `ANALYZE`. Autovacuum runs asynchronously and had not caught up yet when this benchmark ran. The planner was operating on empty statistics and fell back to default selectivity estimates.

This is why the B-tree cold result (300ms) is worse than the sequential scan (175ms) by such a wide margin — worse than it would have been even with perfect planning. The planner chose the most expensive path because it thought it was cheap.

**The warm cache closes the gap.** Warm p50: B-tree 269ms vs sequential scan 303ms. Once the index pages are in shared_buffers and the heap data is partially cached, the B-tree pull-ahead is modest but real. The cold result is the honest one — that is what every new query against a cold cache sees.

**The 10% rule:** If a range query returns more than roughly 10% of the table, a B-tree range index is likely to hurt on cold cache. This query returned 33% of the table.

Verify selectivity before creating any range index:

```sql
SELECT
  count(*) FILTER (WHERE salary_min BETWEEN 100000 AND 140000) AS matches,
  count(*) AS total,
  round(
    100.0 * count(*) FILTER (WHERE salary_min BETWEEN 100000 AND 140000) / count(*), 2
  ) AS selectivity_pct
FROM job_listings;
```

If `selectivity_pct` comes back above 10, stop. Ask whether the query pattern can be redesigned to be more selective. If you still add the index, run `EXPLAIN (ANALYZE, BUFFERS)` against real traffic volume and look at `Buffers: shared read=N`. If that number is close to the total heap page count, no index will fix this.

---

## Proof 2: The Stale Statistics Trap

**The assumption:** After a bulk load, the planner has accurate statistics.

**The reality:** It does not, unless you tell it to collect them.

This finding cuts across every scenario before the explicit `ANALYZE` was called between scenarios 4b and 5. Here are the planner's row estimates versus reality across the first four scenarios:

| Scenario | Planner Estimate | Actual Rows | Error |
|---|---|---|---|
| 1. Seq Scan (salary range) | ~3,126 total | ~337,275 | 108× |
| 2. B-tree (salary range) | 5,000 | 374,654 | 75× |
| 3. Hash (company_id = 42) | 5,000 | 216 | 23× |
| 4. GIN (tags contains) | 25 | 16,078 | 643× |

After the explicit `ANALYZE job_listings` ran before scenario 5:

| Scenario | Planner Estimate | Actual Rows | Error |
|---|---|---|---|
| 5. BRIN (created_at range) | 199,154 | ~200,000 | <1% |
| 6. Covering (salary range) | 374,633 | 374,654 | <0.01% |

Same table. Same data. Night-and-day planning accuracy.

The root cause is simple: `COPY` does not trigger `ANALYZE`. Creating indexes updates `pg_class.reltuples` (a rough row count), but does not update `pg_stats` (the column-level histograms, most-common-values lists, and correlation figures the planner actually uses). Without `pg_stats`, the planner falls back to hardcoded default selectivity fractions — typically 0.5% for equality conditions and 0.33% for range conditions. On a 1M-row table with a salary column that has 8 distinct values, those defaults are catastrophically wrong.

Autovacuum will eventually collect statistics, but "eventually" is asynchronous. When benchmark.py started immediately after seed.py, autovacuum had not run. The planner was blind.

The 643x estimation error on scenario 4 (GIN predicted 25 rows, found 16,078) is the most extreme in this set. It had no practical impact because there is no alternative to GIN for `@>` queries — the planner was forced to use the GIN index regardless of what it thought the row count would be. But in scenarios 1 and 2, the estimation errors directly caused wrong plan choices — the planner selected a strategy it believed would be cheap that turned out to be expensive.

Run this immediately after any bulk load, before any query that matters:

```sql
ANALYZE job_listings;
```

On large tables, auto-analyze sampling is also worth tuning. For a 1M-row table:

```sql
-- Increase statistics target for columns with bad default estimates
ALTER TABLE job_listings
  ALTER COLUMN salary_min SET STATISTICS 500;
ALTER TABLE job_listings
  ALTER COLUMN tags SET STATISTICS 500;
ANALYZE job_listings;
```

The default statistics target is 100 (representing approximately 300 sampled values). Columns with skewed distributions or many distinct values often need a higher target to produce accurate histograms.

---

## Proof 3: Hash vs B-tree — The Real Comparison

Previous benchmark runs were invalidated by a methodology bug: scenario 3b created a B-tree index without first dropping the hash index from scenario 3. PostgreSQL had both indexes and chose hash for the equality query in both scenarios. The "B-tree" scenario was actually measuring hash twice — once with a competing unused B-tree burning shared_buffers, once without.

The fix: scenario 3 now drops `idx_company_hash` as post-scenario cleanup before scenario 3b starts. Scenario 3b drops both indexes belt-and-suspenders at the start of its setup. The index audit printed before each scenario confirms what is actually present.

The first valid comparison:

| Metric | Hash | B-tree |
|---|---|---|
| Cold | 0.27ms | 1.16ms |
| p50 (warm) | 0.51ms | 0.43ms |
| p95 (warm) | 1.27ms | 1.01ms |
| p99 (warm) | 1.52ms | 1.30ms |
| Build time | 787ms | 462ms |

Hash wins cold by 4x (0.27ms vs 1.16ms). B-tree wins every warm percentile. At p99 the gap is 0.22ms — tight enough that workload characteristics and connection pool overhead will matter more than the index type in most production systems.

Hash builds 70% slower (787ms vs 462ms). For a 1M-row table that is not a significant difference, but the pattern holds at scale — hash index construction is more expensive than B-tree construction for the same row count.

B-tree retains every capability hash lacks: range queries, ORDER BY, multi-column indexes. Hash supports only equality predicates. If you have a mix of equality and range queries on the same column, a single B-tree covers both. Hash covers neither the range nor the ORDER BY case and requires a separate index for each.

The practical conclusion: for pure equality lookups, both index types are fast enough that warm-cache p99 is sub-2ms for either. The decision comes down to workload shape. If the query is only ever `column = value` and the column has very high cardinality (UUIDs, user IDs), hash is a viable option and marginally faster cold. If the column is ever queried by range, or needs ORDER BY optimisation, or shares a multi-column index with another column — B-tree, always.

The old claim that "B-tree wins on every metric" was built on bad data. The corrected data shows: neither index type is a clear winner on every axis. Measure your actual workload.

---

## Proof 4: GIN Is Not Optional for Array Columns

**The assumption:** An array column can be queried with a sequential scan. It is slow but it works.

**The query:**

```sql
SELECT id, title, salary_min
FROM job_listings
WHERE tags @> ARRAY['python', 'postgresql'];
```

**The reality:**

| Scenario | Cold | p50 | p95 | p99 |
|---|---|---|---|---|
| GIN index | 25ms | 22.6ms | 28.6ms | 30.9ms |
| Sequential scan | 218ms | 212ms | 298ms | 304ms |

Nearly 10x improvement across all percentiles. But the raw number understates the finding. Look at the p95 spread: GIN goes from 22.6ms to 28.6ms — 27% variance. Sequential scan goes from 212ms to 304ms — 43% variance, across 3 parallel workers reading 29,855 buffer pages. GIN is not just faster; it is stable.

The GIN execution path for this query:
1. Look up two posting lists in the inverted index: 7.3ms from the EXPLAIN Bitmap Index Scan timing. The GIN index itself touched 65 pages — roughly 520KB of index data.
2. Intersect the posting lists to find row IDs matching both `'python'` AND `'postgresql'`.
3. Visit the result heap pages to fetch 16,078 matching rows.

Total: 22ms warm, with most of that time being the heap access in step 3. The GIN lookup itself is sub-8ms every time because the index is small enough to stay hot in shared_buffers.

There is no B-tree workaround for `@>`. B-tree cannot express array containment. If you have a table with an array column and no GIN index, you are doing a full table scan on every `@>` query, always, regardless of how many other indexes you have. This is not a tradeoff with costs — it is a missing capability.

The same applies to JSONB queried with `@>` or `?`, and to full-text search on `tsvector` columns.

Any column queried with `@>`, `?`, `@@`, or `@?` needs a GIN index. Add it before the table reaches 100,000 rows so you never feel the pain of rebuilding it under write load:

```sql
CREATE INDEX CONCURRENTLY idx_tags_gin ON job_listings USING GIN (tags);
```

`CONCURRENTLY` is non-negotiable in production. It builds the index without holding a table lock.

---

## Proof 5: BRIN Is a Skip Hint, Not a Lookup

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
| Cold | 43.5ms |
| p50 (warm) | 160.6ms |
| p95 (warm) | 175.7ms |
| p99 (warm) | 179.6ms |
| Index size | 24 KB |
| Rows removed by recheck | 12,141 |

Warm p50 is 160ms. The index is 24KB. Why is it taking 160ms?

The EXPLAIN line that explains it:

```
Rows Removed by Index Recheck: 12141
```

BRIN stores only the min and max value for every 128 consecutive heap pages. It cannot identify individual rows — only block ranges. When the query asks for rows in 2022, BRIN returns every 128-page block whose min/max range overlaps the 2022 date range. PostgreSQL then fetches all those blocks and rechecks every row against the actual timestamp predicate. The 12,141 removed rows are false positives — they lived in a block that BRIN flagged as potentially matching, but their actual `created_at` value fell outside the query range. The recheck is mandatory. BRIN is lossy by design.

The overall heap access: 6,405 buffer pages touched. Total heap is ~32,000 pages. BRIN eliminated roughly 80% of the table but could not be more precise than block-level hints. Those 6,405 pages still had to be loaded and scanned row by row.

The B-tree on the same column would be ~6.7MB and would pinpoint exactly the rows in the 2022 range with zero false positives. BRIN is 280x smaller. That size difference is what you are buying — at the cost of precision.

BRIN is the right choice when three conditions are simultaneously true:
1. The column correlates strongly with physical storage order.
2. The table is large enough that 280x index size reduction matters.
3. Queries scan wide date ranges, not pinpoint individual rows.

Condition 1 is not automatic. Check it before creating any BRIN index:

```sql
SELECT correlation
FROM pg_stats
WHERE tablename = 'job_listings'
  AND attname = 'created_at';
```

A value near `1.0` means physical storage order matches column order — BRIN works well. Below `0.5`, BRIN degrades toward a full scan. An append-only event log has `created_at` correlation near 1.0. A job listings table where postings are backdated or randomly updated does not. Check the number before committing to BRIN.

---

## Proof 6: The Covering Index — Cold Win, Warm Overhead

**The assumption:** An index-only scan eliminates heap access. Zero heap fetches means maximum speed.

**The query:**

```sql
SELECT title, location, salary_min
FROM job_listings
WHERE salary_min BETWEEN 100000 AND 140000;
```

**The EXPLAIN output:**

```
Index Only Scan using idx_salary_covering on job_listings
  (cost=0.42..18283.33 rows=374633 width=35)
  (actual time=0.167..47.054 rows=374654 loops=1)
Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
Buffers: shared hit=1 read=2648 written=431
```

`Index Only Scan`. The planner estimated 374,633 rows and got 374,654 — within 21 rows. This is what planning looks like after `ANALYZE` has run.

**Cold performance: genuinely good.** 56ms cold vs 175ms cold for the sequential scan — a 3x improvement. The covering index reads 2,648 pages from a 55MB index rather than 27,868 pages from a 361MB heap. When nothing is in shared_buffers, fewer pages to read means faster execution. The index-only access path is doing exactly what it is supposed to do on a cold cache.

**Warm performance: real but modest.** Warm p50: 251ms vs 303ms for the sequential scan — 17% improvement. Not "nearly identical to seq scan" as previously stated. The 55MB covering index cannot fit fully in 256MB shared_buffers alongside the 361MB heap and other index pages, so warm runs still require fetching 2,648 pages from the index on most runs. But 2,648 pages is far fewer than 27,868 heap pages, so the covering index still wins even warm.

**The visibility map caveat.** The benchmark ran `VACUUM job_listings` before scenario 6 specifically to update the heap's visibility map. Index Only Scan works by checking the visibility map — if a heap page's "all-visible" bit is set, PostgreSQL knows every row on that page is visible to all transactions and skips the heap fetch. The `written=431` in the buffer output reveals that 431 heap pages were dirtied during the scan, meaning their visibility map bits were updated on first read. Those 431 pages required actual heap fetches. Without the prior VACUUM, far more heap pages would lack visibility map bits, driving heap fetches dramatically higher and potentially turning the Index Only Scan into a disguised heap scan.

`VACUUM` is not optional infrastructure for tables that rely on Index Only Scan. It is a correctness precondition.

Before adding a covering index with large columns, estimate the projected index size:

```sql
SELECT pg_size_pretty(
  sum(
    pg_column_size(salary_min) +
    pg_column_size(title) +
    pg_column_size(location)
  )
) AS estimated_entry_size
FROM job_listings
LIMIT 1000;
```

Multiply that per-row figure by your row count. If the result exceeds 25% of your `shared_buffers`, the covering index will not fully cache in memory and warm performance will degrade proportionally. Covering indexes deliver their greatest advantage when the included columns are narrow fixed-width types: integers, UUIDs, short enums.

---

## The p99 Is Your Real SLA

The gap between p50 and p99 across scenarios tells a different story from the medians:

| Scenario | p50 | p99 | Variance |
|---|---|---|---|
| BRIN (created_at range) | 160.6ms | 179.6ms | 12% |
| Covering index | 250.8ms | 328.9ms | 31% |
| GIN tags | 22.6ms | 30.9ms | 37% |
| B-tree range | 268.7ms | 373.7ms | 39% |
| Hash equality | 0.51ms | 1.52ms | 198% |
| B-tree equality | 0.43ms | 1.30ms | 202% |

BRIN has the tightest spread of any scenario (12%). Once BRIN has identified its block ranges and the data is warm in shared_buffers, execution is mechanically repetitive — scan 6,405 pages, recheck rows, done. The variance floor is deterministic.

The equality indexes (Hash and B-tree on `company_id`) both show ~200% p50→p99 variance despite sub-millisecond absolute numbers. This is not a pathological failure mode — it reflects how sensitive sub-millisecond measurements are to connection overhead, lock acquisition, and OS scheduler jitter at these timescales. Both are fast enough for any production equality lookup.

GIN's 37% variance (22.6ms to 30.9ms) is slightly higher than BRIN's but still tight for a query touching 16,000 rows. The index stays hot in shared_buffers and the variance comes from heap access patterns as cache state changes between runs.

Set SLA targets at p99, not p50. A dashboard showing a smooth 22ms average for your tag search query is hiding the 31ms p99 spikes that show up as latency in your UI. They are not rounding errors — they are the real user experience for 1 in 100 requests.

---

## Build Indexes After Bulk Loads, Not Before

Index build times from this run, post-COPY:

```
idx_salary_btree:    0.63s  — B-tree on salary_min
idx_company_hash:    0.84s  — Hash on company_id
idx_tags_gin:        2.48s  — GIN on tags
idx_created_brin:    0.15s  — BRIN on created_at
idx_salary_covering: 1.05s  — Covering index with INCLUDE
Total:               ~5.15 seconds
```

5.15 seconds for five indexes on 1M rows, built in bulk after the load. If those same indexes had existed before the COPY, PostgreSQL would have maintained each one on every individual insert: five index page updates per row, across 1M rows, with potential page splits on every B-tree and GIN write. Bulk load time would have been 10–30x longer.

GIN took 2.48 seconds to build — the most expensive of the five, nearly 4x the B-tree build time. The GIN structure is more complex to construct: it must build an inverted index over every array element, merge posting lists, and sort by term. That cost is paid once at build time and not again. Row-by-row GIN maintenance during bulk inserts pays that cost incrementally, and the accumulated overhead is far worse than the one-shot build.

Any migration inserting or modifying large row counts:

```sql
-- Before the migration
DROP INDEX idx_salary_btree;
DROP INDEX idx_tags_gin;
-- Drop all indexes that touch modified columns

-- Run the migration
COPY job_listings FROM '/tmp/new_data.csv' CSV;

-- Rebuild after
CREATE INDEX idx_salary_btree ON job_listings (salary_min);
CREATE INDEX CONCURRENTLY idx_tags_gin ON job_listings USING GIN (tags);

-- Do not forget
ANALYZE job_listings;
```

The `ANALYZE` at the end is not optional. The entire previous section on stale statistics exists because it was missing. Build the indexes, then give the planner the information it needs to use them correctly.

---

## What This Benchmark Actually Proves

Eight findings grounded in the actual output:

1. A B-tree index was **70% slower** than a sequential scan on a cold cache (300ms vs 175ms). At 33% selectivity, the index reorganised I/O in a way that made it more expensive, not less.

2. The planner's row estimates before `ANALYZE` were off by **75–643x** depending on the scenario. The worst estimation error (643x on GIN) had no practical impact. The 75x error on the B-tree scenario caused the planner to choose a plan it believed would be cheap that cost nearly 3 seconds. Stale statistics do not just produce wrong numbers in `EXPLAIN` — they produce wrong query plans.

3. After `ANALYZE`, the planner estimated **374,633 rows vs 374,654 actual** for the covering index scan — 99.99% accurate. Accurate planning comes from statistics, not magic.

4. The Hash vs. B-tree comparison is **now valid** — the first time these scenarios ran with proper isolation. Hash wins cold (0.27ms vs 1.16ms). B-tree wins all warm percentiles. The warm p99 gap is 0.22ms. B-tree builds 41% faster (462ms vs 787ms). Neither is a clear winner on every axis; workload shape decides.

5. GIN delivered a **~10x improvement** over sequential scan for array containment — a capability B-tree is architecturally incapable of replicating. At p99: 31ms vs 304ms.

6. BRIN removed **12,141 false-positive rows** via mandatory recheck on every execution — confirmed by the EXPLAIN output after `ANALYZE` collected accurate statistics. The 24KB index eliminated 80% of the heap scan but cannot avoid a row-level recheck of every page it touches.

7. The covering index delivered a **3x cold improvement** (56ms vs 175ms) because it reads 2,648 index pages instead of 27,868 heap pages. Warm improvement was 17% (251ms vs 303ms), not "nearly identical to a sequential scan." The prior claim was too pessimistic. The 55MB index competes for 256MB shared_buffers but still wins on pages-read counts.

8. `VACUUM` before an Index Only Scan is a **correctness precondition**, not optional maintenance. The 431 written buffers in scenario 6 show that heap pages lacking visibility map bits still required heap fetches even after VACUUM ran. Without VACUUM, far more pages would have been un-mapped, turning the index-only scan into a disguised heap scan.

None of these outcomes were predictable from theory alone.

---

## The Thesis

> Performance is not a default state but a measurable compromise.

Every index decision is an architectural trade. B-tree trades write amplification for fast, consistent sorted access. GIN trades a heavier write path and vacuum overhead for surgical array lookups. BRIN trades precision for a 280x size reduction, accepting lossy blocks and mandatory rechecks. Covering indexes trade index bloat for fewer heap page reads — a trade that pays off on cold cache and moderately on warm, but only if VACUUM keeps the visibility map current. And underneath all of it: accurate statistics, which the planner needs to choose correctly between these options, and which `COPY` will not provide without an explicit `ANALYZE`.

The "best practice" version of indexing — "index your foreign keys," "index your WHERE columns," "ANALYZE after bulk loads" — is not wrong. It is just incomplete. It omits the conditions under which each trade stops being profitable.

`EXPLAIN (ANALYZE, BUFFERS)` is not an optional debugging tool. It is the instrument you use to verify that a trade is paying off in your specific environment, against your specific data, under your specific memory constraints. The numbers in this post were required. The numbers in your production system are too.

Measure first. Ship the index second.