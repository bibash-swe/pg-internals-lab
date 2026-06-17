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