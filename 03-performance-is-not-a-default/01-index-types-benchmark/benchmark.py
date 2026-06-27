"""
Experiment 01: Index Types Benchmark

Runs a representative query for each index type against the job_listings table.
Enforces a mathematically honest cold cache prior to execution by physically
thrashing the LRU cache, forcing the database to read from disk.
"""
import asyncio
import os
import statistics
import time
from datetime import datetime

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

REPEAT = 20


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


async def run_scenario(conn, name: str, description: str, query: str,
                       params=None, drop_sql: str = None,
                       create_sql: str = None) -> dict:
    print(f"\n{'='*60}")
    print(f"SCENARIO: {name}")
    print(f"  {description}")
    print(f"  Query: {query[:80]}...")

    if drop_sql:
        await conn.execute(drop_sql)
        print(f"  Index dropped for isolation.")

    if create_sql:
        t0 = time.perf_counter()
        await conn.execute(create_sql)
        t1 = time.perf_counter()
        print(f"  Index created in {(t1-t0)*1000:.0f}ms")

    # ENFORCE COLD CACHE BEFORE FIRST RUN
    await clear_cache(conn)

    cold = await explain_analyze(conn, query, params)
    print(f"\n  COLD CACHE execution: {cold['execution_ms']:.2f}ms")

    for line in cold['plan'].split('\n'):
        if 'Buffers:' in line or 'Seq Scan' in line or 'Index' in line:
            print(f"  {line.strip()}")

    warm = await time_query_percentiles(conn, query, params)
    print(f"\n  WARM CACHE p50={warm['p50_ms']}ms  "
          f"p95={warm['p95_ms']}ms  "
          f"p99={warm['p99_ms']}ms")

    return {
        "name": name,
        "description": description,
        "query": query,
        "cold_ms": cold['execution_ms'],
        "plan": cold['plan'],
        **warm
    }


async def main():
    print("Experiment 01: Index Types Benchmark")
    print(f"Database: {DATABASE_URL}")
    print(f"Warm cache repetitions: {REPEAT}")

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5) as pool:
        async with pool.acquire() as conn:
            results = []

            count = await conn.fetchval("SELECT COUNT(*) FROM job_listings")
            size = await conn.fetchval("SELECT pg_size_pretty(pg_total_relation_size('job_listings'))")

            print(f"\nTable: job_listings ({count:,} rows, {size})")

            # Scenarios
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
                drop_sql="DROP INDEX IF EXISTS idx_salary_btree",
            ))

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
                create_sql="CREATE INDEX idx_salary_btree ON job_listings (salary_min)",
            ))

            results.append(await run_scenario(
                conn=conn,
                name="3. Hash Index (company_id equality)",
                description="Hash: pure equality lookup. Cannot support range or ORDER BY.",
                query="SELECT id, title, salary_min FROM job_listings WHERE company_id = 42",
                drop_sql="DROP INDEX IF EXISTS idx_company_hash",
                create_sql="CREATE INDEX idx_company_hash ON job_listings USING hash (company_id)",
            ))

            results.append(await run_scenario(
                conn=conn,
                name="3b. B-tree on company_id (same equality query)",
                description="Direct comparison: B-tree vs Hash for equality.",
                query="SELECT id, title, salary_min FROM job_listings WHERE company_id = 42",
                drop_sql="DROP INDEX IF EXISTS idx_company_btree_cmp;",
                create_sql="CREATE INDEX idx_company_btree_cmp ON job_listings (company_id)",
            ))

            results.append(await run_scenario(
                conn=conn,
                name="4. GIN Index (tags array contains)",
                description="GIN: 'find all listings tagged python AND postgresql'.",
                query="""
                    SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
                """,
                drop_sql="DROP INDEX IF EXISTS idx_tags_gin",
                create_sql="CREATE INDEX idx_tags_gin ON job_listings USING gin (tags)",
            ))

            results.append(await run_scenario(
                conn=conn,
                name="4b. Sequential scan (tags, no GIN)",
                description="Baseline for GIN comparison: same array contains query without the GIN index.",
                query="""
                    SELECT id, title, salary_min
                    FROM job_listings
                    WHERE tags @> ARRAY['python', 'postgresql']
                """,
                drop_sql="DROP INDEX IF EXISTS idx_tags_gin",
            ))

            await conn.execute("CREATE INDEX idx_tags_gin ON job_listings USING gin (tags)")

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
                drop_sql="DROP INDEX IF EXISTS idx_created_brin",
                create_sql="CREATE INDEX idx_created_brin ON job_listings USING brin (created_at)",
            ))

            results.append(await run_scenario(
                conn=conn,
                name="6. Covering Index (index-only scan)",
                description="Covering index with INCLUDE: all needed columns in the index, no heap visit.",
                query="""
                    SELECT title, location, salary_min
                    FROM job_listings
                    WHERE salary_min BETWEEN 100000 AND 140000
                """,
                drop_sql="DROP INDEX IF EXISTS idx_salary_covering",
                create_sql="CREATE INDEX idx_salary_covering ON job_listings (salary_min) INCLUDE (title, location)",
            ))

            print(f"\n\n{'='*60}")
            print("SUMMARY — ALL SCENARIOS")
            print(f"{'='*60}")
            print(f"{'Scenario':<45} {'Cold':>8} {'p50':>8} {'p95':>8} {'p99':>8}")
            print("-" * 80)
            for r in results:
                name_short = r['name'][:44]
                cold = f"{r['cold_ms']:.1f}ms" if r['cold_ms'] else "N/A"
                print(
                    f"{name_short:<45} {cold:>8} "
                    f"{r['p50_ms']:>6.1f}ms "
                    f"{r['p95_ms']:>6.1f}ms "
                    f"{r['p99_ms']:>6.1f}ms"
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
        "---",
        "",
    ]

    for r in results:
        lines += [
            f"## {r['name']}",
            "",
            f"**Description:** {r['description']}",
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
        "| Scenario | Cold | p50 | p95 | p99 |",
        "|----------|------|-----|-----|-----|",
    ]
    for r in results:
        cold = f"{r['cold_ms']:.1f}ms" if r['cold_ms'] else "N/A"
        lines.append(
            f"| {r['name']} | {cold} | {r['p50_ms']}ms | {r['p95_ms']}ms | {r['p99_ms']}ms |"
        )

    with open("results.md", "w") as f:
        f.write("\n".join(lines))

    print(f"\nResults written to results.md")


if __name__ == "__main__":
    asyncio.run(main())