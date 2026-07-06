# 04 — WAL and Replication

## The problem

PostgreSQL's Write-Ahead Log is the mechanism behind both crash
recovery and streaming replication — a replica is, structurally,
just another reader of the same append-only log the primary already
keeps for its own durability. This lab builds a real primary/replica
setup via Docker Compose, verifies it with genuine streaming
replication (not a simulation), and measures two real production
tradeoffs: how replication lag behaves under different write patterns,
and what synchronous replication actually costs.

## What this lab proves

**Batching cuts per-row replication cost by ~609x.** 200 individual
writes cost 2.168ms of lag each (median). 5,000 rows in one batch cost
17.8ms total — 0.00356ms per row. Same lesson as the N+1 query finding
in [Experiment 05](../03-performance-is-not-a-default/05-n-plus-one),
appearing again in a different subsystem: fixed per-round-trip costs
dominate when work isn't batched.

**Synchronous replication has a real, measured cost — proven at its
floor, not its ceiling.** `synchronous_commit=on` added ~1.4ms at the
median and ~2.4ms at p99 versus async, on a same-machine Docker
network. A geographically distant production replica would show this
same mechanism costing tens of milliseconds instead.

**`synchronous_commit=on` silently does nothing without
`synchronous_standby_names` naming a specific replica.** This lab hit
that exact trap while building the infrastructure — a misconfiguration
that produces no error and looks like working synchronous replication
right up until a real failure exposes that it never was.

| Measurement | Value |
|-------------|-------|
| Trickle lag per row (p50) | 2.168ms |
| Burst per-row equivalent lag | 0.00356ms |
| Per-row cost ratio | ~609x |
| Sync commit overhead (p50) | +1.379ms (~4.5x) |
| Sync commit overhead (p99) | +2.351ms (~2.9x) |

Full real terminal output and analysis in
[results.md](./results.md) and [result_analysis.md](./result_analysis.md).

## Files

- `docker-compose.yml` — primary + replica services, streaming replication
- `init-primary.sh` — creates the replication role and permits replica connections
- `replica-entrypoint.sh` — runs `pg_basebackup` then boots as a standby,
  with an explicit `application_name` (required for synchronous replication)
- `migration.sql` — the single table used by both experiments (primary only)
- `experiment_a_lag.py` — trickle vs burst replication lag, real percentiles
- `experiment_b_sync_vs_async.py` — commit latency under sync vs async replication
- `results.md` / `result_analysis.md` — real output and analysis

## How to run

```bash
docker compose up -d
docker compose logs -f replica   # wait for "ready to accept read-only connections"

# One-time: register the replica as the synchronous standby
psql postgresql://postgres:postgres@localhost:5434/wal_lab \
  -c "ALTER SYSTEM SET synchronous_standby_names = 'replica1';"
psql postgresql://postgres:postgres@localhost:5434/wal_lab \
  -c "SELECT pg_reload_conf();"

# Verify before trusting any synchronous measurement
psql postgresql://postgres:postgres@localhost:5434/wal_lab \
  -c "SELECT application_name, sync_state FROM pg_stat_replication;"
# Must show sync_state = sync

psql postgresql://postgres:postgres@localhost:5434/wal_lab -f migration.sql

uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

python experiment_a_lag.py
python experiment_b_sync_vs_async.py
```

**Note:** `synchronous_standby_names` does not survive a full volume
wipe (`docker compose down -v`). Re-run the registration step above
after any full reset, and always verify `sync_state = sync` before
trusting a synchronous-mode measurement.