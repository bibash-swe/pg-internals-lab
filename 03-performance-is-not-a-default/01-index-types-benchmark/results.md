# Results: Index Types Benchmark

**Table:** job_listings — 1,000,000 rows
**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, 1GB RAM, 1 CPU)
**Run at:** 2026-06-26 10:54:55
**Warm cache repetitions:** 20 (p50/p95/p99 exclude first run)

Cold cache = first execution after cache eviction via `pg_prewarm`.
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
- Cold cache: 177.99ms
- Warm p50: 253.85ms
- Warm p95: 279.54ms
- Warm p99: 282.12ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Seq Scan on job_listings  (cost=0.00..45203.76 rows=334652 width=43) (actual time=0.394..170.159 rows=337458 loops=1)
  Filter: (is_active AND (salary_min >= 100000) AND (salary_min <= 140000))
  Rows Removed by Filter: 662542
  Buffers: shared hit=2336 read=27868
Planning:
  Buffers: shared hit=16
Planning Time: 0.166 ms
Execution Time: 177.987 ms
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
- Cold cache: 260.08ms
- Warm p50: 228.06ms
- Warm p95: 310.69ms
- Warm p99: 360.63ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=5070.09..40851.09 rows=334657 width=43) (actual time=13.310..248.676 rows=337458 loops=1)
  Recheck Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
  Filter: is_active
  Rows Removed by Filter: 37710
  Heap Blocks: exact=30170
  Buffers: shared hit=22 read=30467 written=2810
  ->  Bitmap Index Scan on idx_salary_btree  (cost=0.00..4986.43 rows=371800 width=0) (actual time=8.747..8.748 rows=375168 loops=1)
        Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
        Buffers: shared read=319
Planning:
  Buffers: shared hit=18 read=1
Planning Time: 0.222 ms
Execution Time: 260.077 ms
```

---

## 3. Hash Index (company_id equality)

**Description:** Hash: pure equality lookup. Cannot support range or ORDER BY.

**Query:**
```sql
SELECT id, title, salary_min FROM job_listings WHERE company_id = 42
```

**Timing:**
- Cold cache: 0.35ms
- Warm p50: 0.44ms
- Warm p95: 1.97ms
- Warm p99: 2.99ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=5.54..755.57 rows=199 width=29) (actual time=0.041..0.328 rows=182 loops=1)
  Recheck Cond: (company_id = 42)
  Heap Blocks: exact=182
  Buffers: shared hit=179 read=7
  ->  Bitmap Index Scan on idx_company_hash  (cost=0.00..5.49 rows=199 width=0) (actual time=0.019..0.020 rows=182 loops=1)
        Index Cond: (company_id = 42)
        Buffers: shared hit=4
Planning:
  Buffers: shared hit=19 read=8 dirtied=2
Planning Time: 1.512 ms
Execution Time: 0.354 ms
```

---

## 3b. B-tree on company_id (same equality query)

**Description:** Direct comparison: B-tree vs Hash for equality.

**Query:**
```sql
SELECT id, title, salary_min FROM job_listings WHERE company_id = 42
```

**Timing:**
- Cold cache: 0.31ms
- Warm p50: 0.37ms
- Warm p95: 0.51ms
- Warm p99: 0.53ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=5.97..756.00 rows=199 width=29) (actual time=0.056..0.284 rows=182 loops=1)
  Recheck Cond: (company_id = 42)
  Heap Blocks: exact=182
  Buffers: shared hit=182 read=3
  ->  Bitmap Index Scan on idx_company_btree_cmp  (cost=0.00..5.92 rows=199 width=0) (actual time=0.034..0.035 rows=182 loops=1)
        Index Cond: (company_id = 42)
        Buffers: shared read=3
Planning:
  Buffers: shared hit=18 read=1
