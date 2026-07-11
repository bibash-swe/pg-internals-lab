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

TWO REAL MISTAKES MADE AND CORRECTED WHILE BUILDING THIS BENCHMARK --
WORTH READING BEFORE TRUSTING THE NUMBERS BELOW:

    Mistake 1: an earlier version added `, id` as a secondary ORDER BY
    sort key, hypothesizing that ties in cosine distance (from
    template-heavy synthetic text) were causing unstable recall
    measurements. This was WRONG, and the fix made things worse, not
    better: an approximate HNSW index cannot guarantee a deterministic
    tie-break order, so demanding one via `, id` forced PostgreSQL to
    abandon the index entirely, for EVERY configuration, permanently.
    The resulting "100% recall every time" was trivial and meaningless
    -- an exact scan was being compared against itself, because HNSW
    was never actually invoked. This is reverted below: ordering is by
    cosine distance alone.

    Mistake 2 (the actual likely explanation): real Mistral embeddings
    are high-precision floats -- genuine exact ties are exceedingly
    unlikely. The original recall variability (a single config showing
    different recall on different runs) is much more plausibly
    explained by PostgreSQL's planner making DIFFERENT scan-method
    decisions across different individual query executions within the
    same 50-query loop, when the estimated cost of using the index
    versus scanning the table is close. Some of the 50 queries in a
    borderline config may be serviced by the index (approximate), and
    others may fall back to sequential scan (exact) -- and the
    resulting AVERAGE recall is a blend of both, which looks unstable
    across runs without being a bug at all.

    Because of this, a single EXPLAIN spot-check (on one query) cannot
    tell the whole story for a config near the planner's cost decision
    boundary. This version classifies EVERY one of the 50 timed queries
    by latency (a documented, approximate heuristic, not a certainty)
    and reports what fraction of executions likely used the index
    versus likely fell back to a full scan, for every configuration.

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

# Latency-based scan-method classification threshold. This is a
# documented heuristic, not a certainty: brute-force baseline queries
# on this 10,000-row table run at roughly 1-40ms depending on cache
# state (see baseline output each run), while genuine HNSW-serviced
# queries run at roughly 1-3ms. 10ms sits clearly between the two
# observed clusters. A query slower than this threshold is classified
# as "likely sequential scan"; faster is classified as "likely index".
SEQ_SCAN_LATENCY_THRESHOLD_MS = 10.0


async def get_test_query_ids(conn: asyncpg.Connection) -> list[int]:
    rows = await conn.fetch(
        "SELECT id FROM support_articles ORDER BY random() LIMIT $1",
        NUM_TEST_QUERIES
    )
    return [r["id"] for r in rows]


async def brute_force_top_k(conn: asyncpg.Connection, query_id: int) -> list[int]:
    """
    Exact nearest neighbors via full cosine distance computation, no
    index. Ordered by distance alone -- see module docstring for why
    a secondary tiebreak was tried and reverted.
    """
    rows = await conn.fetch(f"""
        SELECT id FROM support_articles
        WHERE id != $1
        ORDER BY embedding <=> (SELECT embedding FROM support_articles WHERE id = $1)
        LIMIT {TOP_K}
    """, query_id)
    return [r["id"] for r in rows]


async def hnsw_top_k(conn: asyncpg.Connection, query_id: int) -> list[int]:
    """Same query, serviced by whichever plan the planner chooses."""
    rows = await conn.fetch(f"""
        SELECT id FROM support_articles
        WHERE id != $1
        ORDER BY embedding <=> (SELECT embedding FROM support_articles WHERE id = $1)
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

    # Standard practice after building any new index -- ensures the
    # planner has current statistics, even though (as this file's
    # docstring explains) this alone does not explain the original
    # recall variability.
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
    One-time EXPLAIN spot-check on a single query. Useful as a quick
    sanity signal, but NOT sufficient on its own for configs near the
    planner's cost decision boundary -- see per-query classification
    in run_hnsw_sweep for the full picture across all 50 test queries.
    """
    plan_rows = await conn.fetch(f"""
        EXPLAIN SELECT id FROM support_articles
        WHERE id != {query_id}
        ORDER BY embedding <=> (SELECT embedding FROM support_articles WHERE id = {query_id})
        LIMIT {TOP_K}
    """)
    plan_text = "\n".join(r[0] for r in plan_rows)
    uses_hnsw = "idx_support_embedding_hnsw" in plan_text
    print(f"  Spot-check (1 query): "
          f"{'HNSW index used' if uses_hnsw else 'sequential scan used'}")
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

        await verify_index_is_used(conn, query_ids[0])

        for ef_search in EF_SEARCH_VALUES:
            await conn.execute(f"SET hnsw.ef_search = {ef_search}")

            latencies_ms = []
            recalls = []
            likely_index_count = 0
            likely_seqscan_count = 0

            for qid in query_ids:
                t0 = time.perf_counter()
                result = await hnsw_top_k(conn, qid)
                t1 = time.perf_counter()

                latency_ms = (t1 - t0) * 1000
                latencies_ms.append(latency_ms)
                recalls.append(compute_recall(result, ground_truth[qid]))

                if latency_ms >= SEQ_SCAN_LATENCY_THRESHOLD_MS:
                    likely_seqscan_count += 1
                else:
                    likely_index_count += 1

            perf = percentiles(latencies_ms)
            avg_recall = round(statistics.mean(recalls), 4)

            print(f"\n  ef_search={ef_search}:")
            print(f"    Latency (ms): p50={perf['p50_ms']}  p95={perf['p95_ms']}  "
                  f"p99={perf['p99_ms']}")
            print(f"    Recall@{TOP_K}: {avg_recall * 100:.1f}%")
            print(f"    Scan method mix (by latency, threshold="
                  f"{SEQ_SCAN_LATENCY_THRESHOLD_MS}ms): "
                  f"{likely_index_count}/{NUM_TEST_QUERIES} likely index, "
                  f"{likely_seqscan_count}/{NUM_TEST_QUERIES} likely seq scan")

            results.append({
                "m": config["m"],
                "ef_construction": config["ef_construction"],
                "ef_search": ef_search,
                "build_time_sec": build_info["build_time_sec"],
                "index_size_pretty": build_info["size_pretty"],
                "index_size_bytes": build_info["size_bytes"],
                "latency": perf,
                "recall": avg_recall,
                "likely_index_count": likely_index_count,
                "likely_seqscan_count": likely_seqscan_count,
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
                  f"{'p95(ms)':>9} {'recall':>8} {'idx/seq mix':>14}")
        print(header)
        print("-" * len(header))
        for r in sweep_results:
            mix = f"{r['likely_index_count']}/{r['likely_seqscan_count']}"
            print(f"{r['m']:>4} {r['ef_construction']:>10} {r['ef_search']:>10} "
                  f"{r['build_time_sec']:>9} {r['index_size_pretty']:>10} "
                  f"{r['latency']['p50_ms']:>9} {r['latency']['p95_ms']:>9} "
                  f"{r['recall']*100:>7.1f}% {mix:>14}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())