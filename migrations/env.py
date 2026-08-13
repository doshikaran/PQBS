"""Alembic environment configuration for PQBS — CockroachDB Serverless.

Uses sqlalchemy-cockroachdb dialect (cockroachdb+psycopg://) which handles
CockroachDB's non-standard version string correctly.

Install: pip install sqlalchemy-cockroachdb
"""
from __future__ import annotations

import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

load_dotenv()

# ---------------------------------------------------------------------------
# Alembic config object
# ---------------------------------------------------------------------------
config = context.config

# Build CockroachDB-specific SQLAlchemy URL.
# The .env stores: postgresql://user:pass@host:port/db?sslmode=verify-full
# SQLAlchemy-cockroachdb with psycopg3 needs: cockroachdb+psycopg://...
_raw_url = os.environ["COCKROACH_URL"]
# Replace the scheme for the CockroachDB dialect + psycopg3 driver
url = _raw_url.replace("postgresql://", "cockroachdb+psycopg://", 1)
config.set_main_option("sqlalchemy.url", url)


def run_migrations_offline() -> None:
    """Run migrations in offline mode (generates SQL without connecting)."""
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in online mode.

    Uses cockroachdb+psycopg:// dialect which properly handles CockroachDB's
    version string and serializable isolation semantics.
    """
    connectable = create_engine(
        url,
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            compare_type=True,
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