Planning Time: 0.192 ms
Execution Time: 0.306 ms
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
- Cold cache: 19.09ms
- Warm p50: 18.75ms
- Warm p95: 21.81ms
- Warm p99: 22.2ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=292.76..27766.67 rows=17944 width=29) (actual time=7.602..18.522 rows=15902 loops=1)
  Recheck Cond: (tags @> '{python,postgresql}'::text[])
  Heap Blocks: exact=12456
  Buffers: shared hit=11964 read=561
  ->  Bitmap Index Scan on idx_tags_gin  (cost=0.00..288.27 rows=17944 width=0) (actual time=6.591..6.592 rows=15902 loops=1)
        Index Cond: (tags @> '{python,postgresql}'::text[])
        Buffers: shared hit=69
Planning:
  Buffers: shared hit=46 read=7
Planning Time: 1.670 ms
Execution Time: 19.087 ms
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
- Cold cache: 152.48ms
- Warm p50: 197.91ms
- Warm p95: 283.17ms
- Warm p99: 291.24ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Gather  (cost=1000.00..38206.73 rows=17944 width=29) (actual time=1.137..152.046 rows=15902 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=29720 read=792
  ->  Parallel Seq Scan on job_listings  (cost=0.00..35412.33 rows=7477 width=29) (actual time=1.214..142.778 rows=5301 loops=3)
        Filter: (tags @> '{python,postgresql}'::text[])
        Rows Removed by Filter: 328033
        Buffers: shared hit=29720 read=792
Planning:
  Buffers: shared hit=7
Planning Time: 0.173 ms
Execution Time: 152.483 ms
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
- Cold cache: 41.78ms
- Warm p50: 145.28ms
- Warm p95: 168.17ms
- Warm p99: 170.81ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=62.99..33254.30 rows=197939 width=33) (actual time=0.142..37.018 rows=200000 loops=1)
  Recheck Cond: ((created_at >= '2022-01-01 00:00:00+00'::timestamp with time zone) AND (created_at < '2023-01-01 00:00:00+00'::timestamp with time zone))
  Rows Removed by Index Recheck: 7929
  Heap Blocks: lossy=6272
  Buffers: shared hit=6277
  ->  Bitmap Index Scan on idx_created_brin  (cost=0.00..13.50 rows=199154 width=0) (actual time=0.111..0.112 rows=62720 loops=1)
        Index Cond: ((created_at >= '2022-01-01 00:00:00+00'::timestamp with time zone) AND (created_at < '2023-01-01 00:00:00+00'::timestamp with time zone))
        Buffers: shared hit=5
Planning:
  Buffers: shared hit=41 read=3 dirtied=1
Planning Time: 0.700 ms
Execution Time: 41.777 ms
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
- Cold cache: 47.30ms
- Warm p50: 222.21ms
- Warm p95: 280.27ms
- Warm p99: 289.08ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Index Only Scan using idx_salary_covering on job_listings  (cost=0.42..18130.66 rows=371800 width=35) (actual time=0.040..38.423 rows=375168 loops=1)
  Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
  Heap Fetches: 0
  Buffers: shared hit=1 read=2652
Planning:
  Buffers: shared hit=23 read=1
Planning Time: 0.255 ms
Execution Time: 47.302 ms
```

---

## Summary

| Scenario | Cold | p50 | p95 | p99 |
|----------|------|-----|-----|-----|
| 1. Sequential Scan (no index) | 178.0ms | 253.85ms | 279.54ms | 282.12ms |
| 2. B-tree Index (salary_min) | 260.1ms | 228.06ms | 310.69ms | 360.63ms |
| 3. Hash Index (company_id equality) | 0.4ms | 0.44ms | 1.97ms | 2.99ms |
| 3b. B-tree on company_id (same equality query) | 0.3ms | 0.37ms | 0.51ms | 0.53ms |
| 4. GIN Index (tags array contains) | 19.1ms | 18.75ms | 21.81ms | 22.2ms |
| 4b. Sequential scan (tags, no GIN) | 152.5ms | 197.91ms | 283.17ms | 291.24ms |
| 5. BRIN Index (created_at range) | 41.8ms | 145.28ms | 168.17ms | 170.81ms |
| 6. Covering Index (index-only scan) | 47.3ms | 222.21ms | 280.27ms | 289.08ms |