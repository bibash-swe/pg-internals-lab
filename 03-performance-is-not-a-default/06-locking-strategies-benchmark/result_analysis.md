# Result Analysis: Locking Strategies Benchmark

**Problem:** prevent duplicate payment processing under concurrency,
benchmarked across five distinct concurrency-control strategies, all
solving the identical correctness requirement proven in Lab 01.

---

## Finding 1: FOR UPDATE has no fairness guarantee — and the aggregate
## metric hides it completely

```
Per-worker claim distribution: [0, 0, 0, 0, 13, 21, 26, 26, 44, 51, 52, 55, 56, 74, 82]
```

The benchmark's top-level result was `[PASS]` — all 500 rows were
processed correctly, exactly once each. But 4 of the 15 concurrent
workers claimed **zero rows across the entire run**. This is not a
bug in the benchmark. It is documented PostgreSQL behavior: when
multiple transactions block waiting on the same row lock, PostgreSQL
makes no promise about which waiter gets the lock next once it's
released. There is no FIFO queue for lock waiters.

This is the same category of lesson as Experiment 02's HOT-update
discovery: the metric a team would naturally check first — did the
job finish correctly, is throughput acceptable — says nothing about
whether the work was distributed fairly. If "workers" here represented
15 separate application server processes each serving real user
requests, four of those processes would have sat idle-but-blocked for
the entire benchmark window while the other eleven did all the work —
invisible in an aggregate throughput dashboard, very visible as
inconsistent latency to the unlucky quarter of users routed through
the starved processes.

**Production rule:** never assume `SELECT ... FOR UPDATE` provides
fair, ordered access to contended rows. If ordering fairness is an
actual business requirement, it must be enforced explicitly at the
application layer (e.g., an application-level queue with its own
ordering guarantee) — PostgreSQL's row lock alone will not provide it.

---

## Finding 2: SKIP LOCKED wins on throughput AND fairness simultaneously

```
Per-worker claim distribution: [31, 32, 32, 33, 33, 33, 33, 33, 33, 34, 34, 34, 35, 35, 35]
```

3.7x faster than plain FOR UPDATE (2,350 vs 635 rows/sec), and the
claim distribution is nearly perfectly even — every worker did close
to exactly its fair share (500 / 15 ≈ 33.3).

This is the mechanism worth understanding precisely: `SKIP LOCKED`
doesn't resolve contention after the fact the way FOR UPDATE does
(block, then race unfairly for the freed lock). It avoids contention
structurally, by having every blocked worker immediately move to a
*different* unlocked row instead of ever queueing for the same one.
No worker waits on another worker at all — each one independently
finds whatever work is currently available. This is precisely why it
wins on both axes at once: fairness emerges naturally from a design
that never creates a queue to be unfair within.

**Production rule:** for any job-queue or worker-pool pattern where
processing order doesn't matter — background job workers, most
payment-retry processors, task queues — `SKIP LOCKED` should be the
default, not plain `FOR UPDATE`. Reach for plain `FOR UPDATE` only
when strict ordering is a genuine requirement, and even then, be aware
it does not guarantee fairness, only mutual exclusion.

---

## Finding 3: Optimistic locking is the slowest strategy of all five —
## because this benchmark is close to its worst case

```
Optimistic:   382.8 rows/sec  (slowest of all 5 strategies)
FOR UPDATE:   635.0 rows/sec  (a BLOCKING strategy — beats optimistic)
```

This is the most theoretically important result in the experiment.
Optimistic locking's entire value proposition is "never block, retry
on conflict instead" — and here, it lost to a strategy that blocks
constantly.

The reason is the benchmark's own design: `ORDER BY id LIMIT 1` means
every worker, every single time, targets the exact same lowest-id
pending row. Under optimistic locking, there is no lock preventing
multiple workers from reading that same row's version simultaneously
— so on nearly every row transition, several workers read the same
version, compute their update, and all but one lose the subsequent
conditional `UPDATE ... WHERE version = $captured`. Every loss is a
fully wasted SELECT + UPDATE round-trip, and then the losing worker
loops back to read again — usually landing on the same contested row
once more.

Optimistic locking wastes time **retrying**. Pessimistic locking
wastes time **waiting**. This benchmark deliberately created close to
worst-case contention for optimistic locking — many workers, one
narrow contended resource — and the retry cost compounded far faster
than a blocking wait would have.

