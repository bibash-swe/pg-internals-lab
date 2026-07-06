# Result Analysis: WAL and Replication

---

## Finding 1: batching cuts per-row replication cost by roughly 609x —
## the same lesson as N+1, in a different subsystem

```
Trickle: 2.168ms lag per individual row (200 separate INSERTs)
Burst:   17.8ms total for 5,000 rows in one batch
         = 0.00356ms per-row equivalent
```

This is the N+1 finding from an earlier lab, reappearing in a
completely different part of the system. There, firing 100 separate
queries instead of one JOIN cost roughly 100x the database
round-trips. Here, firing 200 separate single-row INSERTs instead of
one batched insert of 5,000 rows costs roughly 609x the per-row
replication overhead. The mechanism is structurally identical: **each
individual round-trip — a network hop, a WAL flush, a client-side
poll cycle — pays a fixed cost regardless of how much data it
carries.** Batching doesn't just do the same work more efficiently; it
does the same work while paying that fixed cost once instead of once
per unit of work.

**Production rule:** if an application is bulk-loading or generating
many related writes in a short window, batch them into as few
statements as possible before they hit the primary — not just for the
INSERT throughput reasons proven in earlier labs, but because
replication lag itself scales with the *number of round-trips*, not
primarily with the *volume of data*. A slow trickle of individually
committed rows keeps replicas measurably further behind than the same
total data delivered as fewer, larger transactions.

---

## Finding 2: synchronous replication has a real, measurable cost —
## and this benchmark shows its floor, not its ceiling

```
async (off):  p50 = 0.395ms
sync (on):    p50 = 1.774ms   (+1.379ms, ~4.5x)
sync (on):    p99 = 3.598ms   (+2.351ms vs async p99, ~2.9x)
```

This is a clean, direct proof of the tradeoff explained conceptually
earlier in this lab: `synchronous_commit=on` makes the primary wait
for `replica1`'s confirmation before telling the client the
transaction succeeded, and that wait is not free. Every commit under
synchronous mode paid roughly 1.4ms more at the median, and over 2ms
more at p99, purely for the extra round-trip confirmation.

**The number that matters most here is what this benchmark cannot
show:** the primary and replica in this lab share the same Docker
bridge network on the same physical machine — about as close to
zero physical network latency as two separate PostgreSQL processes
can be. The ~1.4-2.4ms overhead measured here is close to the
theoretical floor for synchronous replication's cost. In a real
production deployment with a replica in a different availability zone
(typically 1-3ms one-way) or a different region entirely (commonly
20-80ms one-way), that same `synchronous_commit=on` decision would add
tens of milliseconds to every single commit, not low single digits.
This benchmark proves the mechanism correctly; anyone applying this
number to a geographically distributed production system would be
making a serious sizing error.

**Production rule:** never assume a local or same-datacenter
replication benchmark represents real-world synchronous replication
cost if the production replica will be geographically distant. Measure
the actual network path being deployed, or at minimum apply a
conservative multiplier based on known inter-region latency figures,
before committing to synchronous replication for latency-sensitive
write paths.

---

## Finding 3: synchronous_commit=on silently does nothing without
## synchronous_standby_names — a trap this lab hit before writing a
## single line of benchmark code

Before either experiment could produce a meaningful result, this lab's
own build process surfaced a real correctness trap: setting
`synchronous_commit=on` alone has zero effect unless
`synchronous_standby_names` names a specific replica by
`application_name`. Without that name, there is no eligible standby to
wait for, and synchronous mode silently degrades to identical async
behavior — no error, no warning, just a wrong result that looks
correct.

This was caught only because the replica's `sync_state` was checked
explicitly via `pg_stat_replication` before trusting any measurement
— exactly the same discipline as Experiment 05's observer-effect
discovery (verify the instrument before trusting what it reports) and
Experiment 06's fairness finding (check what the aggregate metric is
hiding, not just whether it looks correct).

**Production rule:** whenever configuring synchronous replication,
always verify `sync_state = 'sync'` in `pg_stat_replication` on the
primary before assuming any durability guarantee is actually active.
A misconfigured `synchronous_standby_names` list is invisible until
the moment of an actual primary failure, at which point it is too late
to discover that "durable" writes were never durable at all.

---

## A methodological note: the trickle measurement includes real
## polling granularity noise

`experiment_a_lag.py` polls the replica every 0.5ms
(`POLL_INTERVAL_SEC`) to detect when a row becomes visible. This means
every trickle measurement includes up to ~0.5ms of granularity noise
on top of the true underlying replication lag — the actual lag could
be anywhere between the previous poll and the poll that detected the
row. The reported p50 of 2.168ms is therefore a reasonable but not
perfectly precise upper-bound estimate of true lag; a tighter poll
interval would narrow this margin at the cost of more query overhead
on both connections during the benchmark itself.

---

## Core Thesis, Extended a Fifth Time

> Performance is not a default state but a measurable compromise; every
> database decision — whether adding an index, pooling connections, or
> structuring a query — extracts a specific architectural cost that
> must be explicitly benchmarked and proven under load, rather than
> blindly assumed.

This lab proves the thesis at the replication layer, and closes the
loop on a pattern that has now appeared in three separate subsystems
across this repository: individual round-trips cost disproportionately
more than batched ones, whether the round-trip is a database query
(Experiment 05's N+1 finding), a raw connection setup (Experiment 04's
pooling finding), or — as proven here — a single row's replication
across the network to a standby server. The specific mechanism differs
each time; the underlying arithmetic of fixed-cost-per-round-trip does
not.