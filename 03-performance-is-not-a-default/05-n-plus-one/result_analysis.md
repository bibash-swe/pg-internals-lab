# Result Analysis: N+1 Query Detection and Elimination

**Endpoints:** `/naive/{limit}` vs `/fixed/{limit}` — identical returned
data, radically different database round-trip counts.

---

## The core finding, stated as a complexity class, not a number

`query_count(n) = n + 1` for the naive endpoint. `query_count(n) = 1`
for the fixed endpoint, confirmed constant across a 1,000x range of n
(100 to 100,000).

This is the same Big-O vocabulary used to grade an algorithm's time
complexity — because it is exactly that. The "operation" being counted
here isn't an array comparison or a hashmap lookup; it's a full
database round-trip, each one carrying real network latency and the
connection-pool contention proven in Experiment 04. **The naive
endpoint has O(n) query complexity. The fixed endpoint has O(1) query
complexity.** An O(n) algorithm that looks harmless at n=20 in a dev
environment becomes measurably expensive at n=2,000 and would be
catastrophic at n=100,000 — extrapolating the proven formula, that's
100,001 sequential round-trips for one API response.

---

## The Rust-relevant framing: this is a `sort()` vs manual bubble sort problem

The JOIN is PostgreSQL doing the equivalent of calling `Vec::sort()`:
you make **one function call**, and the implementation performs
whatever internal work is needed — nested loop join, hash join, or
merge join, using `accounts_pkey`'s B-tree index exactly as Experiment
01 characterized — entirely out of view, in one round-trip. You don't
write a loop manually invoking a comparator for every pair of elements
when you call `.sort()`; you hand the whole batch to one call and let
a well-tested implementation handle the internal complexity
efficiently.

The N+1 pattern is the equivalent of hand-rolling a bubble sort in
application code, one comparison at a time — badly reimplementing work
a well-optimized engine already does internally, and paying a full
network round-trip for every "comparison" instead of an in-memory
operation.

No compiler catches this in either language. `rustc` checks types,
ownership, and lifetimes; it has no concept of "this loop body performs
a network round-trip that should be batched." A `for txn in transactions`
loop that calls `.fetch_one(&pool)` per iteration compiles cleanly in
Rust with `sqlx`, exactly as the equivalent Python loop runs without
warning. This is precisely why `pg_stat_statements`-based query-count
instrumentation matters more than a code review here: a reviewer
skimming source — in either language — can miss this completely,
especially behind an ORM's lazy-loaded relationship accessor, where the
loop firing N queries isn't even visible as a loop in the calling code.
A query-count proof catches it with certainty, every time, independent
of language.

---

## A methodology finding worth keeping: the observer effect

The first version of the query-count instrumentation added a constant
+1 to every measurement, because the "before" snapshot query's own
completed execution got counted by the "after" snapshot — the
instrumentation was measuring itself. See `results.md` for the full
mechanism and fix.

This is worth stating as a general principle, not just a one-off bug:
**any instrumentation that reads from the same substrate it's
measuring must explicitly exclude its own footprint, or it will
silently bias every result by a constant amount.** The same class of
correction is required for a CPU performance counter (excluding the
instruction that reads the counter), a Rust benchmarking harness like
`criterion` (subtracting its own sampling overhead before reporting),
or a profiler instrumenting its own sampling interrupt. A constant
offset in benchmark results is almost never noise — it's a mechanism,
and it's usually the measurement tool measuring itself.

---

## Why this compounds specifically under concurrent load

Experiment 04 proved that raw, unpooled connections collapse under
concurrency because each one pays the full connection-setup cost
(TCP handshake, auth, backend process fork) per request. The naive
N+1 endpoint compounds that exact problem differently: even *with* a
connection pool (which this experiment's endpoints both use), every
one of the `n+1` queries for a single request holds a pooled
connection for its own round-trip duration before releasing it back
for the next query in the same loop. A single `/naive/100` request
occupies a connection **101 times in sequence** — effectively
serializing 101 round-trips onto whatever pool connection it acquired,
for the duration of one HTTP request. Under concurrent traffic, this
means N+1 endpoints starve the connection pool far faster than their
raw request count would suggest, because each request holds up the
queue for `n` sequential round-trips instead of one.

---

## Production rule

Any query pattern where the number of database round-trips scales
with the number of results returned — rather than staying constant —
is an O(n) liability that will not show up in code review, will not
show up in unit tests against small fixtures, and will not show up in
staging environments with sparse test data. It will show up exactly
when a feature succeeds and its underlying dataset grows, and it will
show up as compounding pressure on connection pool exhaustion (see
Experiment 04) before it shows up as visibly high per-request latency.

The fix is not "optimize the loop" — it is recognizing that the loop
should not exist at all. Any time application code fetches a
collection and then fetches related data per item in that collection,
the correct default assumption is that a single JOIN (or, where a JOIN
genuinely isn't expressible, a single batched `WHERE id = ANY($1)`
query) replaces the entire loop, converting O(n) round-trips into O(1).

---

## Core Thesis, Extended a Third Time

> Performance is not a default state but a measurable compromise; every
> database decision — whether adding an index, pooling connections, or
> structuring a query — extracts a specific architectural cost that
> must be explicitly benchmarked and proven under load, rather than
> blindly assumed.

This experiment proves the thesis at the query-design layer: N+1 is
not a slow query. It is the *wrong number* of queries, growing without
bound as the feature that uses it succeeds. Nothing about the naive
endpoint's source code signals this — it reads as ordinary, working
Python, exactly as the equivalent Rust would read as ordinary, working
Rust. The formula `query_count(n) = n + 1`, proven exactly across six
orders of magnitude, was required to make the cost undeniable.