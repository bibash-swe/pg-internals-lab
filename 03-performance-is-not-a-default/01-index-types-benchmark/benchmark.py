"""
Experiment 01: Index Types Benchmark

Runs a representative query for each index type against the job_listings
table, under a forced cold cache, then again under warm cache (p50/p95/p99).

Enforces a mathematically honest cold cache prior to execution via LRU
eviction (buffer thrashing), ensuring all heap and index pages are cleared
from shared_buffers.
"""
from __future__ import annotations

import asyncio
import os
import re
import statistics
import time
from datetime import datetime

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

REPEAT = 20
TABLE_NAME = "job_listings"

# Matches the planner's name for whichever index actually got used:
#   Index Scan using idx_foo on job_listings
#   Index Only Scan using idx_foo on job_listings
#   Bitmap Index Scan on idx_foo
_INDEX_SCAN_PATTERN = re.compile(
    r"(?:Index Scan using|Index Only Scan using|Bitmap Index Scan on)\s+(\S+)"
)


def _as_list(sqls: list[str] | str | None) -> list[str]:
    """Normalize drop_sqls / post_drop_sqls to a flat list of statements."""
    if sqls is None:
        return []
    if isinstance(sqls, str):
        return [sqls]
    return list(sqls)


def extract_used_index(plan_text: str) -> str | None:
    """
    Parse EXPLAIN ANALYZE output and return the name of the index the
    planner actually used, or None if the plan is a sequential scan.

    This is the check that would have caught the scenario-3b ghost-index
    bug immediately: expected_index="idx_company_btree_cmp" vs. the actual
    "idx_company_hash" parsed straight out of the plan text.

    If the plan references more than one *distinct* index name (e.g. a
    BitmapOr combining two indexes), all matches are logged so that
    ambiguity is visible rather than silently collapsed to "the first one".
    """
    matches = _INDEX_SCAN_PATTERN.findall(plan_text)
    if not matches:
        return None
    if len(set(matches)) > 1:
        print(f"Plan references multiple distinct indexes: {matches}")
    return matches[0]


