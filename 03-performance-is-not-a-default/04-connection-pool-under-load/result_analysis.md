# Result Analysis: Connection Pooling Under Load

**Endpoints:** `/raw/{id}` vs `/pooled/{id}` — identical query, identical
200-VU load profile, only the connection strategy differs.

---

## The core finding, stated precisely

At 200 concurrent requests against a database limited to 20 connections:

- **Raw per-request connections: 80.73% failure rate**, 219 req/s effective
  throughput, p95 latency 2.36s.
- **Pooled connections: 0% failure rate**, 1,507 req/s effective throughput
  (6.9x higher), p95 latency 132ms (18x lower).

This is not a marginal optimization. It is the difference between a
system that functions under load and one that does not.

---

## Why the failure modes are so different in kind, not just degree

The raw endpoint's failure is **structural and immediate**: every
request beyond `max_connections=20` attempts a full TCP handshake, TLS
negotiation, authentication, and PostgreSQL backend process fork
(the connection cost mechanism covered earlier in this lab series)
— and then PostgreSQL rejects it outright with
`FATAL: sorry, too many clients already`. The client receives an error
in well under a second because the rejection is fast; the failure is
not slow, it is just total for 4 out of every 5 requests under this load.

The pooled endpoint's behavior is **structural and graceful**: only 10
real database connections exist, already established and reused. When
all 10 are busy, the 11th request does not attempt a new connection at
all — it waits in `asyncpg.Pool`'s internal queue. The cost of excess
concurrency is paid entirely in latency, visible and monitorable, not
in hard failures invisible until a client sees a 5xx.

**The production implication:** a raw-connection API under sudden
traffic spikes (a marketing push, a viral post, a retry storm from an
upstream service) doesn't degrade — it cliffs. A pooled API under the
same spike slows down, which is recoverable, alertable, and far less
likely to cascade into other systems calling it.

---

## The pool's own failure mode is real, not hypothetical

The pooled run's max latency (2.84s) against its own p95 (132ms) is
the most important secondary finding in this experiment. It proves
the pool was genuinely under load-induced queueing pressure at its
peak — not coasting effortlessly. With an `acquire timeout` of 5
seconds explicitly configured, the system came within roughly 57% of
that ceiling during the 200-VU hold.

This means: at a somewhat higher concurrency level, or against a
slightly slower query, the pooled endpoint would begin producing its
own `pool_acquire_timeout` errors — a different failure mode from the
raw endpoint's immediate rejection, but a real failure mode
nonetheless. A connection pool is not a guarantee against failure
under arbitrary load; it is a much higher, much more gracefully
approached ceiling.

**Production rule:** always set the pool's acquire timeout explicitly.
asyncpg's default is `None` — an unbounded wait. An unbounded acquire
timeout means a single slow query holding a connection can cause
every other request in the system to queue indefinitely behind it,
turning one slow query into a total outage instead of a contained,
visible latency spike. An explicit timeout converts "the whole system
hangs forever" into "some requests fail fast with a clear, attributable
error" — a strictly better failure mode to design for deliberately.

---

## Sizing the pool: why max_size=10 against max_connections=20

The pool was deliberately sized to use only half the container's
connection ceiling (10 of 20). This headroom is not arbitrary — it is
the same principle as leaving capacity for `psql` sessions, monitoring
tools, and other application instances that might connect to the same
database simultaneously. A pool sized to consume 100% of
`max_connections` leaves zero room for anything else, including the
database's own maintenance connections (autovacuum workers, replication,
administrative access) — a common production misconfiguration that
causes "database unreachable" incidents that have nothing to do with
application traffic at all.

**Production rule:** size connection pools to a fraction (commonly
50-70%) of the database's `max_connections`, accounting for every
other service, replica, and administrative tool that also needs to
connect — not just the one application you're currently tuning.

---

## Why this connects to everything else in this lab

This experiment is the application-layer consequence of the
connection-cost mechanism covered earlier in this curriculum: every
new PostgreSQL connection forks a real OS process, with real memory
and real setup latency. The raw endpoint's catastrophic failure rate
is not a coincidence of bad luck under load — it is the direct,
measurable cost of paying that fork-and-handshake price on every
single request instead of once per pooled connection.

It also connects to Experiment 02's HOT update finding in spirit, if
not mechanism: in both cases, the metric a team would naturally watch
first (throughput, in Experiment 02; "is the server up," in this
experiment) tells an incomplete story. Here, a raw-connection endpoint
might look completely fine in a low-traffic staging environment —
plenty of headroom under 20 connections — and then fail catastrophically
the moment real production traffic exceeds that ceiling, with no
warning in between. The failure is binary and sudden, not a gradual
degradation a team would catch in time.

---

## Core Thesis, Extended Again

> Performance is not a default state but a measurable compromise; every
> database decision — whether adding an index, pooling connections, or
> structuring a query — extracts a specific architectural cost that
> must be explicitly benchmarked and proven under load, rather than
> blindly assumed.

This experiment proves the thesis at the application-architecture
layer, not just the schema layer: choosing not to pool connections is
not a neutral default. It is an active decision with an 80.73% failure
rate under exactly the kind of concurrent load a production API will
eventually see. Nothing about that number was visible from reading the
code — `asyncpg.connect()` per request looks identical to a working
pattern right up until real concurrent traffic arrives. The number was
required.