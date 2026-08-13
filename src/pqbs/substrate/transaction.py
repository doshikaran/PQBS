"""Transaction lifecycle helpers for the PQBS substrate layer.

These thin wrappers enforce consistent transaction semantics across the write
path. CockroachDB Serverless defaults to SERIALIZABLE isolation, so
begin_serializable is an explicit belt-and-suspenders assertion.
"""
from __future__ import annotations

from typing import Any

import psycopg
import structlog

logger = structlog.get_logger(__name__)


def begin_serializable(conn: psycopg.Connection[Any]) -> None:
    """Begin a SERIALIZABLE transaction.

    In autocommit mode: issues explicit BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE.
    In default (non-autocommit) mode: psycopg3 has already opened an implicit
    transaction on the first execute call; SET TRANSACTION sets its isolation
    level and must be the first statement in that transaction.

    CockroachDB Serverless defaults to SERIALIZABLE, but we assert it explicitly
    so no future connection-pool change can silently downgrade isolation
    (Security Invariant 4).
    """
    if conn.autocommit:
        conn.execute("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
    else:
        conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
    logger.debug("transaction_begun", isolation="serializable", autocommit=conn.autocommit)


def commit(conn: psycopg.Connection[Any]) -> None:
    """Commit the current transaction.

    Raises whatever psycopg raises on commit failure (e.g., a final
    SerializationFailure that the retry wrapper did not catch on the last
    attempt). The caller is responsible for handling that.
    """
    conn.commit()
    logger.debug("transaction_committed")


def rollback(conn: psycopg.Connection[Any]) -> None:
    """Roll back the current transaction, swallowing all errors.

    Swallowing is intentional: rollback is called in error-handling paths
    where the underlying error is the one we want to surface. A secondary
    rollback failure would mask the real exception.
    """
    try:
        conn.rollback()
        logger.debug("transaction_rolled_back")
    except Exception as exc:  # noqa: BLE001
        logger.warning("rollback_failed", error=str(exc))
