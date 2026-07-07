"""
Step 0: Confirm the real embedding dimension

Run:
    python get_embedding_dimension.py
"""
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
if not MISTRAL_API_KEY:
    raise RuntimeError(
        f"MISTRAL_API_KEY not found. Expected it in {ENV_PATH} "
        f"as: MISTRAL_API_KEY=your-key-here"
    )

MISTRAL_EMBEDDINGS_URL = "https://api.mistral.ai/v1/embeddings"
MODEL = "mistral-embed"


def main():
    print(f"Loaded API key from {ENV_PATH}")
    print(f"Calling Mistral embeddings API (model={MODEL})...")

    response = httpx.post(
        MISTRAL_EMBEDDINGS_URL,
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "input": ["This is a test sentence to confirm embedding dimension."],
        },
        timeout=30.0,
    )

    response.raise_for_status()
    data = response.json()

    embedding = data["data"][0]["embedding"]
    dimension = len(embedding)

    print(f"\nConfirmed embedding dimension: {dimension}")
    print(f"First 5 values (sanity check, not zero/NaN): {embedding[:5]}")


if __name__ == "__main__":
    main()