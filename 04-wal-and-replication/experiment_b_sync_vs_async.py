"""
Synchronous vs Asynchronous Commit Latency

Measures the real cost of the durability/latency tradeoff explained
in the WAL/replication walkthrough: does the primary wait for
replica1's confirmation before telling the client "committed," or
does it confirm immediately and ship the WAL separately?

This ONLY produces a meaningful difference because
synchronous_standby_names='replica1' is already set at the system
level (via ALTER SYSTEM + pg_reload_conf(), done before this script
exists). Without that, synchronous_commit=on has no named standby to
wait for and silently behaves exactly like async -- see this lab's
README for the full explanation of that trap.

synchronous_commit is deliberately toggled per SESSION here via a
plain `SET` command, not by touching synchronous_standby_names again.
This mirrors a real production pattern: synchronous_standby_names
defines WHO is eligible to act as the synchronous confirming replica
(a system-wide, rarely-changed setting), while synchronous_commit is
the per-transaction or per-session knob controlling WHETHER to
actually wait for that confirmation on this specific write. A real
application commonly runs most transactions async and flips a
specific critical transaction (e.g. a financial transfer) to sync
just for that one commit.

Run:
    python experiment_b_sync_vs_async.py

Requires the primary (port 5434) with synchronous_standby_names
already set to 'replica1' and the replica actively streaming
(sync_state = 'sync' when checked via pg_stat_replication).
"""
import asyncio
import os
import statistics
import time
import uuid

import asyncpg

PRIMARY_URL = os.getenv(
    "PRIMARY_URL", "postgresql://postgres:postgres@localhost:5434/wal_lab"
)

SAMPLE_COUNT = 200


async def measure_commit_latency(conn: asyncpg.Connection, mode: str) -> dict:
    """
    Runs SAMPLE_COUNT individual INSERTs under the given
    synchronous_commit setting, measuring pure client-perceived
    commit latency per statement -- the time between issuing the
    INSERT and it returning as committed. This is NOT replication
    lag (that's experiment_a_lag.py); this is the direct cost paid
    by the client on the write itself.
    """
    await conn.execute(f"SET synchronous_commit = '{mode}'")

    print(f"\n{'='*60}")
    print(f"MODE: synchronous_commit = {mode}")
    print(f"{'='*60}")

    latencies_ms = []

    for i in range(SAMPLE_COUNT):
        t0 = time.perf_counter()
        await conn.execute(
            "INSERT INTO lag_test (payload) VALUES ($1)",
            f"sync_test_{mode}_{i}_{uuid.uuid4().hex[:8]}"
        )
        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000)

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{SAMPLE_COUNT} commits measured")

    result = {
        "mode": mode,
        "p50_ms": round(statistics.median(latencies_ms), 3),
        "p95_ms": round(statistics.quantiles(latencies_ms, n=20)[18], 3),
        "p99_ms": round(statistics.quantiles(latencies_ms, n=100)[98], 3),
        "min_ms": round(min(latencies_ms), 3),
        "max_ms": round(max(latencies_ms), 3),
        "mean_ms": round(statistics.mean(latencies_ms), 3),
    }

    print(f"\n  Commit latency (ms) under synchronous_commit={mode}:")
    print(f"    min={result['min_ms']}  p50={result['p50_ms']}  "
          f"p95={result['p95_ms']}  p99={result['p99_ms']}  "
          f"max={result['max_ms']}  mean={result['mean_ms']}")

    return result


async def main():
    print("Experiment B: Synchronous vs Asynchronous Commit Latency")
    print(f"Primary: {PRIMARY_URL}")
    print(f"Samples per mode: {SAMPLE_COUNT}")

    conn = await asyncpg.connect(PRIMARY_URL)

    try:
        await conn.execute("TRUNCATE TABLE lag_test")

        # Confirm the replica is actually registered as the
        # synchronous standby before measuring anything. If this
        # doesn't show sync_state='sync', the "on" measurement below
        # would be silently meaningless -- see module docstring.
        standby_check = await conn.fetchrow(
            "SELECT application_name, sync_state FROM pg_stat_replication "
            "WHERE application_name = 'replica1'"
        )
        if standby_check is None or standby_check["sync_state"] != "sync":
            print("\nWARNING: replica1 is not registered as a synchronous "
                  "standby (sync_state != 'sync'). The 'on' measurement "
                  "below will not reflect a real synchronous wait. "
                  "Run: ALTER SYSTEM SET synchronous_standby_names = "
                  "'replica1'; SELECT pg_reload_conf(); before proceeding.")
        else:
            print(f"\nConfirmed: replica1 sync_state = "
                  f"{standby_check['sync_state']} -- proceeding.")

        async_result = await measure_commit_latency(conn, "off")
        sync_result = await measure_commit_latency(conn, "on")

        print(f"\n\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"  {'Metric':<10} {'async (off)':>15} {'sync (on)':>15} {'overhead':>12}")
        print(f"  {'-'*10} {'-'*15} {'-'*15} {'-'*12}")
        for metric in ["p50_ms", "p95_ms", "p99_ms", "mean_ms"]:
            a = async_result[metric]
            s = sync_result[metric]
            overhead = s - a
            print(f"  {metric:<10} {a:>15.3f} {s:>15.3f} {overhead:>+11.3f}ms")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())