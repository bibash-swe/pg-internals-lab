"""
Generates realistic, category-coherent support article text, embeds
it via REAL calls to Mistral's embeddings API, and persists it
INCREMENTALLY -- one batch at a time, not accumulated in memory for
a single final write. This is the fix for a real failure mode this
lab hit directly: a crash mid-run at batch ~123 threw away roughly
3,900 rows of already-paid-for API calls, because nothing had been
written to the database yet when the process died.

RESUMABILITY:
    On startup, this script counts existing rows and continues
    generation from that exact index, preserving the category
    round-robin assignment. Running this script again after any
    crash or rate-limit failure picks up where it left off -- it
    never re-embeds rows that are already safely persisted, and
    never loses rows that were already paid for.

WHY register_vector() REPLACES MANUAL STRING FORMATTING:
    The original version of this script formatted each embedding as
    a bracketed string ("[0.1,0.2,...]") and relied on asyncpg's
    default encoding to get that string into a `vector` column. This
    works for a plain INSERT (which uses PostgreSQL's extended query
    protocol and can cast text to vector implicitly), but COPY's
    binary protocol is far stricter about type matching -- asyncpg
    has no built-in codec for pgvector's custom type, and without one,
    it doesn't know how to encode a Python value into the exact byte
    layout PostgreSQL expects for `vector`. The `pgvector` Python
    package (already in this lab's requirements.txt) ships exactly
    this missing codec via `register_vector()`. Once registered on a
    connection, you pass a plain Python list of floats directly as
    the column value -- no manual string formatting, no CSV
    round-trip, no risk of a comma inside a formatted vector string
    being mishandled by a hand-rolled encoder.

WHY EMBEDDING REQUESTS ARE BATCHED, NOT ONE-PER-ROW:
    Firing 10,000 individual embedding API calls would repeat the
    exact mistake proven twice already in this lab series: N+1
    database queries costing O(n) round-trips versus O(1) for a
    JOIN, and 200 individual writes costing ~609x more per-row
    replication lag than one batched insert. This is the same
    lesson applied to a third kind of round-trip -- an HTTP request.
    BATCH_SIZE controls how many texts are embedded per API call.

Run:
    python seed.py

Safe to re-run at any point -- it will resume, not restart or
duplicate.
"""
import asyncio
import os
import random
import time
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv
from faker import Faker
from pgvector.asyncpg import register_vector

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
if not MISTRAL_API_KEY:
    raise RuntimeError(f"MISTRAL_API_KEY not found in {ENV_PATH}")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/lab3"
)

MISTRAL_EMBEDDINGS_URL = "https://api.mistral.ai/v1/embeddings"
MODEL = "mistral-embed"

TOTAL_ROWS = 10_000
BATCH_SIZE = 32
MAX_RETRIES = 8  # worst-case cumulative backoff: 1+2+4+...+128 = 255s
INTER_BATCH_DELAY_SEC = 0.3  # small proactive pacing between successful
                              # batches, to reduce (not eliminate) the
                              # chance of hitting the rate limit at all,
                              # rather than only reacting after the fact

fake = Faker()

