"""
Replication Lag Under Write Load

Two distinct measurements, because "replication lag" behaves
differently depending on write pattern:

  TRICKLE TEST:
      200 individual INSERTs, one at a time. After each INSERT
      commits on the primary, we poll the REPLICA in a tight loop
      until that exact row becomes visible there, and record the
      elapsed time. This directly answers the "read your own write"
      question: if a client writes to the primary and then reads
      from the replica, how long before that read reflects the
      write? Reported as p50/p95/p99/max, not an average -- a single
      average would hide exactly the kind of latency spike that
      matters most to a real user experiencing it.

  BURST TEST:
      One large batch of rows (BURST_SIZE) inserted via COPY, all at
      once. We measure two separate durations: how long the primary
      took to accept the whole burst, and how much ADDITIONAL time
      the replica needed after that to fully catch up (i.e. until
      every row in the burst is visible there). This reveals whether
      replication lag grows disproportionately under sustained write
      pressure, or whether the replica keeps pace by simply replaying
      WAL as fast as it streams in.

Run:
    python experiment_a_lag.py

Requires the primary (port 5434) and replica (port 5435) both
running and streaming, with migration.sql already applied to the
primary.
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
REPLICA_URL = os.getenv(
    "REPLICA_URL", "postgresql://postgres:postgres@localhost:5435/wal_lab"
)

TRICKLE_COUNT = 200
BURST_SIZE = 5_000
POLL_INTERVAL_SEC = 0.0005  # 0.5ms between visibility checks


async def trickle_test(primary: asyncpg.Connection, replica: asyncpg.Connection) -> dict:
    print(f"\n{'='*60}")
    print(f"TRICKLE TEST: {TRICKLE_COUNT} individual writes, "
          f"lag measured per write")
    print(f"{'='*60}")

    lags_ms = []

    for i in range(TRICKLE_COUNT):
        t0 = time.perf_counter()

        row = await primary.fetchrow(
            "INSERT INTO lag_test (payload) VALUES ($1) RETURNING id",
            f"trickle_{i}_{uuid.uuid4().hex[:8]}"
        )
        row_id = row["id"]

        # Poll the REPLICA until this exact row becomes visible there.
        # This is the literal "read your own write" scenario from the
        # WAL/replication explainer: write to primary, immediately
        # check if a read against the replica would see it.
        while True:
            exists = await replica.fetchval(
                "SELECT EXISTS(SELECT 1 FROM lag_test WHERE id = $1)",
                row_id
            )
            if exists:
                break
            await asyncio.sleep(POLL_INTERVAL_SEC)

        t1 = time.perf_counter()
        lag_ms = (t1 - t0) * 1000
        lags_ms.append(lag_ms)

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{TRICKLE_COUNT} writes measured "
                  f"(latest lag: {lag_ms:.2f}ms)")

    result = {
        "p50_ms": round(statistics.median(lags_ms), 3),
        "p95_ms": round(statistics.quantiles(lags_ms, n=20)[18], 3),
        "p99_ms": round(statistics.quantiles(lags_ms, n=100)[98], 3),
        "min_ms": round(min(lags_ms), 3),
        "max_ms": round(max(lags_ms), 3),
        "mean_ms": round(statistics.mean(lags_ms), 3),
    }

    print(f"\n  Trickle lag distribution (ms):")
    print(f"    min={result['min_ms']}  p50={result['p50_ms']}  "
          f"p95={result['p95_ms']}  p99={result['p99_ms']}  "
          f"max={result['max_ms']}  mean={result['mean_ms']}")

    return result


async def burst_test(primary: asyncpg.Connection, replica: asyncpg.Connection) -> dict:
    print(f"\n{'='*60}")
    print(f"BURST TEST: {BURST_SIZE:,} rows in one batch, "
          f"measuring replica catch-up time")
    print(f"{'='*60}")

    batch_marker = f"burst_{uuid.uuid4().hex[:8]}"

    async def generate_burst_rows():
        for i in range(BURST_SIZE):
            yield (batch_marker,)

    t0 = time.perf_counter()

    await primary.copy_records_to_table(
        "lag_test", records=generate_burst_rows(), columns=["payload"]
    )

    t_primary_done = time.perf_counter()
    primary_insert_ms = (t_primary_done - t0) * 1000
    print(f"  Primary finished accepting burst in {primary_insert_ms:.1f}ms")

    # Poll the replica until it has replayed every row in this burst.
    while True:
        count = await replica.fetchval(
            "SELECT COUNT(*) FROM lag_test WHERE payload = $1", batch_marker
        )
        if count >= BURST_SIZE:
            break
        await asyncio.sleep(POLL_INTERVAL_SEC)

    t_replica_caught_up = time.perf_counter()
    total_ms = (t_replica_caught_up - t0) * 1000
    catchup_after_insert_ms = (t_replica_caught_up - t_primary_done) * 1000

    print(f"  Replica fully caught up {catchup_after_insert_ms:.1f}ms "
          f"AFTER primary finished writing")
    print(f"  Total time (write + full replication): {total_ms:.1f}ms")

    return {
        "burst_size": BURST_SIZE,
        "primary_insert_ms": round(primary_insert_ms, 1),
        "catchup_after_insert_ms": round(catchup_after_insert_ms, 1),
        "total_ms": round(total_ms, 1),
    }


async def main():
    print("Experiment A: Replication Lag Under Write Load")
    print(f"Primary: {PRIMARY_URL}")
    print(f"Replica: {REPLICA_URL}")

    primary = await asyncpg.connect(PRIMARY_URL)
    replica = await asyncpg.connect(REPLICA_URL)

    try:
        await primary.execute("TRUNCATE TABLE lag_test")

        trickle_result = await trickle_test(primary, replica)
        burst_result = await burst_test(primary, replica)

        print(f"\n\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"  Trickle (per-write lag, ms):")
        print(f"    p50={trickle_result['p50_ms']}  "
              f"p95={trickle_result['p95_ms']}  "
              f"p99={trickle_result['p99_ms']}  "
              f"max={trickle_result['max_ms']}")
        print(f"  Burst ({BURST_SIZE:,} rows):")
        print(f"    primary write time: {burst_result['primary_insert_ms']}ms")
        print(f"    replica catch-up after: {burst_result['catchup_after_insert_ms']}ms")
        print(f"    total: {burst_result['total_ms']}ms")

    finally:
        await primary.close()
        await replica.close()


if __name__ == "__main__":
    asyncio.run(main())