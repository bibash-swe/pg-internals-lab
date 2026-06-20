# pg-internals-lab

Hands-on experiments dissecting PostgreSQL internals through reproducible
code — not tutorials, not theory. Each lab poses a real production
problem, reproduces the failure with working code, and proves the fix
with real terminal output.

## Why this exists

Most backend engineers use patterns like idempotency keys, row locking,
and UNIQUE constraints without understanding what happens underneath —
or what edge cases their "fix" doesn't actually cover. This repo is me
forcing myself to understand the storage engine layer before trusting
the pattern.

## Labs

### [01 — Idempotency Under Concurrency](./01-idempotency-unique-constraint)

What happens when two identical payment requests (e.g. a retried
Stripe webhook) hit your backend at the same instant? This lab
reproduces the race condition with no protection, shows why a UNIQUE
constraint alone isn't enough to keep your API correct, and proves
the fix with three real, measured states — not assumptions.

Key finding: a UNIQUE constraint guarantees data integrity, but the
application layer still has to handle the exception correctly, or you
turn a successfully-deduplicated payment into a 500 error for the
client.

## How to run any lab

Each lab folder is self-contained. General pattern:

```bash
cd 01-idempotency-unique-constraint
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

psql -d postgres -f migration.sql
python test_race.py
```

See each lab's own README/experiment notes for specifics.

## About me

Backend engineer working primarily in Python/FastAPI on fintech and
SaaS systems. This repo is part of a deliberate effort to go deeper
into the database and systems internals that most application-level
work lets you skip.

# 02 — Write Skew Across Isolation Levels

## The problem

Two doctors, Alice and Bob, are both on call. The business rule: at
least one doctor must remain on call at all times. Each doctor's
logic is: "I can go off-call, as long as someone else is still
covering."

If both check the schedule at the same moment — before either has
committed their decision — both can see "someone else is covering,"
both go off-call, and the rule is violated. Neither transaction did
anything wrong in isolation; the violation only exists when you look
at their combined effect. This is called **write skew**.

## What this lab proves

Write skew survives both Read Committed (Postgres's default) and
Repeatable Read. It is only caught by **Serializable** isolation,
which tracks read/write dependencies *between* concurrent
transactions, not just consistency *within* a single one.

Three runs, one per isolation level, same scenario, same starting
state. Only the isolation level changes between runs.

| Isolation level   | Doctors on call at end | Correct? |
|--------------------|:----------------------:|:--------:|
| Read Committed      | 0                       | No       |
| Repeatable Read      | 0                       | No       |
| Serializable         | 1                       | Yes      |

Full real terminal output and analysis: [experiment.md](./experiment.md)

## Files

- `migration.sql` — creates and seeds the `on_call` table
- `test_write_skew.py` — runs all three isolation levels automatically,
  with controlled interleaving so the anomaly reproduces every run
- `experiment.md` — real output and written analysis

## How to run

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

psql -d postgres -f migration.sql
python test_write_skew.py
```

Override the connection string with `DATABASE_URL` if needed.

## Scope note

This script reports Bob's serialization failure but does not retry
it. A production system using Serializable isolation must catch the
serialization failure (SQLSTATE `40001`) and retry the entire
transaction — see `experiment.md` for details. Implementing that
retry loop is a natural next extension of this lab.