CATEGORY_TEMPLATES = {
    "billing": [
        "I was charged twice for my {month} subscription, order #{order_id}. Please refund the duplicate charge of ${amount}.",
        "My invoice for order #{order_id} shows ${amount}, which is higher than the price I was quoted at checkout.",
        "There is an unrecognized charge of ${amount} on my card statement referencing order #{order_id}. Can you explain this?",
        "I canceled my subscription last month but was still billed ${amount} on {month} {day}. Please issue a refund.",
        "The tax amount on invoice #{order_id} seems incorrect -- I was charged ${amount} in tax on a ${amount} order.",
    ],
    "shipping": [
        "My order #{order_id} was supposed to arrive by {month} {day}, but tracking still shows it hasn't shipped.",
        "The package for order #{order_id} arrived damaged. The {product} inside was broken on arrival.",
        "I received someone else's order instead of mine. My order number is #{order_id}.",
        "Tracking for order #{order_id} shows delivered, but I never received the {product} at my address.",
        "Can I change the shipping address for order #{order_id}? It hasn't shipped yet according to the status page.",
    ],
    "returns": [
        "I would like to return the {product} from order #{order_id}. It doesn't fit as expected.",
        "How do I initiate a return for order #{order_id}? The {product} arrived in the wrong size.",
        "I returned the {product} from order #{order_id} three weeks ago and haven't received my refund of ${amount}.",
        "The return label for order #{order_id} isn't working when I try to print it.",
        "Can I exchange the {product} from order #{order_id} for a different color instead of a full return?",
    ],
    "account_access": [
        "I can't log into my account anymore. It says my password is incorrect even after I reset it.",
        "My account got locked after several failed login attempts. Can you help me regain access?",
        "I'm not receiving the two-factor authentication code when I try to log in to my account.",
        "I changed my email address but now I can't log in with either my old or new email.",
        "My account shows as suspended with no explanation. I haven't violated any policies that I know of.",
    ],
    "technical_issue": [
        "The mobile app crashes every time I try to open the {product} details page.",
        "Your website is showing a 500 error whenever I try to complete checkout for order #{order_id}.",
        "The search feature on your site isn't returning any results for {product} even though I know you sell it.",
        "I'm unable to upload a photo for my review of the {product} I purchased.",
        "The checkout page freezes when I try to apply a discount code to my cart.",
    ],
    "product_defect": [
        "The {product} I received from order #{order_id} stopped working after only {day} days of use.",
        "There's a manufacturing defect in the {product} from order #{order_id} -- a visible crack on arrival.",
        "The {product} doesn't match the description on the product page at all. It's missing key features.",
        "My {product} from order #{order_id} has a battery that won't hold a charge for more than an hour.",
        "The {product} arrived with a strong chemical smell that hasn't gone away after a week.",
    ],
    "subscription": [
        "How do I upgrade my subscription plan from the basic tier to premium?",
        "I want to pause my subscription for {month} instead of canceling it entirely. Is that possible?",
        "My subscription renewed automatically but I thought I had canceled it last {month}.",
        "What's the difference between the monthly and annual subscription pricing for premium?",
        "I'd like to downgrade my subscription plan starting next billing cycle on {month} {day}.",
    ],
    "security": [
        "I received a suspicious email claiming to be from your company asking for my password. Is this legitimate?",
        "I think someone accessed my account without permission -- there are orders I never placed.",
        "Can you tell me what personal data your company stores about me and how to request deletion?",
        "I want to enable two-factor authentication on my account but can't find the setting.",
        "My saved payment method was used for a purchase I didn't authorize on {month} {day}.",
    ],
}

PRODUCTS = [
    "wireless headphones", "laptop stand", "coffee maker", "running shoes",
    "desk lamp", "backpack", "smartwatch", "bluetooth speaker",
    "office chair", "water bottle", "phone case", "keyboard",
]


def generate_article(category: str) -> tuple[str, str]:
    template = random.choice(CATEGORY_TEMPLATES[category])
    body = template.format(
        order_id=fake.random_number(digits=8),
        amount=round(random.uniform(9.99, 499.99), 2),
        month=random.choice(["January", "February", "March", "April", "May",
                             "June", "July", "August", "September",
                             "October", "November", "December"]),
        day=random.randint(1, 28),
        product=random.choice(PRODUCTS),
    )
    title = body.split(".")[0][:80]
    return title, body


