"""Database connection helpers for PQBS substrate layer.

Provides two connection factories:
  get_connection()             — standard transactional connection with dict rows
  get_autocommit_connection()  — autocommit mode for DDL or admin operations

Both read COCKROACH_URL from the environment (loaded via python-dotenv).
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()


def get_connection() -> psycopg.Connection[Any]:
    """Return a psycopg3 connection in default (transactional) mode.

    Rows are returned as dictionaries (column name → value).
    CockroachDB default isolation is SERIALIZABLE — no need to set it explicitly.
    """
    url = os.environ["COCKROACH_URL"]
    return psycopg.connect(url, row_factory=dict_row)  # type: ignore[arg-type]


def get_autocommit_connection() -> psycopg.Connection[Any]:
    """Return a psycopg3 connection with autocommit=True.

    Use for DDL statements that cannot run inside an explicit transaction,
    or for admin/operational tasks where per-statement commit semantics are needed.
    Rows are returned as dictionaries.
    """
    url = os.environ["COCKROACH_URL"]
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)  # type: ignore[arg-type]
