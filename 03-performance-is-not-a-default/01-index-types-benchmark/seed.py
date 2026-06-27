"""
Experiment 01: Index Types Benchmark

Generates 1,000,000 realistic job listing rows and streams them directly
into the database using an asynchronous generator and the COPY protocol.
This ensures a near-zero memory footprint during bulk load.
"""
import asyncio
import os
import random
import time
from datetime import datetime, timedelta, timezone

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

TOTAL_ROWS = 1_000_000

TITLES = [
    "Backend Engineer", "Senior Backend Engineer", "Staff Engineer",
    "Principal Engineer", "Software Engineer", "Senior Software Engineer",
    "Python Developer", "Rust Engineer", "Go Developer", "DevOps Engineer",
    "Platform Engineer", "Site Reliability Engineer", "Data Engineer",
    "ML Engineer", "Full Stack Engineer", "Frontend Engineer",
    "Engineering Manager", "Tech Lead", "Solutions Architect",
    "Cloud Engineer",
]

LOCATIONS = [
    "Remote", "New York, NY", "San Francisco, CA", "Austin, TX",
    "Seattle, WA", "London, UK", "Berlin, Germany", "Toronto, Canada",
    "Amsterdam, Netherlands", "Singapore", "Sydney, Australia",
    "Dublin, Ireland", "Warsaw, Poland", "Bangalore, India", "Remote (US Only)",
]

TAGS = [
    "python", "rust", "golang", "java", "typescript", "react", "fastapi",
    "django", "postgresql", "redis", "kafka", "kubernetes", "docker",
    "aws", "gcp", "azure", "terraform", "machine-learning", "data-pipelines",
    "distributed-systems", "microservices", "graphql", "rest-api", "grpc",
    "fintech", "saas", "startup", "series-a", "series-b", "remote-friendly",
]

DESCRIPTIONS = [
    "We are looking for an experienced engineer to join our growing team.",
    "Help us build the next generation of financial infrastructure.",
    "Join a team of world-class engineers solving hard distributed systems problems.",
    "Work on high-throughput data pipelines processing millions of events per second.",
    "Build developer tooling used by thousands of engineers worldwide.",
    "Scale our platform to handle 10x growth over the next 18 months.",
    "Lead technical decisions on a greenfield product with direct customer impact.",
    "Own the backend architecture for our core payment processing system.",
    "Collaborate with ML engineers to bring models into production at scale.",
    "Improve developer experience across our entire microservices ecosystem.",
]

async def generate_rows_stream():
    """Asynchronous generator yielding rows with near-zero memory footprint."""
    base_date = datetime(2020, 1, 1, tzinfo=timezone.utc)

    for i in range(TOTAL_ROWS):
        salary_min = random.choice([60, 80, 100, 120, 140, 160, 180, 200]) * 1000
        salary_max = salary_min + random.choice([20, 30, 40, 50]) * 1000
        num_tags = random.randint(2, 6)
        row_tags = random.sample(TAGS, num_tags)
        # Chronological distribution: spreads 1M rows perfectly over ~5 years
        created_at = base_date + timedelta(days=i * 1825 // TOTAL_ROWS)

        yield (
            random.randint(1, 5000),            # company_id
            random.choice(TITLES),              # title
            random.choice(LOCATIONS),           # location
            salary_min,                         # salary_min
            salary_max,                         # salary_max
            row_tags,                           # tags
            random.random() > 0.1,              # is_active (90% active)
            created_at,                         # created_at
            random.choice(DESCRIPTIONS),        # description
        )

async def seed():
    print(f"Connecting to {DATABASE_URL}")

    async with asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5) as pool:
        async with pool.acquire() as conn:

            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'job_listings')"
            )
            if not exists:
                print("ERROR: job_listings table not found.")
                print("Run: psql ... -f migration.sql first.")
                return

            count = await conn.fetchval("SELECT COUNT(*) FROM job_listings")
            if count >= TOTAL_ROWS:
                print(f"Table already has {count:,} rows. Skipping seed.")
                return

            print(f"Streaming {TOTAL_ROWS:,} rows into database...")
            start = time.perf_counter()

            await conn.copy_records_to_table(
                "job_listings",
                records=generate_rows_stream(),
                columns=[
                    "company_id", "title", "location",
                    "salary_min", "salary_max", "tags",
                    "is_active", "created_at", "description"
                ]
            )

            elapsed = time.perf_counter() - start
            final_count = await conn.fetchval("SELECT COUNT(*) FROM job_listings")
            print(f"\nSeeded {final_count:,} rows in {elapsed:.1f}s "
                  f"({final_count / elapsed:,.0f} rows/sec)")

            size = await conn.fetchval(
                "SELECT pg_size_pretty(pg_total_relation_size('job_listings'))"
            )
            print(f"Table size on disk: {size}")

            print("\nBuilding indexes after bulk load (correct pattern)...")

            indexes = [
                (
                    "idx_salary_btree",
                    "CREATE INDEX idx_salary_btree ON job_listings (salary_min)",
                    "B-tree on salary_min — range queries"
                ),
                (
                    "idx_company_hash",
                    "CREATE INDEX idx_company_hash ON job_listings USING hash (company_id)",
                    "Hash on company_id — pure equality"
                ),
                (
                    "idx_tags_gin",
                    "CREATE INDEX idx_tags_gin ON job_listings USING gin (tags)",
                    "GIN on tags array — contains queries"
                ),
                (
                    "idx_created_brin",
                    "CREATE INDEX idx_created_brin ON job_listings USING brin (created_at)",
                    "BRIN on created_at — range on append-ordered column"
                ),
                (
                    "idx_salary_covering",
                    "CREATE INDEX idx_salary_covering ON job_listings (salary_min) INCLUDE (title, location)",
                    "Covering index — index-only scan without heap visit"
                ),
            ]

            for name, sql, description in indexes:
                t0 = time.perf_counter()
                await conn.execute(sql)
                t1 = time.perf_counter()
                print(f"  {name}: {t1 - t0:.2f}s — {description}")

if __name__ == "__main__":
    asyncio.run(seed())