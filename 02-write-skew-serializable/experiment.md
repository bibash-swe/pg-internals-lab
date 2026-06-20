# Experiment: Write Skew Across Isolation Levels

## Setup

Two simulated doctors, Alice and Bob, both initially `is_on_call = true`.
Business rule: at least one doctor must remain on call at all times.
Each doctor's transaction independently checks "is someone else
covering?" before deciding to go off-call.

Two connections (`conn_alice`, `conn_bob`) run concurrently via asyncio.
Both transactions are forced to complete their `SELECT` before either
is allowed to proceed to `UPDATE` + `COMMIT` — using `asyncio.Event`
to control the interleaving deliberately, rather than relying on
scheduler timing. This guarantees the anomaly is reproduced on every
run, not just sometimes.

The same scenario is run three times, once per isolation level, with
the table reset to its initial state between runs. Only the isolation
level changes between runs — every other variable is held constant.

---

## Run 1 — READ COMMITTED

```
[Bob] sees 2 doctor(s) on call
[Alice] sees 2 doctor(s) on call
[Alice] COMMIT succeeded — went off-call
[Bob] COMMIT succeeded — went off-call
Final state: [{'doctor': 'Alice', 'is_on_call': False}, {'doctor': 'Bob', 'is_on_call': False}]
Doctors still on call: 0
```

**Result: BUG.** Both Alice and Bob read "2 on call" before either
committed. Both concluded it was safe to leave. Both committed
successfully. Final state: zero doctors on call, violating the
business rule, even though neither individual transaction did
anything incorrect by its own logic.

---

## Run 2 — REPEATABLE READ

```
[Alice] sees 2 doctor(s) on call
[Bob] sees 2 doctor(s) on call
[Alice] COMMIT succeeded — went off-call
[Bob] COMMIT succeeded — went off-call
Final state: [{'doctor': 'Alice', 'is_on_call': False}, {'doctor': 'Bob', 'is_on_call': False}]
Doctors still on call: 0
```

**Result: STILL A BUG.** Repeatable Read guarantees each transaction's
own snapshot stays internally consistent for its full duration — but
that is not the same guarantee as detecting that another concurrent
transaction is making a decision based on the same soon-to-be-stale
data. Both transactions are individually consistent. The violation
only exists when you look at their combined effect, which Repeatable
Read has no mechanism to check.

---

## Run 3 — SERIALIZABLE

```
[Alice] sees 2 doctor(s) on call
[Bob] sees 2 doctor(s) on call
[Alice] COMMIT succeeded — went off-call
[Bob] COMMIT FAILED — serialization error: could not serialize access due to read/write dependencies among transactions
DETAIL:  Reason code: Canceled on identification as a pivot, during write.
HINT:  The transaction might succeed if retried.
Final state: [{'doctor': 'Bob', 'is_on_call': True}, {'doctor': 'Alice', 'is_on_call': False}]
Doctors still on call: 1
```

**Result: CORRECT.** Both transactions still read "2 on call" at the
same moment — Serializable doesn't prevent that read. But at commit
time, PostgreSQL detects that Bob's transaction read a row (Alice's)
that Alice's transaction then wrote to and committed while Bob's was
still in flight. Bob is identified as the "pivot" transaction in that
dependency cycle and is aborted with a serialization failure. The
final state correctly leaves one doctor on call.

---

## Summary table

| Isolation level   | Doctors on call at end | Correct? |
|--------------------|:----------------------:|:--------:|
| Read Committed      | 0                       |    No    |
| Repeatable Read      | 0                       |    No    |
| Serializable         | 1                       |    Yes   |

---

## Key insight

Write skew is not caught by "the transaction sees consistent data" —
both Read Committed and Repeatable Read satisfy that, in their own
way, and both still produce the bug. Write skew is only caught by a
level that tracks **dependencies between concurrent transactions**,
not just consistency within a single one. Serializable does this by
tracking which rows each transaction read and cross-referencing that
against what concurrent transactions wrote, aborting one side of any
cycle that couldn't have occurred under a real one-at-a-time ordering.

## Operational note: Serializable is not "fire and forget"

The `HINT: The transaction might succeed if retried.` line in the
error is not optional advice — it's a requirement. Application code
using Serializable isolation must catch the specific serialization
failure (SQLSTATE `40001`) and retry the whole transaction. Without
that retry logic, Serializable will surface real, user-facing errors
under normal concurrent load. This script does not implement the
retry — Bob's failure is reported, not retried — which is a
deliberate scope limit for this lab, not a production-ready pattern.

## Why this matters in production

Idempotency keys and UNIQUE constraints (see [lab 01](../01-idempotency-unique-constraint))
solve a narrower problem: preventing duplicate writes for a single
identifiable entity. Write skew is a different shape of bug entirely
— it shows up whenever a business invariant spans *multiple rows* and
each transaction's decision depends on the current state of rows it
doesn't itself own. On-call scheduling, account balance limits across
multiple withdrawals, and seat/inventory reservation systems are all
real-world instances of this same pattern. A UNIQUE constraint cannot
express this kind of multi-row invariant — only Serializable isolation
(or explicit application-level locking) can.