# Results: Index Types Benchmark

**Table:** job_listings — 1,000,000 rows
**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, 1GB RAM, 1 CPU)
**Run at:** 2026-06-27 11:12:26
**Warm cache repetitions:** 20 (p50/p95/p99 exclude first run)

Cold cache = first execution after mathematically forcing LRU buffer eviction.
Warm cache = subsequent executions (data in shared_buffers).

---

## 1. Sequential Scan (no index)

**Description:** Baseline: full table scan with no index on salary_min.

**Query:**
```sql
SELECT id, title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                    AND is_active = true
```

**Timing:**
- Cold cache: 107.02ms
- Warm p50: 295.42ms
- Warm p95: 341.17ms
- Warm p99: 350.58ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Seq Scan on job_listings  (cost=0.00..45204.00 rows=336069 width=43) (actual time=0.012..99.953 rows=337704 loops=1)
  Filter: (is_active AND (salary_min >= 100000) AND (salary_min <= 140000))
  Rows Removed by Filter: 662296
  Buffers: shared hit=28280 read=1924
Planning:
  Buffers: shared hit=20 read=2
Planning Time: 0.351 ms
Execution Time: 107.015 ms
```

---

## 2. B-tree Index (salary_min)

**Description:** B-tree: optimal for range queries on ordered values.

**Query:**
```sql
SELECT id, title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                    AND is_active = true
```

**Timing:**
- Cold cache: 116.76ms
- Warm p50: 265.23ms
- Warm p95: 300.63ms
- Warm p99: 310.04ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=5079.77..40874.77 rows=336069 width=43) (actual time=9.639..108.965 rows=337704 loops=1)
  Recheck Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
  Filter: is_active
  Rows Removed by Filter: 37369
  Heap Blocks: exact=30167
  Buffers: shared hit=19482 read=11004
  ->  Bitmap Index Scan on idx_salary_btree  (cost=0.00..4995.76 rows=372733 width=0) (actual time=6.999..7.000 rows=375073 loops=1)
        Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
        Buffers: shared read=319
Planning:
  Buffers: shared hit=16 read=1
Planning Time: 0.220 ms
Execution Time: 116.760 ms
```

---

## 3. Hash Index (company_id equality)

**Description:** Hash: pure equality lookup. Cannot support range or ORDER BY.

**Query:**
```sql
SELECT id, title, salary_min FROM job_listings WHERE company_id = 42
```

**Timing:**
- Cold cache: 0.27ms
- Warm p50: 0.37ms
- Warm p95: 0.48ms
- Warm p99: 0.5ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=5.54..755.57 rows=199 width=29) (actual time=0.030..0.250 rows=199 loops=1)
  Recheck Cond: (company_id = 42)
  Heap Blocks: exact=199
  Buffers: shared hit=192 read=11
  ->  Bitmap Index Scan on idx_company_hash  (cost=0.00..5.49 rows=199 width=0) (actual time=0.015..0.016 rows=199 loops=1)
        Index Cond: (company_id = 42)
        Buffers: shared hit=4
Planning:
  Buffers: shared hit=16 read=6
Planning Time: 0.550 ms
Execution Time: 0.267 ms
```

---

## 3b. B-tree on company_id (same equality query)

**Description:** Direct comparison: B-tree vs Hash for equality.

**Query:**
```sql
SELECT id, title, salary_min FROM job_listings WHERE company_id = 42
```

**Timing:**
- Cold cache: 0.41ms
- Warm p50: 0.46ms
- Warm p95: 1.53ms
- Warm p99: 2.02ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=5.54..755.57 rows=199 width=29) (actual time=0.041..0.387 rows=199 loops=1)
  Recheck Cond: (company_id = 42)
  Heap Blocks: exact=199
  Buffers: shared hit=202
  ->  Bitmap Index Scan on idx_company_hash  (cost=0.00..5.49 rows=199 width=0) (actual time=0.018..0.018 rows=199 loops=1)
        Index Cond: (company_id = 42)
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=17 read=1
Planning Time: 0.214 ms
Execution Time: 0.411 ms
```

---

## 4. GIN Index (tags array contains)

**Description:** GIN: 'find all listings tagged python AND postgresql'.

**Query:**
```sql
SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
```

**Timing:**
- Cold cache: 15.36ms
- Warm p50: 21.25ms
- Warm p95: 30.22ms
- Warm p99: 30.48ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=289.75..27422.63 rows=17371 width=29) (actual time=6.695..14.982 rows=15953 loops=1)
  Recheck Cond: (tags @> '{python,postgresql}'::text[])
  Heap Blocks: exact=12528
  Buffers: shared hit=12075 read=520
  ->  Bitmap Index Scan on idx_tags_gin  (cost=0.00..285.41 rows=17371 width=0) (actual time=5.844..5.845 rows=15953 loops=1)
        Index Cond: (tags @> '{python,postgresql}'::text[])
        Buffers: shared hit=67
