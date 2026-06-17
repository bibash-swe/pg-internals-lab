# Experiment: Idempotency Under Concurrency

## Setup
Two concurrent requests with identical `idempotency_key`, fired via
`asyncio.gather` against two separate connections to a real PostgreSQL
instance — simulating two application server instances handling the
same retried request at the same moment.

Each state below changes exactly one variable from the previous state,
so the effect of that single change can be isolated.

---

## State 1 — No UNIQUE constraint (true race condition)

**Schema:** `payments` table created WITHOUT the UNIQUE constraint
(only `id`, `idempotency_key`, `amount` — no constraint applied).

**Code under test:** `broken_naive.py` (check-then-act, no constraint
to fall back on).

```
BROKEN naive version:
Request 1 result: <Record id=2 idempotency_key='pay_race_test' amount=Decimal('100.00')>
Request 2 result: <Record id=1 idempotency_key='pay_race_test' amount=Decimal('100.00')>
Rows in DB for key 'pay_race_test': 2
 -> {'id': 2, 'idempotency_key': 'pay_race_test', 'amount': Decimal('100.00')}
 -> {'id': 1, 'idempotency_key': 'pay_race_test', 'amount': Decimal('100.00')}
```

**Result:** 2 rows for the same idempotency key. Both requests read
`existing = None` before either committed an insert, so both proceeded
to insert. In a real payment system, the customer is charged twice.
No protection exists at any layer.

---

## State 2 — UNIQUE constraint added, naive exception handling

**Schema:** `migration.sql` fully applied, including
`ALTER TABLE payments ADD CONSTRAINT payments_key_unique UNIQUE (idempotency_key);`

**Code under test:** `broken_naive.py` (unchanged — still has no
exception handling for a constraint violation).

```
--BROKEN naive version--
Request 1 result: duplicate key value violates unique constraint "payments_key_unique"
DETAIL:  Key (idempotency_key)=(pay_race_test) already exists.
Request 2 result: <Record id=1 idempotency_key='pay_race_test' amount=Decimal('100.00')>
Rows in DB for key 'pay_race_test': 1
 -> {'id': 1, 'idempotency_key': 'pay_race_test', 'amount': Decimal('100.00')}
```

**Result:** 1 row — data integrity is now correct, because the
B-tree index backing the UNIQUE constraint serialized the two inserts
and rejected the second. But Request 2 raised an unhandled
`UniqueViolationError`. In a real FastAPI endpoint this becomes an
unhandled `500` returned to a client whose payment actually succeeded
under the other concurrent request. The database is consistent; the
API is not.

---

## State 3 — UNIQUE constraint with retry-and-return logic

**Schema:** Same as State 2 — constraint already applied, unchanged.

**Code under test:** `fixed_idempotent.py` — catches
`UniqueViolationError`, checks for the existing committed row, and
returns it; falls back to retrying the insert if the conflicting
transaction had rolled back (the MVCC edge case).

```
--FIXED idempotent version--
Request 1 result: <Record id=3 idempotency_key='pay_race_test' amount=Decimal('100.00')>
Request 2 result: <Record id=3 idempotency_key='pay_race_test' amount=Decimal('100.00')>
Rows in DB for key 'pay_race_test': 1
 -> {'id': 3, 'idempotency_key': 'pay_race_test', 'amount': Decimal('100.00')}
```

**Result:** 1 row, and both concurrent requests received the *same*
successful record back — no exception surfaced to either caller. This
is the only state correct at both the database layer and the
application layer.

---

## Key insight

A UNIQUE constraint guarantees data integrity but does not, by itself,
guarantee a correct user-facing response. State 2 proves this directly:
the data was correct (1 row) but the API behavior was broken (an
unhandled exception on one of two functionally identical requests).
The application layer must explicitly catch the constraint violation
and decide what the client should see — data correctness and API
correctness are two separate problems that require two separate
pieces of code to solve.

## Why this matters in production

Stripe and most payment processors retry webhooks and requests
aggressively on timeout. Every retried request is functionally
identical to the original — same idempotency key, same amount. A
backend that only solves State 2 will intermittently return 500s to
legitimate retries, even though no money was lost. Customers see
"payment failed," may attempt to pay again through a different flow,
and support tickets get filed for a bug that the database itself
already prevented. State 3's pattern is what closes that gap.