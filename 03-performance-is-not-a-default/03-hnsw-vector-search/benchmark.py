"""
Three things measured, in this order:

  1. BRUTE FORCE GROUND TRUTH (before any index exists):
     Exact top-10 nearest neighbors via full cosine distance
     computation, sequential scan, no index.

  2. HNSW BUILD-TIME PARAMETER SWEEP (m, ef_construction):
     Build time, index size, and downstream recall/latency at each
     configuration.

  3. HNSW RUNTIME PARAMETER SWEEP (ef_search):
     Same index, different ef_search values -- trades query latency
     for recall live, without rebuilding anything.

A REAL BUG FOUND AND FIXED DURING THIS EXPERIMENT -- WORTH READING:
    An earlier version of this benchmark showed a confirmed exact
    sequential scan producing only ~58-61% recall against its own
    ground truth on some runs, and 100% on others, with no code
    changed in between. An exact scan disagreeing with its own
    ground truth is only possible one way: `ORDER BY embedding <=>
    (...)` had no secondary sort key. This dataset's synthetic text
    is built from only 5 templates per category, producing many rows
    with identical or near-identical cosine distance to any given
    query. Without a deterministic tiebreak, PostgreSQL makes no
    promise about which tied row comes first -- so two "exact"
    executions of the same logical query could legitimately return
    different (but equally valid) top-10 sets. Both queries below
    now sort by `, id` as a secondary key, making ties -- and
    therefore recall comparisons -- fully reproducible.

    ANALYZE after CREATE INDEX is also kept, since a freshly built
    index without updated statistics can cause inconsistent plan
    choices -- the same stale-statistics mechanism proven in
    Experiment 01 -- but the tiebreak, not ANALYZE, is what actually
    resolves the exact-scan self-disagreement described above.

Run:
    python benchmark.py
"""
import asyncio
import os
import statistics
import time
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/lab3"
)

NUM_TEST_QUERIES = 50
TOP_K = 10

HNSW_CONFIGS = [
    {"m": 8, "ef_construction": 64},
    {"m": 16, "ef_construction": 64},
    {"m": 16, "ef_construction": 128},
    {"m": 32, "ef_construction": 200},
]

EF_SEARCH_VALUES = [40, 100, 200]


async def get_test_query_ids(conn: asyncpg.Connection) -> list[int]:
    rows = await conn.fetch(
        "SELECT id FROM support_articles ORDER BY random() LIMIT $1",
        NUM_TEST_QUERIES
    )
    return [r["id"] for r in rows]


async def brute_force_top_k(conn: asyncpg.Connection, query_id: int) -> list[int]:
    """
    Exact nearest neighbors via full cosine distance computation.
    `, id` is a DETERMINISTIC TIEBREAK -- without it, rows with equal
    or near-equal cosine distance (common in this dataset, built from
    only 5 templates per category) can be returned in a different
    order across separate executions of this same logical query,
    even though each execution is individually "correct." This is
    the fix for the exact-scan self-disagreement described in the
    module docstring.
    """
    rows = await conn.fetch(f"""
        SELECT id FROM support_articles
        WHERE id != $1
        ORDER BY embedding <=> (SELECT embedding FROM support_articles WHERE id = $1), id
        LIMIT {TOP_K}
    """, query_id)
    return [r["id"] for r in rows]


async def hnsw_top_k(conn: asyncpg.Connection, query_id: int) -> list[int]:
    """
    Same query, same deterministic tiebreak, now serviced by
    whichever HNSW index currently exists (or by a planner-chosen
    sequential scan, if the planner judges the index not worth
    using -- see the m=8 finding in result_analysis.md).
    """
    rows = await conn.fetch(f"""
        SELECT id FROM support_articles
        WHERE id != $1
        ORDER BY embedding <=> (SELECT embedding FROM support_articles WHERE id = $1), id
        LIMIT {TOP_K}
    """, query_id)
    return [r["id"] for r in rows]


def compute_recall(hnsw_result: list[int], ground_truth: list[int]) -> float:
    return len(set(hnsw_result) & set(ground_truth)) / len(ground_truth)


def percentiles(values_ms: list[float]) -> dict:
    return {
        "p50_ms": round(statistics.median(values_ms), 3),
        "p95_ms": round(statistics.quantiles(values_ms, n=20)[18], 3),
        "p99_ms": round(statistics.quantiles(values_ms, n=100)[98], 3),
        "mean_ms": round(statistics.mean(values_ms), 3),
    }


async def run_brute_force_baseline(conn: asyncpg.Connection, query_ids: list[int]) -> dict:
    print(f"\n{'='*60}")
    print(f"BASELINE: Brute force exact search (no index)")
    print(f"{'='*60}")

    ground_truth = {}
    latencies_ms = []

    for qid in query_ids:
        t0 = time.perf_counter()
        result = await brute_force_top_k(conn, qid)
        t1 = time.perf_counter()
        ground_truth[qid] = result
        latencies_ms.append((t1 - t0) * 1000)

    perf = percentiles(latencies_ms)
    print(f"  {NUM_TEST_QUERIES} queries, exact top-{TOP_K} each")
    print(f"  Latency (ms): p50={perf['p50_ms']}  p95={perf['p95_ms']}  "
          f"p99={perf['p99_ms']}  mean={perf['mean_ms']}")

    return {"ground_truth": ground_truth, "latency": perf}


