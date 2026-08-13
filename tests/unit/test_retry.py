"""Unit tests for pqbs.substrate.retry.

Tests cover:
- First-attempt success (retry_count=0)
- Single SerializationFailure then success (retry_count=1)
- Non-retryable error propagates immediately
- Exhaustion raises RetryExhaustedError
- retry_count tracks actual retries
- @retry_serializable decorator behaves identically
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import psycopg
import psycopg.errors

from pqbs.substrate.retry import retry_serializable, with_serializable_retry
from pqbs.contracts.exceptions import RetryExhaustedError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: construct a SerializationFailure without needing a real DB
# ---------------------------------------------------------------------------

def _make_serialization_failure() -> psycopg.errors.SerializationFailure:
    """Create a SerializationFailure instance without a real DB."""
    exc = psycopg.errors.SerializationFailure.__new__(
        psycopg.errors.SerializationFailure
    )
    # Initialize the base Exception
    Exception.__init__(exc, "40001 serialization failure")
    return exc


def _make_conn() -> MagicMock:
    """Return a mock psycopg connection."""
    conn = MagicMock(spec=psycopg.Connection)
    conn.rollback = MagicMock()
    return conn


# ---------------------------------------------------------------------------
# with_serializable_retry tests
# ---------------------------------------------------------------------------

class TestWithSerializableRetry:
    def test_success_on_first_attempt(self) -> None:
        """fn succeeds on the first call → result returned, retry_count=0."""
        conn = _make_conn()

        def fn(c: psycopg.Connection, x: int) -> int:
            return x * 2

        result, retry_count = with_serializable_retry(conn, fn, 21, max_attempts=5)
        assert result == 42
        assert retry_count == 0
        conn.rollback.assert_not_called()

    def test_retry_on_serialization_failure(self) -> None:
        """fn fails once with SerializationFailure, then succeeds → retry_count=1."""
        conn = _make_conn()
        call_count = 0

        def fn(c: psycopg.Connection) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _make_serialization_failure()
            return "ok"

        with patch("pqbs.substrate.retry.time.sleep"):  # don't actually sleep
            result, retry_count = with_serializable_retry(conn, fn, max_attempts=5)

        assert result == "ok"
        assert retry_count == 1
        assert call_count == 2
        conn.rollback.assert_called_once()

    def test_non_retryable_error_propagates(self) -> None:
        """fn raises ValueError → propagates immediately without retry."""
        conn = _make_conn()
        call_count = 0

        def fn(c: psycopg.Connection) -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("not a serialization issue")

        with pytest.raises(ValueError, match="not a serialization issue"):
            with_serializable_retry(conn, fn, max_attempts=5)

        assert call_count == 1
        conn.rollback.assert_not_called()

    def test_exhaustion_raises_retry_exhausted_error(self) -> None:
        """fn always fails with SerializationFailure → RetryExhaustedError."""
        conn = _make_conn()
        call_count = 0

        def fn(c: psycopg.Connection) -> None:
            nonlocal call_count
            call_count += 1
            raise _make_serialization_failure()

        with patch("pqbs.substrate.retry.time.sleep"):
            with pytest.raises(RetryExhaustedError, match="Exhausted 3 attempts"):
                with_serializable_retry(conn, fn, max_attempts=3)

        assert call_count == 3
        assert conn.rollback.call_count == 3

    def test_retry_count_tracked(self) -> None:
        """fn fails 3 times, succeeds on 4th → retry_count=3."""
        conn = _make_conn()
        call_count = 0

        def fn(c: psycopg.Connection) -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise _make_serialization_failure()
            return "done"

        with patch("pqbs.substrate.retry.time.sleep"):
            result, retry_count = with_serializable_retry(conn, fn, max_attempts=5)

        assert result == "done"
        assert retry_count == 3
        assert call_count == 4
        assert conn.rollback.call_count == 3

    def test_args_forwarded_to_fn(self) -> None:
        """Positional and keyword arguments are forwarded correctly."""
        conn = _make_conn()

        def fn(c: psycopg.Connection, a: int, b: int, *, multiplier: int = 1) -> int:
            return (a + b) * multiplier

        result, retry_count = with_serializable_retry(
            conn, fn, 3, 4, multiplier=10, max_attempts=1
        )
        assert result == 70
        assert retry_count == 0

    def test_no_retry_when_max_attempts_is_one(self) -> None:
        """max_attempts=1 means no retries at all — one failure → exhausted."""
        conn = _make_conn()

        def fn(c: psycopg.Connection) -> None:
            raise _make_serialization_failure()

        with patch("pqbs.substrate.retry.time.sleep"):
            with pytest.raises(RetryExhaustedError):
                with_serializable_retry(conn, fn, max_attempts=1)

        assert conn.rollback.call_count == 1

    def test_rollback_called_before_each_retry(self) -> None:
        """rollback() is called exactly once per retry (before re-executing fn)."""
        conn = _make_conn()
        call_count = 0

        def fn(c: psycopg.Connection) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _make_serialization_failure()
            return "success"

        with patch("pqbs.substrate.retry.time.sleep"):
            result, retry_count = with_serializable_retry(conn, fn, max_attempts=5)

        assert result == "success"
        assert retry_count == 2
        # rollback called once per failed attempt (attempts 1 and 2)
        assert conn.rollback.call_count == 2


# ---------------------------------------------------------------------------
# @retry_serializable decorator tests
# ---------------------------------------------------------------------------

class TestRetrySerializableDecorator:
    def test_decorator_success_on_first_attempt(self) -> None:
        """Decorated function succeeds → (result, 0)."""
        conn = _make_conn()

        @retry_serializable(max_attempts=3)
        def my_txn(c: psycopg.Connection, value: str) -> str:
            return value.upper()

        result, retry_count = my_txn(conn, "hello")
        assert result == "HELLO"
        assert retry_count == 0

    def test_decorator_retries_on_serialization_failure(self) -> None:
        """Decorated function retries correctly."""
        conn = _make_conn()
        call_count = 0

        @retry_serializable(max_attempts=5)
        def my_txn(c: psycopg.Connection) -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise _make_serialization_failure()
            return 99

        with patch("pqbs.substrate.retry.time.sleep"):
            result, retry_count = my_txn(conn)

        assert result == 99
        assert retry_count == 1

    def test_decorator_exhaustion_raises(self) -> None:
        """Decorator propagates RetryExhaustedError on exhaustion."""
        conn = _make_conn()

        @retry_serializable(max_attempts=2)
        def my_txn(c: psycopg.Connection) -> None:
            raise _make_serialization_failure()

        with patch("pqbs.substrate.retry.time.sleep"):
            with pytest.raises(RetryExhaustedError):
                my_txn(conn)

    def test_decorator_preserves_function_name(self) -> None:
        """functools.wraps should preserve the wrapped function's __name__."""
        @retry_serializable()
        def my_special_txn(c: psycopg.Connection) -> None:
            pass

        assert my_special_txn.__name__ == "my_special_txn"