async def get_embeddings_batch(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """
    Embeds a batch of texts in one API call. On 429, respects the
    server's own Retry-After header if present -- honoring the
    server's guidance is more correct than guessing a backoff
    duration, since the server actually knows its own rate limit
    window. Falls back to exponential backoff only if no such
    header is provided.
    """
    for attempt in range(MAX_RETRIES):
        response = await client.post(
            MISTRAL_EMBEDDINGS_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": MODEL, "input": texts},
            timeout=60.0,
        )

        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("retry-after")
            wait = float(retry_after) if retry_after else 2 ** attempt
            print(f"    Rate limited or server error (status "
                  f"{response.status_code}). Retrying in {wait}s "
                  f"(attempt {attempt + 1}/{MAX_RETRIES})"
                  f"{' [server-specified via Retry-After]' if retry_after else ''}...")
            await asyncio.sleep(wait)
            continue

        response.raise_for_status()
        data = response.json()
        return [item["embedding"] for item in data["data"]]

    raise RuntimeError(f"Failed to get embeddings after {MAX_RETRIES} retries")


async def seed():
    print(f"Connecting to {DATABASE_URL}")
    conn = await asyncpg.connect(DATABASE_URL)
    await register_vector(conn)
    # From this point on, embedding lists (plain Python lists of floats)
    # can be passed directly as `vector` column values -- to both
    # regular queries AND copy_records_to_table -- with no manual
    # string formatting. This is the fix for the binary-encoding gap
    # that manual formatting was working around.

    try:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'support_articles')"
        )
        if not exists:
            print("ERROR: support_articles not found. Run migration.sql first.")
            return

        current_count = await conn.fetchval("SELECT COUNT(*) FROM support_articles")

        if current_count >= TOTAL_ROWS:
            print(f"support_articles already has {current_count:,} rows "
                  f"(target {TOTAL_ROWS:,}). Nothing to do.")
            return
        elif current_count > 0:
            print(f"Found {current_count:,} existing rows. Resuming from here...")
        else:
            print("Table is empty. Starting fresh...")

        categories = list(CATEGORY_TEMPLATES.keys())
        articles = []
        for i in range(current_count, TOTAL_ROWS):
            category = categories[i % len(categories)]
            title, body = generate_article(category)
            articles.append((title, body, category))

        total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Embedding and persisting {len(articles):,} remaining "
              f"rows in {total_batches} batches...")

        start = time.perf_counter()
        rows_done = current_count

        async with httpx.AsyncClient() as client:
            for batch_start in range(0, len(articles), BATCH_SIZE):
                batch = articles[batch_start:batch_start + BATCH_SIZE]
                texts = [f"{title}. {body}" for title, body, _ in batch]

                embeddings = await get_embeddings_batch(client, texts)

                # Plain Python lists of floats, passed directly --
                # register_vector() on this connection handles the
                # rest. No string formatting, no CSV.
                db_batch = [
                    (title, body, category, embedding)
                    for (title, body, category), embedding in zip(batch, embeddings)
                ]

                await conn.copy_records_to_table(
                    "support_articles",
                    records=db_batch,
                    columns=["title", "body", "category", "embedding"],
                )

                rows_done += len(db_batch)
                batch_num = batch_start // BATCH_SIZE + 1

                if batch_num % 10 == 0 or batch_num == total_batches:
                    elapsed = time.perf_counter() - start
                    print(f"  Batch {batch_num}/{total_batches} "
                          f"({rows_done:,} total rows persisted, "
                          f"{elapsed:.1f}s elapsed)")

                if batch_start + BATCH_SIZE < len(articles):
                    await asyncio.sleep(INTER_BATCH_DELAY_SEC)

        total_time = time.perf_counter() - start
        print(f"\nSeeding complete in {total_time:.1f}s")

        final_count = await conn.fetchval("SELECT COUNT(*) FROM support_articles")
        size = await conn.fetchval(
            "SELECT pg_size_pretty(pg_total_relation_size('support_articles'))"
        )
        print(f"Final: {final_count:,} rows, table size {size}")

        cat_counts = await conn.fetch(
            "SELECT category, COUNT(*) FROM support_articles "
            "GROUP BY category ORDER BY category"
        )
        print("\nRows per category:")
        for row in cat_counts:
            print(f"  {row['category']:<20} {row['count']:,}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())