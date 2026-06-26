"""
Single source of truth for database connection settings across all
experiments in lab 03. All benchmark scripts import from here.

The Docker Compose database runs on port 5433 to avoid colliding
with any host-installed PostgreSQL on the default 5432.

Override via environment variable:
    export DATABASE_URL="postgresql://postgres:postgres@localhost:5433/lab3"
"""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/lab3"
)

# Parsed components for libraries that need them separately
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5433"))
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")
DB_NAME = os.getenv("DB_NAME", "lab3")