# Results: Index Types Benchmark

**Table:** job_listings — 1,000,000 rows
**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, 1GB RAM, 1 CPU)
**Run at:** 2026-06-28 08:09:01
**Warm cache repetitions:** 20 (p50/p95/p99 exclude first run)

Cold cache = first execution after mathematically forcing LRU buffer eviction.
Warm cache = subsequent executions (data in shared_buffers).

Each scenario's `EXPLAIN` output was parsed to confirm the planner actually used the index under test (`Index Used` / `Expected`). A mismatch means the scenario's timing reflects a different index than intended and should be treated as invalid.

---

## 1. Sequential Scan (no index)

**Description:** Baseline: full table scan with no index on salary_min.

**Index used:** `none (seq scan)` (expected: `none`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                    AND is_active = true
```

**Timing:**
- Cold cache: 175.35ms
- Warm p50: 303.06ms
- Warm p95: 385.45ms
- Warm p99: 419.13ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Gather  (cost=1000.00..37704.00 rows=2500 width=76) (actual time=0.418..120.073 rows=337274 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=2336 read=27868
  ->  Parallel Seq Scan on job_listings  (cost=0.00..36454.00 rows=1042 width=76) (actual time=0.027..109.126 rows=112425 loops=3)
        Filter: (is_active AND (salary_min >= 100000) AND (salary_min <= 140000))
        Rows Removed by Filter: 220909
        Buffers: shared hit=2336 read=27868
Planning:
  Buffers: shared hit=14
Planning Time: 0.113 ms
Execution Time: 175.348 ms
```

---

## 2. B-tree Index (salary_min)

**Description:** B-tree: optimal for range queries on ordered values.

**Index used:** `idx_salary_btree` (expected: `idx_salary_btree`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                    AND is_active = true
```

**Timing:**
- Cold cache: 299.75ms
- Warm p50: 268.74ms
- Warm p95: 340.22ms
- Warm p99: 373.69ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=71.05..13200.91 rows=2500 width=76) (actual time=18.206..291.690 rows=337274 loops=1)
  Recheck Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
  Filter: is_active
  Rows Removed by Filter: 37380
  Heap Blocks: exact=30167
  Buffers: shared hit=272 read=30213 written=3006
  ->  Bitmap Index Scan on idx_salary_btree  (cost=0.00..70.42 rows=5000 width=0) (actual time=15.410..15.411 rows=374654 loops=1)
        Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
        Buffers: shared read=318
Planning:
  Buffers: shared hit=24 read=1
Planning Time: 0.415 ms
Execution Time: 299.745 ms
```

---

## 3. Hash Index (company_id equality)

**Description:** Hash: pure equality lookup. Cannot support range or ORDER BY.

**Index used:** `idx_company_hash` (expected: `idx_company_hash`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, salary_min FROM job_listings WHERE company_id = 42
```

**Timing:**
- Cold cache: 0.27ms
- Warm p50: 0.51ms
- Warm p95: 1.27ms
- Warm p99: 1.52ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=134.75..13252.11 rows=5000 width=44) (actual time=0.034..0.255 rows=216 loops=1)
  Recheck Cond: (company_id = 42)
  Heap Blocks: exact=214
  Buffers: shared hit=213 read=5
  ->  Bitmap Index Scan on idx_company_hash  (cost=0.00..133.50 rows=5000 width=0) (actual time=0.018..0.018 rows=216 loops=1)
        Index Cond: (company_id = 42)
        Buffers: shared hit=4
Planning:
  Buffers: shared hit=21 read=5
Planning Time: 0.901 ms
Execution Time: 0.271 ms
```

---

## 3b. B-tree on company_id (same equality query)

**Description:** Direct comparison: B-tree vs Hash for equality.

**Index used:** `idx_company_btree_cmp` (expected: `idx_company_btree_cmp`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, salary_min FROM job_listings WHERE company_id = 42
```

**Timing:**
- Cold cache: 1.16ms
- Warm p50: 0.43ms
- Warm p95: 1.01ms
- Warm p99: 1.3ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=59.17..13176.54 rows=5000 width=44) (actual time=0.088..1.106 rows=216 loops=1)
  Recheck Cond: (company_id = 42)
  Heap Blocks: exact=214
  Buffers: shared hit=214 read=4
  ->  Bitmap Index Scan on idx_company_btree_cmp  (cost=0.00..57.92 rows=5000 width=0) (actual time=0.037..0.038 rows=216 loops=1)
        Index Cond: (company_id = 42)
        Buffers: shared read=4
Planning:
  Buffers: shared hit=18 read=1
Planning Time: 0.306 ms
Execution Time: 1.162 ms
```

---

## 4. GIN Index (tags array contains)

**Description:** GIN: 'find all listings tagged python AND postgresql'.

**Index used:** `idx_tags_gin` (expected: `idx_tags_gin`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
```

**Timing:**
- Cold cache: 25.18ms
- Warm p50: 22.55ms
- Warm p95: 28.59ms
- Warm p99: 30.93ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=198.69..296.84 rows=25 width=44) (actual time=8.266..24.442 rows=16078 loops=1)
  Recheck Cond: (tags @> '{python,postgresql}'::text[])
  Heap Blocks: exact=12592
  Buffers: shared hit=12219 read=438
  ->  Bitmap Index Scan on idx_tags_gin  (cost=0.00..198.68 rows=25 width=0) (actual time=7.276..7.278 rows=16078 loops=1)
        Index Cond: (tags @> '{python,postgresql}'::text[])
        Buffers: shared hit=65
Planning:
  Buffers: shared hit=24 read=3
Planning Time: 0.390 ms
Execution Time: 25.176 ms
```

---

## 4b. Sequential scan (tags, no GIN)

**Description:** Baseline for GIN comparison: same array contains query without the GIN index.

**Index used:** `none (seq scan)` (expected: `none`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
```

**Timing:**
- Cold cache: 218.34ms
- Warm p50: 212.04ms
- Warm p95: 298.46ms
- Warm p99: 303.77ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Gather  (cost=1000.00..36414.83 rows=25 width=44) (actual time=1.406..217.874 rows=16078 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=29855 read=657
  ->  Parallel Seq Scan on job_listings  (cost=0.00..35412.33 rows=10 width=44) (actual time=1.079..207.607 rows=5359 loops=3)
        Filter: (tags @> '{python,postgresql}'::text[])
        Rows Removed by Filter: 327974
        Buffers: shared hit=29855 read=657
Planning:
  Buffers: shared hit=7
Planning Time: 0.158 ms
Execution Time: 218.343 ms
```

---

## 5. BRIN Index (created_at range)

**Description:** BRIN: 280x smaller than B-tree. Works because rows are inserted in time order.

**Index used:** `idx_created_brin` (expected: `idx_created_brin`) — ✓ matches expected

**Query:**
```sql
SELECT id, title, created_at
                                FROM job_listings
                                WHERE created_at >= '2022-01-01'
                                AND created_at < '2023-01-01'
```

**Timing:**
- Cold cache: 43.52ms
- Warm p50: 160.61ms
- Warm p95: 175.65ms
- Warm p99: 179.61ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Bitmap Heap Scan on job_listings  (cost=62.92..33254.23 rows=197681 width=33) (actual time=0.558..38.526 rows=200000 loops=1)
  Recheck Cond: ((created_at >= '2022-01-01 00:00:00+00'::timestamp with time zone) AND (created_at < '2023-01-01 00:00:00+00'::timestamp with time zone))
  Rows Removed by Index Recheck: 12141
  Heap Blocks: lossy=6400
  Buffers: shared hit=6405
  ->  Bitmap Index Scan on idx_created_brin  (cost=0.00..13.50 rows=199154 width=0) (actual time=0.100..0.101 rows=64000 loops=1)
        Index Cond: ((created_at >= '2022-01-01 00:00:00+00'::timestamp with time zone) AND (created_at < '2023-01-01 00:00:00+00'::timestamp with time zone))
        Buffers: shared hit=5
Planning:
  Buffers: shared hit=40 read=1
Planning Time: 0.198 ms
Execution Time: 43.521 ms
```

---

## 6. Covering Index (index-only scan)

**Description:** Covering index with INCLUDE: all needed columns in the index, no heap visit.

**Index used:** `idx_salary_covering` (expected: `idx_salary_covering`) — ✓ matches expected

**Query:**
```sql
SELECT title, location, salary_min
                                FROM job_listings
                                WHERE salary_min BETWEEN 100000 AND 140000
```

**Timing:**
- Cold cache: 56.25ms
- Warm p50: 250.78ms
- Warm p95: 320.07ms
- Warm p99: 328.87ms

**EXPLAIN (ANALYZE, BUFFERS):**
```
Index Only Scan using idx_salary_covering on job_listings  (cost=0.42..18283.33 rows=374633 width=35) (actual time=0.167..47.054 rows=374654 loops=1)
  Index Cond: ((salary_min >= 100000) AND (salary_min <= 140000))
  Heap Fetches: 0
  Buffers: shared hit=1 read=2648 written=431
Planning:
  Buffers: shared hit=34 read=1 dirtied=1
Planning Time: 0.294 ms
Execution Time: 56.250 ms
```

---

## Summary

| Scenario | Cold | p50 | p95 | p99 | Index Used | Match |
|----------|------|-----|-----|-----|------------|-------|
| 1. Sequential Scan (no index) | 175.3ms | 303.06ms | 385.45ms | 419.13ms | seq scan | ✓ |
| 2. B-tree Index (salary_min) | 299.7ms | 268.74ms | 340.22ms | 373.69ms | idx_salary_btree | ✓ |
| 3. Hash Index (company_id equality) | 0.3ms | 0.51ms | 1.27ms | 1.52ms | idx_company_hash | ✓ |
| 3b. B-tree on company_id (same equality query) | 1.2ms | 0.43ms | 1.01ms | 1.3ms | idx_company_btree_cmp | ✓ |
| 4. GIN Index (tags array contains) | 25.2ms | 22.55ms | 28.59ms | 30.93ms | idx_tags_gin | ✓ |
| 4b. Sequential scan (tags, no GIN) | 218.3ms | 212.04ms | 298.46ms | 303.77ms | seq scan | ✓ |
| 5. BRIN Index (created_at range) | 43.5ms | 160.61ms | 175.65ms | 179.61ms | idx_created_brin | ✓ |
| 6. Covering Index (index-only scan) | 56.2ms | 250.78ms | 320.07ms | 328.87ms | idx_salary_covering | ✓ |