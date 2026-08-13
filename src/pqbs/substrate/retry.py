"""Serializable transaction retry wrapper.

Usage:
    result, retry_count = with_serializable_retry(conn, my_txn_fn, arg1, arg2, max_attempts=5)

The wrapped function receives the connection as its first argument and must
perform ALL reads and writes inside a single call — the wrapper re-calls it
from scratch on every retry, which is what makes re-reading correct.

Security Invariant 3: The retry wrapper must re-read state on retry. Reusing
stale reads silently reintroduces the anomaly the system exists to prevent.

Security Invariant 4: Serializable isolation is never downgraded on retry
exhaustion. Fail with an explicit RetryExhaustedError.
"""
from __future__ import annotations

import functools
import random
import time
from typing import Any, Callable, TypeVar

import psycopg
import psycopg.errors
import structlog

from pqbs.contracts.exceptions import RetryExhaustedError

logger = structlog.get_logger(__name__)

T = TypeVar("T")


def with_serializable_retry(
    conn: psycopg.Connection[Any],
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 5,
    base_delay: float = 0.05,
    max_delay: float = 2.0,
    **kwargs: Any,
) -> tuple[T, int]:
    """Execute fn(conn, *args, **kwargs) under serializable retry semantics.

    Args:
        conn: An open psycopg3 connection. The connection must NOT be in
              autocommit mode — the caller is responsible for transaction
              lifecycle; this wrapper handles rollback on retry.
        fn: The transaction function. Receives conn as its first positional
            argument. MUST re-read all state on every call — the wrapper
            guarantees this by re-calling from scratch.
        *args: Forwarded to fn after conn.
        max_attempts: Maximum number of total attempts (1 = no retries).
        base_delay: Base backoff delay in seconds (doubled each retry).
        max_delay: Maximum delay cap in seconds.
        **kwargs: Forwarded to fn.

    Returns:
        (result, retry_count) where retry_count is 0 on first-attempt success.

    Raises:
        RetryExhaustedError: When all attempts are exhausted. Isolation is
            NEVER downgraded — we fail explicitly per Security Invariant 4.
        Any non-SerializationFailure exception from fn propagates immediately.
    """
    last_exc: psycopg.errors.SerializationFailure | None = None

    for attempt in range(max_attempts):
        try:
            result = fn(conn, *args, **kwargs)
            retry_count = attempt  # 0 on first success, N on Nth retry success
            if attempt > 0:
                logger.info(
                    "serializable_retry_succeeded",
                    attempt=attempt,
                    retry_count=retry_count,
                )
            return result, retry_count
        except psycopg.errors.SerializationFailure as exc:
            last_exc = exc
            logger.warning(
                "serialization_failure_retrying",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(exc),
            )
            # Roll back before retrying — mandatory to leave the connection in
            # a clean state for the next attempt.
            conn.rollback()

            if attempt < max_attempts - 1:
                delay = min(
                    base_delay * (2 ** attempt) + random.uniform(0, 0.01),
                    max_delay,
                )
                time.sleep(delay)
        # All other exceptions propagate immediately without retry.

    logger.error(
        "retry_exhausted",
        max_attempts=max_attempts,
        last_error=str(last_exc),
    )
    raise RetryExhaustedError(
        f"Exhausted {max_attempts} attempts due to serialization failures"
    ) from last_exc


def retry_serializable(
    max_attempts: int = 5,
    base_delay: float = 0.05,
    max_delay: float = 2.0,
) -> Callable[[Callable[..., T]], Callable[..., tuple[T, int]]]:
    """Decorator that wraps a transaction function with serializable retry logic.

    The decorated function MUST accept conn as its first positional argument.
    Returns a wrapper that returns (result, retry_count).

    Example:
        @retry_serializable(max_attempts=3)
        def my_txn(conn, user_id):
            row = conn.execute("SELECT ...").fetchone()
            conn.execute("UPDATE ...", (row["val"] + 1, user_id))
            conn.commit()
            return row
    """
    def decorator(fn: Callable[..., T]) -> Callable[..., tuple[T, int]]:
        @functools.wraps(fn)
        def wrapper(conn: psycopg.Connection[Any], *args: Any, **kwargs: Any) -> tuple[T, int]:
            return with_serializable_retry(
                conn,
                fn,
                *args,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                **kwargs,
            )
        return wrapper  # type: ignore[return-value]
    return decorator