async def build_hnsw_index(conn: asyncpg.Connection, m: int, ef_construction: int) -> dict:
    await conn.execute("DROP INDEX IF EXISTS idx_support_embedding_hnsw")

    t0 = time.perf_counter()
    await conn.execute(f"""
        CREATE INDEX idx_support_embedding_hnsw
        ON support_articles
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = {m}, ef_construction = {ef_construction})
    """)
    build_time_sec = time.perf_counter() - t0

    # See module docstring: this alone does not fix the tiebreak
    # issue, but is kept since it is still correct, standard practice
    # after building any new index.
    await conn.execute("ANALYZE support_articles")

    size = await conn.fetchval(
        "SELECT pg_size_pretty(pg_relation_size('idx_support_embedding_hnsw'))"
    )
    size_bytes = await conn.fetchval(
        "SELECT pg_relation_size('idx_support_embedding_hnsw')"
    )

    return {
        "m": m,
        "ef_construction": ef_construction,
        "build_time_sec": round(build_time_sec, 2),
        "size_pretty": size,
        "size_bytes": size_bytes,
    }


async def verify_index_is_used(conn: asyncpg.Connection, query_id: int) -> bool:
    """
    Explicit check of whether the planner actually chose the HNSW
    index -- not assumed. Also reveals a genuine, separate finding:
    at low m, the planner may judge the index not worth using at
    all, regardless of ef_search, and correctly fall back to an
    exact sequential scan.
    """
    plan_rows = await conn.fetch(f"""
        EXPLAIN SELECT id FROM support_articles
        WHERE id != {query_id}
        ORDER BY embedding <=> (SELECT embedding FROM support_articles WHERE id = {query_id}), id
        LIMIT {TOP_K}
    """)
    plan_text = "\n".join(r[0] for r in plan_rows)
    uses_hnsw = "idx_support_embedding_hnsw" in plan_text
    print(f"  Index verification: "
          f"{'HNSW index confirmed in use' if uses_hnsw else 'Planner chose sequential scan instead (see result_analysis.md)'}")
    if not uses_hnsw:
        print(f"  Plan was:\n{plan_text}")
    return uses_hnsw


async def run_hnsw_sweep(conn: asyncpg.Connection, query_ids: list[int],
                          ground_truth: dict) -> list[dict]:
    results = []

    for config in HNSW_CONFIGS:
        print(f"\n{'='*60}")
        print(f"HNSW CONFIG: m={config['m']}  ef_construction={config['ef_construction']}")
        print(f"{'='*60}")

        build_info = await build_hnsw_index(conn, config["m"], config["ef_construction"])
        print(f"  Build time: {build_info['build_time_sec']}s")
        print(f"  Index size: {build_info['size_pretty']}")

        index_used = await verify_index_is_used(conn, query_ids[0])

        for ef_search in EF_SEARCH_VALUES:
            await conn.execute(f"SET hnsw.ef_search = {ef_search}")

            latencies_ms = []
            recalls = []

            for qid in query_ids:
                t0 = time.perf_counter()
                result = await hnsw_top_k(conn, qid)
                t1 = time.perf_counter()

                latencies_ms.append((t1 - t0) * 1000)
                recalls.append(compute_recall(result, ground_truth[qid]))

            perf = percentiles(latencies_ms)
            avg_recall = round(statistics.mean(recalls), 4)

            print(f"\n  ef_search={ef_search}:")
            print(f"    Latency (ms): p50={perf['p50_ms']}  p95={perf['p95_ms']}  "
                  f"p99={perf['p99_ms']}")
            print(f"    Recall@{TOP_K}: {avg_recall * 100:.1f}%")

            results.append({
                "m": config["m"],
                "ef_construction": config["ef_construction"],
                "ef_search": ef_search,
                "build_time_sec": build_info["build_time_sec"],
                "index_size_pretty": build_info["size_pretty"],
                "index_size_bytes": build_info["size_bytes"],
                "index_used": index_used,
                "latency": perf,
                "recall": avg_recall,
            })

    return results


async def main():
    print("Experiment 03: HNSW Vector Search Benchmark")
    print(f"Database: {DATABASE_URL}")

    conn = await asyncpg.connect(DATABASE_URL)
    await register_vector(conn)

    try:
        count = await conn.fetchval("SELECT COUNT(*) FROM support_articles")
        print(f"support_articles: {count:,} rows")

        if count < 1000:
            print("WARNING: fewer than 1,000 rows. Recall and latency "
                  "results may not be representative at this scale.")

        query_ids = await get_test_query_ids(conn)
        print(f"Selected {len(query_ids)} random test queries")

        baseline = await run_brute_force_baseline(conn, query_ids)
        sweep_results = await run_hnsw_sweep(conn, query_ids, baseline["ground_truth"])

        print(f"\n\n{'='*60}")
        print("SUMMARY — ALL CONFIGURATIONS")
        print(f"{'='*60}")
        print(f"Baseline (brute force): p50={baseline['latency']['p50_ms']}ms  "
              f"p95={baseline['latency']['p95_ms']}ms")
        print()
        header = (f"{'m':>4} {'ef_constr':>10} {'ef_search':>10} "
                  f"{'build(s)':>9} {'size':>10} {'p50(ms)':>9} "
                  f"{'p95(ms)':>9} {'recall':>8} {'index_used':>11}")
        print(header)
        print("-" * len(header))
        for r in sweep_results:
            print(f"{r['m']:>4} {r['ef_construction']:>10} {r['ef_search']:>10} "
                  f"{r['build_time_sec']:>9} {r['index_size_pretty']:>10} "
                  f"{r['latency']['p50_ms']:>9} {r['latency']['p95_ms']:>9} "
                  f"{r['recall']*100:>7.1f}% {str(r['index_used']):>11}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())