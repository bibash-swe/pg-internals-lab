# Results: Connection Pooling Under Load

**Endpoints:** `/raw/{id}` (fresh `asyncpg.connect()` per request) vs
`/pooled/{id}` (shared `asyncpg.Pool`, max_size=10, acquire timeout=5s)
**Target table:** `transactions_five_indexes` (500,000 rows, Experiment 02)
**Database:** PostgreSQL 16 in Docker (256MB shared_buffers, max_connections=20)
**Load profile:** k6, ramp 0→200 VUs over 10s, hold 200 VUs for 30s, ramp down 5s
**Both runs use the identical load profile — connection strategy is the only variable.**

---

## Run 1 — `/raw/{id}` (no pooling)

```
THRESHOLDS
  errors
  ✓ 'rate<1.0' rate=80.73%
  http_req_duration
  ✓ 'p(95)<5000' p(95)=2.36s

TOTAL RESULTS
  checks_total.......: 10468  219.24177/s
  checks_succeeded...: 19.26% 2017 out of 10468
  checks_failed......: 80.73% 8451 out of 10468
  ✗ status is 200
    ↳  19% — ✓ 2017 / ✗ 8451

  errors.........................: 80.73% 8451 out of 10468
  request_duration_ms............: avg=721.57ms  med=314.36ms  p(90)=1.61s  p(95)=2.36s  max=6.38s

  http_req_duration..............: avg=721.56ms  med=314.36ms  p(90)=1.6s   p(95)=2.36s
  http_req_failed................: 80.73% 8451 out of 10468
  http_reqs......................: 10468  219.24177/s

  iterations.....................: 10468  219.24177/s
  vus_max........................: 200
running (0m47.7s), 10468 complete iterations
```

**Result: 80.73% of all requests failed.** Once concurrent raw connections
exceeded the container's `max_connections=20`, PostgreSQL began rejecting
new connection attempts outright. Only 19.26% of requests — those lucky
enough to acquire one of the 20 available slots — succeeded.

---

## Run 2 — `/pooled/{id}` (asyncpg.Pool, max_size=10)

```
THRESHOLDS
  errors
  ✓ 'rate<1.0' rate=0.00%
  http_req_duration
  ✓ 'p(95)<5000' p(95)=132.27ms

TOTAL RESULTS
  checks_total.......: 75864   1507.065197/s
  checks_succeeded...: 100.00% 75864 out of 75864
  checks_failed......: 0.00%   0 out of 75864
  ✓ status is 200

  errors.........................: 0.00%  0 out of 75864
  request_duration_ms............: avg=52.17ms  med=45.24ms  p(90)=100.73ms  p(95)=132.27ms  max=2.84s

  http_req_duration..............: avg=52.17ms  med=45.23ms  p(90)=100.72ms  p(95)=132.27ms
  http_req_failed................: 0.00%  0 out of 75864
  http_reqs......................: 75864  1507.065197/s

  iterations.....................: 75864  1507.065197/s
  vus_max........................: 200
running (0m50.3s), 75864 complete iterations
```

**Result: 0% failure rate.** Despite the identical 200-VU load, every
single request succeeded. Excess concurrency was absorbed as queueing
latency inside the pool, not surfaced as errors to the client.

---

## Summary Table

| Metric | Raw connections | Pooled connections | Difference |
|--------|------------------|----------------------|------------|
| Total requests completed | 10,468 | 75,864 | **7.25x more** |
| Error rate | 80.73% | 0.00% | — |
| Effective throughput | 219 req/s | 1,507 req/s | **6.9x higher** |
| p50 latency | 314ms | 45ms | **7x lower** |
| p90 latency | 1.6s | 101ms | **16x lower** |
| p95 latency | 2.36s | 132ms | **18x lower** |
| Max latency | 6.38s | 2.84s | **2.2x lower** |

---

## The number worth a second look: pooled max latency = 2.84s

Even the pooled endpoint, with zero errors across 75,864 requests, had a
maximum single-request latency of 2.84 seconds — far above its own p95
of 132ms. This is not noise. It is the pool's internal queue under
genuine strain at the peak of the 200-VU hold period: with only 10
real database connections serving 200 concurrent virtual users, some
requests had to wait behind 9 others ahead of them in the queue before
a connection became available.

The pool's `acquire timeout` was explicitly set to 5 seconds in
`app.py`. The observed 2.84s max means the system came within ~57%
of that ceiling under this load level, but never crossed it. At a
higher VU count, or with a slower query, this same mechanism would
begin producing real `pool_acquire_timeout` errors — the pooled
endpoint's own designed failure mode, distinct from the raw endpoint's
immediate connection rejection.

A connection pool does not eliminate failure under extreme load. It
changes failure from "every excess request crashes immediately" to
"requests queue gracefully until a defined timeout is exceeded." Both
are real failure modes; the pooled one is simply far more forgiving,
far more observable (latency climbing is a visible warning sign;
connection rejection is not), and triggers at a much higher load
threshold.