Planning:
  Buffers: shared hit=44 read=8
Planning Time: 0.725 ms
Execution Time: 15.357 ms
```

---

## 4b. Sequential scan (tags, no GIN)

**Description:** Baseline for GIN comparison: same array contains query without the GIN index.

**Query:**
```sql
SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
```

**Timing:**
- Cold cache: 176.83ms
- Warm p50: 207.19ms
- Warm p95: 279.29ms
- Warm p99: 282.25ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Gather  (cost=1000.00..38149.43 rows=17371 width=29) (actual time=1.015..176.374 rows=15953 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=28554 read=1958
  ->  Parallel Seq Scan on job_listings  (cost=0.00..35412.33 rows=7238 width=29) (actual time=1.105..146.005 rows=5318 loops=3)
        Filter: (tags @> '{python,postgresql}'::text[])
        Rows Removed by Filter: 328016
        Buffers: shared hit=28554 read=1958
Planning:
  Buffers: shared hit=6
Planning Time: 0.127 ms
Execution Time: 176.833 ms
```

---

## 5. BRIN Index (created_at range)

**Description:** BRIN: 280x smaller than B-tree. Works because rows are inserted in time order.

**Query:**
```sql
SELECT id, title, created_at
                    FROM job_listings
                    WHERE created_at >= '2022-01-01'
                    AND created_at < '2023-01-01'
```

**Timing:**
- Cold cache: 39.73ms
- Warm p50: 155.08ms
- Warm p95: 195.35ms
- Warm p99: 217.57ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=63.16..33254.46 rows=198631 width=33) (actual time=0.470..35.087 rows=200000 loops=1)
  Recheck Cond: ((created_at >= '2022-01-01 00:00:00+00'::timestamp with time zone) AND (created_at < '2023-01-01 00:00:00+00'::timestamp with time zone))
  Rows Removed by Index Recheck: 16439
  Heap Blocks: lossy=6528
  Buffers: shared hit=6533
  ->  Bitmap Index Scan on idx_created_brin  (cost=0.00..13.50 rows=199153 width=0) (actual time=0.072..0.073 rows=65280 loops=1)
        Index Cond: ((created_at >= '2022-01-01 00:00:00+00'::timestamp with time zone) AND (created_at < '2023-01-01 00:00:00+00'::timestamp with time zone))
        Buffers: shared hit=5
Planning:
  Buffers: shared hit=38 read=5
Planning Time: 0.488 ms
Execution Time: 39.726 ms
```

---

## 6. Covering Index (index-only scan)

**Description:** Covering index with INCLUDE: all needed columns in the index, no heap visit.

**Query:**
```sql
SELECT title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
```

**Timing:**
- Cold cache: 42.28ms
- Warm p50: 253.72ms
- Warm p95: 288.05ms
- Warm p99: 297.78ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Index Only Scan using idx_salary_covering on job_listings  (cost=0.42..18188.98 rows=372733 width=35) (actual time=0.052..33.744 rows=375073 loops=1)
  Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
  Heap Fetches: 0
  Buffers: shared hit=1 read=2651
Planning:
  Buffers: shared hit=23 read=1
Planning Time: 0.133 ms
Execution Time: 42.285 ms
```

---

## Summary

| Scenario | Cold | p50 | p95 | p99 |
|----------|------|-----|-----|-----|
| 1. Sequential Scan (no index) | 107.0ms | 295.42ms | 341.17ms | 350.58ms |
| 2. B-tree Index (salary_min) | 116.8ms | 265.23ms | 300.63ms | 310.04ms |
| 3. Hash Index (company_id equality) | 0.3ms | 0.37ms | 0.48ms | 0.5ms |
| 3b. B-tree on company_id (same equality query) | 0.4ms | 0.46ms | 1.53ms | 2.02ms |
| 4. GIN Index (tags array contains) | 15.4ms | 21.25ms | 30.22ms | 30.48ms |
| 4b. Sequential scan (tags, no GIN) | 176.8ms | 207.19ms | 279.29ms | 282.25ms |
| 5. BRIN Index (created_at range) | 39.7ms | 155.08ms | 195.35ms | 217.57ms |
| 6. Covering Index (index-only scan) | 42.3ms | 253.72ms | 288.05ms | 297.78ms |