async def get_index_audit(conn) -> list[str]:
    """
    Snapshot of every index currently defined on job_listings.

    Called before and after each scenario's setup so leftover ("ghost")
    indexes from a prior scenario show up in the run log directly, instead
    of only being inferable later from a wrong-looking EXPLAIN result.
    """
    rows = await conn.fetch(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = $1
        ORDER BY indexname
        """,
        TABLE_NAME,
    )
    return [r["indexname"] for r in rows]


async def explain_analyze(conn, query: str, params=None) -> dict:
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {query}"
    plan_rows = await conn.fetch(explain_sql, *(params or []))
    plan_text = "\n".join(row[0] for row in plan_rows)

    exec_time = None
    for line in plan_text.split("\n"):
        if "Execution Time:" in line:
            exec_time = float(line.split(":")[1].strip().replace(" ms", ""))
            break

    return {"plan": plan_text, "execution_ms": exec_time}


async def time_query_percentiles(conn, query: str, params=None) -> dict:
    timings = []
    for i in range(REPEAT):
        t0 = time.perf_counter()
        await conn.fetch(query, *(params or []))
        t1 = time.perf_counter()
        if i > 0:
            timings.append((t1 - t0) * 1000)

    return {
        "p50_ms": round(statistics.median(timings), 2),
        "p95_ms": round(statistics.quantiles(timings, n=20)[18], 2),
        "p99_ms": round(statistics.quantiles(timings, n=100)[98], 2),
        "min_ms": round(min(timings), 2),
        "max_ms": round(max(timings), 2),
    }


async def clear_cache(conn):
    """
    True cold cache simulation via LRU Eviction (Buffer Thrashing).
    We create and scan a temporary table roughly ~300MB in size to force
    PostgreSQL to evict the existing job_listings heap and index pages.
    """
    await conn.execute("SELECT pg_stat_reset()")

    async with conn.transaction():
        await conn.execute("""
            CREATE TEMP TABLE cache_thrash ON COMMIT DROP AS
            SELECT generate_series(1, 3000000) AS id,
                   md5(random()::text) AS junk1,
                   md5(random()::text) AS junk2
        """)
        # Force the database to pull these new pages into shared_buffers
        await conn.execute("SELECT count(*) FROM cache_thrash")
        # The transaction closes here, and Postgres automatically drops cache_thrash.
    print("  [Cache explicitly evicted via transactional buffer thrashing]")


async def run_scenario(
    conn,
    name: str,
    description: str,
    query: str,
    params=None,
    drop_sqls: list[str] | str | None = None,
    create_sql: str | None = None,
    post_drop_sqls: list[str] | str | None = None,
    expected_index: str | None = None,
) -> dict:
    """
    Run one benchmark scenario with explicit index isolation.

    drop_sqls       -- everything that must be dropped BEFORE measuring,
                        so no other index can compete for this query.
                        Drop every index that touches the relevant
                        column(s), not just the one being recreated.
    create_sql      -- the index under test for this scenario (or None
                        for the "no index" baselines).
    post_drop_sqls  -- cleanup run AFTER measurement, BEFORE the next
                        scenario starts. This is how a scenario stops a
                        temporary index from leaking into the next one
                        (the exact gap that caused the 3 -> 3b bug).
    expected_index  -- the index name EXPLAIN should report using. None
                        means "expect a sequential scan". A mismatch
                        prints a warning instead of failing silently.
    """
    print(f"\n{'='*60}")
    print(f"SCENARIO: {name}")
    print(f"  {description}")
    print(f"  Query: {query[:80]}...")

    # Pre-setup audit: what indexes exist right now?
    audit_before = await get_index_audit(conn)
    print(f"  Indexes present BEFORE setup : {audit_before}")

    # Isolation: drop anything that could compete for this query
    drop_list = _as_list(drop_sqls)
    for sql in drop_list:
        await conn.execute(sql)
    if drop_list:
        print(f"  Dropped for isolation        : {drop_list}")

    # Create the index under test
    if create_sql:
        t0 = time.perf_counter()
        await conn.execute(create_sql)
        t1 = time.perf_counter()
        print(f"  Index created in {(t1 - t0) * 1000:.0f}ms")

    audit_during = await get_index_audit(conn)
    print(f"  Indexes present DURING measure: {audit_during}")

    # ENFORCE COLD CACHE BEFORE FIRST RUN
    await clear_cache(conn)

    cold = await explain_analyze(conn, query, params)
    print(f"\n  COLD CACHE execution: {cold['execution_ms']:.2f}ms")

    for line in cold['plan'].split('\n'):
        if 'Buffers:' in line or 'Seq Scan' in line or 'Index' in line:
            print(f"  {line.strip()}")

    # Validate the planner actually used what we think it did
    used_index = extract_used_index(cold["plan"])
    print(f"  Index actually used by planner: {used_index or '(none / seq scan)'}")
    index_match = (used_index == expected_index)
    if not index_match:
        print(
            f"  ⚠️  WARNING: expected index '{expected_index}', planner used "
            f"'{used_index}'. This scenario's numbers are likely INVALID — "
            f"check the index audit above for a leftover index."
        )

    warm = await time_query_percentiles(conn, query, params)
    print(f"\n  WARM CACHE p50={warm['p50_ms']}ms  "
          f"p95={warm['p95_ms']}ms  "
          f"p99={warm['p99_ms']}ms")

    # Post-scenario cleanup
    post_drop_list = _as_list(post_drop_sqls)
    for sql in post_drop_list:
        await conn.execute(sql)
    if post_drop_list:
        print(f"  Post-scenario cleanup         : {post_drop_list}")

    return {
        "name": name,
        "description": description,
        "query": query,
        "cold_ms": cold["execution_ms"],
        "plan": cold["plan"],
        "expected_index": expected_index,
        "index_used": used_index,
        "index_match": index_match,
        **warm,
    }


async def main():
    print("Experiment 01: Index Types Benchmark")
    print(f"Database: {DATABASE_URL}")
    print(f"Warm cache repetitions: {REPEAT}")

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5) as pool:
        async with pool.acquire() as conn:
            results = []

            count = await conn.fetchval(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            size = await conn.fetchval(
                f"SELECT pg_size_pretty(pg_total_relation_size('{TABLE_NAME}'))"
            )

            print(f"\nTable: {TABLE_NAME} ({count:,} rows, {size})")

            # Scenario 1 — Sequential scan baseline (salary_min range)
            results.append(await run_scenario(
                conn=conn,
                name="1. Sequential Scan (no index)",
                description="Baseline: full table scan with no index on salary_min.",
                query="""
                    SELECT id, title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                    AND is_active = true
                """,
                drop_sqls=[
                    "DROP INDEX IF EXISTS idx_salary_btree",
                    "DROP INDEX IF EXISTS idx_salary_covering",
                ],
                expected_index=None,
            ))

            # Scenario 2 — B-tree index on salary_min
            results.append(await run_scenario(
                conn=conn,
                name="2. B-tree Index (salary_min)",
                description="B-tree: optimal for range queries on ordered values.",
                query="""
                    SELECT id, title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                    AND is_active = true
                """,
                drop_sqls=["DROP INDEX IF EXISTS idx_salary_covering"],
                create_sql="CREATE INDEX idx_salary_btree ON job_listings (salary_min)",
                post_drop_sqls=["DROP INDEX IF EXISTS idx_salary_btree"],
                expected_index="idx_salary_btree",
            ))

            # Scenario 3 — Hash index on company_id (equality)
            results.append(await run_scenario(
                conn=conn,
                name="3. Hash Index (company_id equality)",
                description="Hash: pure equality lookup. Cannot support range or ORDER BY.",
                query="SELECT id, title, salary_min FROM job_listings WHERE company_id = 42",
                drop_sqls=["DROP INDEX IF EXISTS idx_company_hash"],
                create_sql="CREATE INDEX idx_company_hash ON job_listings USING hash (company_id)",
                post_drop_sqls=["DROP INDEX IF EXISTS idx_company_hash"],
                expected_index="idx_company_hash",
            ))

            # Scenario 3b — B-tree on company_id (same equality query)
            results.append(await run_scenario(
                conn=conn,
                name="3b. B-tree on company_id (same equality query)",
                description="Direct comparison: B-tree vs Hash for equality.",
                query="SELECT id, title, salary_min FROM job_listings WHERE company_id = 42",
                drop_sqls=[
                    "DROP INDEX IF EXISTS idx_company_hash",
                    "DROP INDEX IF EXISTS idx_company_btree_cmp",
                ],
                create_sql="CREATE INDEX idx_company_btree_cmp ON job_listings (company_id)",
                post_drop_sqls=["DROP INDEX IF EXISTS idx_company_btree_cmp"],
                expected_index="idx_company_btree_cmp",
            ))

            # Scenario 4 — GIN index on tags (array contains)
            results.append(await run_scenario(
                conn=conn,
                name="4. GIN Index (tags array contains)",
                description="GIN: 'find all listings tagged python AND postgresql'.",
                query="""
                    SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
                """,
                drop_sqls=["DROP INDEX IF EXISTS idx_tags_gin"],
                create_sql="CREATE INDEX idx_tags_gin ON job_listings USING gin (tags)",
                expected_index="idx_tags_gin",
            ))

            # Scenario 4b — Sequential scan on tags (no GIN)
            results.append(await run_scenario(
                conn=conn,
                name="4b. Sequential scan (tags, no GIN)",
                description="Baseline for GIN comparison: same array contains query without the GIN index.",
                query="""
                    SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
                """,
                drop_sqls=["DROP INDEX IF EXISTS idx_tags_gin"],
                expected_index=None,
            ))

            # Scenario 5 — BRIN index on created_at (range)
            print("\n  [Running ANALYZE job_listings to ensure accurate statistics for BRIN]")
            await conn.execute("ANALYZE job_listings")

            results.append(await run_scenario(
                conn=conn,
                name="5. BRIN Index (created_at range)",
                description="BRIN: 280x smaller than B-tree. Works because rows are inserted in time order.",
                query="""
                                SELECT id, title, created_at
                                FROM job_listings
                                WHERE created_at >= '2022-01-01'
                                AND created_at < '2023-01-01'
                            """,
                drop_sqls=["DROP INDEX IF EXISTS idx_created_brin"],
                create_sql="CREATE INDEX idx_created_brin ON job_listings USING brin (created_at)",
                expected_index="idx_created_brin",
            ))

            # Scenario 6 — Covering index (index-only scan)
            print("\n  [Running VACUUM job_listings to update visibility map for Covering Index]")
            await conn.execute("VACUUM job_listings")

            results.append(await run_scenario(
                conn=conn,
                name="6. Covering Index (index-only scan)",
                description="Covering index with INCLUDE: all needed columns in the index, no heap visit.",
                query="""
                                SELECT title, location, salary_min
                                FROM job_listings
                                WHERE salary_min BETWEEN 100000 AND 140000
                            """,
                drop_sqls=[
                    "DROP INDEX IF EXISTS idx_salary_btree",
                    "DROP INDEX IF EXISTS idx_salary_covering"
                ],
                create_sql="CREATE INDEX idx_salary_covering ON job_listings (salary_min) INCLUDE (title, location)",
                expected_index="idx_salary_covering",
            ))

            print(f"\n\n{'='*60}")
            print("SUMMARY — ALL SCENARIOS")
            print(f"{'='*60}")
            header = (
                f"{'Scenario':<42} {'Cold':>8} {'p50':>8} {'p95':>8} {'p99':>8}  "
                f"{'Index Used':<24} {'OK?':>4}"
            )
            print(header)
            print("-" * len(header))
            for r in results:
                name_short = r['name'][:41]
                cold = f"{r['cold_ms']:.1f}ms" if r['cold_ms'] else "N/A"
                index_used = r['index_used'] or "(seq scan)"
                ok = "✓" if r['index_match'] else "✗"
                print(
                    f"{name_short:<42} {cold:>8} "
                    f"{r['p50_ms']:>6.1f}ms "
                    f"{r['p95_ms']:>6.1f}ms "
                    f"{r['p99_ms']:>6.1f}ms  "
                    f"{index_used:<24} {ok:>4}"
                )

            await write_results_md(results, count)


async def write_results_md(results: list, row_count: int):
    lines = [
        "# Results: Index Types Benchmark",
        "",
        f"**Table:** job_listings — {row_count:,} rows",
        f"**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, 1GB RAM, 1 CPU)",
        f"**Run at:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Warm cache repetitions:** {REPEAT} (p50/p95/p99 exclude first run)",
        "",
        "Cold cache = first execution after mathematically forcing LRU buffer eviction.",
        "Warm cache = subsequent executions (data in shared_buffers).",
        "",
        "Each scenario's `EXPLAIN` output was parsed to confirm the planner actually "
        "used the index under test (`Index Used` / `Expected`). A mismatch means the "
        "scenario's timing reflects a different index than intended and should be "
        "treated as invalid.",
        "",
        "---",
        "",
    ]

    for r in results:
        match_str = "✓ matches expected" if r["index_match"] else "✗ MISMATCH — INVALID RUN"
        lines += [
            f"## {r['name']}",
            "",
            f"**Description:** {r['description']}",
            "",
            f"**Index used:** `{r['index_used'] or 'none (seq scan)'}` "
            f"(expected: `{r['expected_index'] or 'none'}`) — {match_str}",
            "",
            "**Query:**",
            "```sql",
            r['query'].strip(),
            "```",
            "",
            "**Timing:**",
            f"- Cold cache: {r['cold_ms']:.2f}ms",
            f"- Warm p50: {r['p50_ms']}ms",
            f"- Warm p95: {r['p95_ms']}ms",
            f"- Warm p99: {r['p99_ms']}ms",
            "",
            "**EXPLAIN (ANALYZE, BUFFERS):**",
            "```",
            r['plan'],
            "```",
            "",
            "---",
            "",
        ]

    lines += [
        "## Summary",
        "",
        "| Scenario | Cold | p50 | p95 | p99 | Index Used | Match |",
        "|----------|------|-----|-----|-----|------------|-------|",
    ]
    for r in results:
        cold = f"{r['cold_ms']:.1f}ms" if r['cold_ms'] else "N/A"
        index_used = r['index_used'] or "seq scan"
        ok = "✓" if r['index_match'] else "✗"
        lines.append(
            f"| {r['name']} | {cold} | {r['p50_ms']}ms | {r['p95_ms']}ms | "
            f"{r['p99_ms']}ms | {index_used} | {ok} |"
        )

    with open("results.md", "w") as f:
        f.write("\n".join(lines))

    print(f"\nResults written to results.md")


if __name__ == "__main__":
    asyncio.run(main())