**This is not evidence that optimistic locking is a bad strategy in
general.** It is evidence that optimistic locking is the *wrong*
strategy specifically under high contention on a narrow set of
resources. Its actual design assumption is the opposite scenario: many
independent rows, contention on any single one is *rare*, so the
occasional wasted retry is cheap. A production system with genuinely
low per-row contention — say, 10,000 independent rows and 15 workers,
rather than 15 workers all targeting the identical single row — would
very likely show optimistic locking winning, not losing. The lesson is
to measure your actual contention pattern before choosing, not to
treat any single strategy as universally correct.

---

## Finding 4: UNIQUE constraint beats advisory locks by 1.6x for
## idempotency-key deduplication — and the reason is coordination overhead

```
UNIQUE constraint:  6,395.2 attempts/sec
Advisory lock:      3,988.3 attempts/sec
```

Both strategies achieved perfect correctness (8 successes out of 2,400
attempts, exactly matching the 8 shared keys). The throughput
difference comes down to how many round-trips each attempt costs.

Advisory locking's sequence per attempt: `BEGIN` → acquire
`pg_advisory_xact_lock` (blocks if another worker holds this exact
key's lock) → `SELECT EXISTS(...)` → conditionally `INSERT` →
`COMMIT`. Under the deliberately concentrated contention of 15 workers
repeatedly hitting only 8 keys, most attempts spend real time waiting
for the advisory lock alone, before ever reaching the existence check.

The UNIQUE constraint strategy's sequence per attempt: one `INSERT`,
full stop. No explicit lock acquisition step exists at all — the
B-tree index backing the constraint performs an atomic check-and-insert
internally, at the storage engine level, exactly as proven in Lab 01.
There is nothing to wait on because there is no separate coordination
step to serialize.

**Production rule:** for idempotency-key-style duplicate prevention —
the exact shape of problem this represents — the UNIQUE constraint
pattern from Lab 01 is not just simpler than advisory locking, it is
measurably faster under contention, because it has zero explicit
coordination overhead. Advisory locks remain the correct tool for a
different problem: protecting a *logical* operation that has no
natural row to attach a constraint to at all (for example, "only one
instance of this scheduled job may run at a time across N application
servers").

---

## A methodological note: two throughput numbers, two different meanings

The job-queue strategies (1, 2, 5) report rows of **real, useful work
completed per second** — 500 total rows exist, and the number reflects
how fast they were genuinely processed. These three numbers are
directly comparable to each other.

The insert-race strategies (3, 4) report **attempts absorbed per
second under a workload where 2,392 of every 2,400 attempts are
expected to fail** — this measures resilience under a retry storm, not
useful-work throughput, since only 8 rows are ever meant to exist.
These two numbers are directly comparable to each other, but **not**
to the job-queue numbers above them. Reporting "6,395/sec" next to
"635/sec" without this distinction would imply the UNIQUE constraint
strategy is ten times faster than plain FOR UPDATE at the same task —
which is not a claim this benchmark makes or supports, since the two
pairs of strategies are not solving the same shape of problem.

---

## Production decision guide, derived directly from this data

| Use case | Correct strategy | Why |
|----------|-------------------|-----|
| Worker pool claiming jobs from a queue, order doesn't matter | **SKIP LOCKED** | Fastest and fairest — proven 3.7x over FOR UPDATE with near-perfect worker balance |
| Worker pool claiming jobs, strict processing order required | FOR UPDATE (plain) | Only reach for this when ordering is a real requirement — and still implement fairness at the application layer if needed |
| Idempotency-key duplicate prevention (payments, webhooks) | **UNIQUE constraint** | Proven 1.6x faster than advisory locks, matches Lab 01, zero coordination overhead |
| Protecting a logical resource with no natural row (e.g., single-instance cron) | Advisory lock | The only strategy of the five that doesn't require a row to exist at all |
| Low-contention updates to many independent rows | Optimistic locking | Correct when conflicts are rare — proven to fail badly under this benchmark's deliberately high, narrow contention; would need re-testing under realistic low-contention conditions before trusting it in that regime |

---

## Core Thesis, Extended a Fourth Time

> Performance is not a default state but a measurable compromise; every
> database decision — whether adding an index, pooling connections, or
> structuring a query — extracts a specific architectural cost that
> must be explicitly benchmarked and proven under load, rather than
> blindly assumed.

This experiment proves the thesis at the concurrency-control layer:
there is no universally correct locking strategy. Each of the five
tested here won decisively in exactly one dimension and lost
decisively in at least one other, depending entirely on the shape of
contention it faced. Optimistic locking — often taught as the
"modern," non-blocking default — was the single slowest strategy
under this benchmark's specific contention pattern. Plain FOR UPDATE
— often assumed "safe" because it's correct — silently starved a
quarter of the workers testing it. Neither fact was visible from
reading documentation or reasoning abstractly. Both required a real,
concurrent, measured benchmark to surface.