"""Unit tests for AuditEngine (A10).

All tests use mock connections — no external dependencies.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from pqbs.audit.engine import AuditEngine
from pqbs.contracts import TemporalMechanism, TemporalQuery

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_PAST_48H = _NOW - timedelta(hours=48)
_PAST_12H = _NOW - timedelta(hours=12)


def _mock_conn(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows or []
    conn.execute.return_value = cursor
    return conn


def _make_query(
    mechanism: TemporalMechanism = TemporalMechanism.BITEMPORAL,
    as_of: datetime = _NOW,
    **kwargs: Any,
) -> TemporalQuery:
    return TemporalQuery(
        tenant_id=_TENANT,
        as_of=as_of,
        mechanism=mechanism,
        requesting_agent_id="a10-test",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Bitemporal tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bitemporal_query_uses_tx_columns() -> None:
    """Bitemporal SQL must include tx_from and tx_to columns."""
    engine = AuditEngine()
    conn = _mock_conn()

    sqls: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    engine.query_bitemporal(_make_query(), conn)

    assert sqls, "No SQL executed"
    main_sql = sqls[0]
    assert "tx_from" in main_sql.lower(), f"tx_from not found in SQL: {main_sql}"
    assert "tx_to" in main_sql.lower(), f"tx_to not found in SQL: {main_sql}"


@pytest.mark.unit
def test_bitemporal_no_time_bound() -> None:
    """Bitemporal query must not use LIMIT or cap the time range."""
    engine = AuditEngine()
    conn = _mock_conn()

    sqls: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    engine.query_bitemporal(_make_query(), conn)

    assert sqls
    main_sql = sqls[0]
    # No row limit — bitemporal is unbounded by design
    assert "LIMIT" not in main_sql.upper(), (
        "Bitemporal query must not have a LIMIT — it must return all historical beliefs"
    )


# ---------------------------------------------------------------------------
# MVCC tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mvcc_query_uses_as_of_system_time() -> None:
    """MVCC SQL must contain AS OF SYSTEM TIME."""
    engine = AuditEngine()
    conn = _mock_conn()

    sqls: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    result = engine.query_mvcc(_make_query(mechanism=TemporalMechanism.MVCC), conn)

    assert sqls, "No SQL executed"
    main_sql = sqls[0]
    assert "AS OF SYSTEM TIME" in main_sql.upper(), (
        f"Expected AS OF SYSTEM TIME in SQL: {main_sql}"
    )
    # Should return a dict with mechanism key
    assert isinstance(result, dict)
    assert result.get("mechanism") == "mvcc"


@pytest.mark.unit
def test_mvcc_gc_error_returns_graceful_dict() -> None:
    """When CockroachDB raises a GC threshold error, MVCC returns an error dict (not exception)."""
    engine = AuditEngine()
    conn = MagicMock()

    def raise_gc_error(sql: str, params: Any = None) -> MagicMock:
        raise Exception(
            "pq: AS OF SYSTEM TIME: cannot read data within gc threshold"
        )

    conn.execute.side_effect = raise_gc_error

    result = engine.query_mvcc(_make_query(mechanism=TemporalMechanism.MVCC), conn)

    assert isinstance(result, dict), "Expected dict result on GC error"
    assert "error" in result, f"Expected 'error' key in result: {result}"
    assert result["error"] == "MVCC window exceeded"
    assert "suggestion" in result
    assert "bitemporal" in result["suggestion"].lower()


# ---------------------------------------------------------------------------
# query_auto tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_auto_recent_uses_mvcc() -> None:
    """A timestamp within the last 24h should trigger MVCC mechanism."""
    engine = AuditEngine()
    conn = _mock_conn()

    sqls: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    recent = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    result = engine.query_auto(_make_query(as_of=recent), conn)

    assert isinstance(result, dict)
    mechanism = result.get("mechanism_used") or result.get("mechanism")
    assert mechanism == "mvcc", f"Expected mvcc for recent timestamp, got: {mechanism}"


@pytest.mark.unit
def test_query_auto_old_uses_bitemporal() -> None:
    """A timestamp older than 48h should trigger bitemporal mechanism."""
    engine = AuditEngine()
    conn = _mock_conn()

    sqls: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    old = datetime.now(tz=timezone.utc) - timedelta(hours=48)
    result = engine.query_auto(_make_query(as_of=old), conn)

    assert isinstance(result, dict)
    mechanism = result.get("mechanism_used")
    assert mechanism == "bitemporal", (
        f"Expected bitemporal for old timestamp (48h ago), got: {mechanism}"
    )


# ---------------------------------------------------------------------------
# Attribution tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_attribution_returns_belief_and_quarantine_keys() -> None:
    """get_attribution must return a dict with 'belief', 'quarantine', 'influenced_queries'."""
    engine = AuditEngine()

    belief_id = uuid.uuid4()
    call_index = 0

    def side_effect(sql: str, params: Any = None) -> MagicMock:
        nonlocal call_index
        cursor = MagicMock()
        if call_index == 0:
            # belief + provenance query
            cursor.fetchall.return_value = [
                {
                    "belief_id": belief_id,
                    "subject": "Alice",
                    "predicate": "works_at",
                    "object": "Acme",
                    "status": "trusted",
                    "author_agent_id": "agent-v1",
                    "tx_from": _NOW,
                    "screened_at": _NOW,
                    "confidence": 0.9,
                    "trust_score": 0.85,
                    "source_type": "user_statement",
                    "source_trust_tier": "corroborated",
                    "source_uri": None,
                    "source_digest": "a" * 64,
                    "derived_from": None,
                }
            ]
        elif call_index == 1:
            # quarantine + verdict query
            cursor.fetchall.return_value = []
        else:
            # retrieval_log query
            cursor.fetchall.return_value = []
        call_index += 1
        return cursor

    conn = MagicMock()
    conn.execute.side_effect = side_effect

    result = engine.get_attribution(belief_id, _TENANT, conn)

    assert "belief" in result, f"Expected 'belief' key in result: {result}"
    assert "quarantine" in result, f"Expected 'quarantine' key in result: {result}"
    assert "influenced_queries" in result, f"Expected 'influenced_queries' key in result: {result}"
    assert isinstance(result["influenced_queries"], list)


# ---------------------------------------------------------------------------
# Diff tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_diff_returns_added_removed_keys() -> None:
    """diff_beliefs must return a dict with 'added', 'removed', 'count_t1', 'count_t2'."""
    engine = AuditEngine()

    t1 = _NOW - timedelta(hours=2)
    t2 = _NOW

    belief_id_old = str(uuid.uuid4())
    belief_id_new = str(uuid.uuid4())

    t1_row = {
        "belief_id": belief_id_old,
        "subject": "Alice",
        "predicate": "works_at",
        "object": "OldCorp",
        "tx_from": t1.isoformat(),
        "tx_to": t2.isoformat(),
        "source_type": "user_statement",
        "source_trust_tier": "unverified",
        "source_uri": None,
        "author_agent_id": "agent-v1",
    }
    t2_row = {
        "belief_id": belief_id_new,
        "subject": "Alice",
        "predicate": "works_at",
        "object": "NewCorp",
        "tx_from": t2.isoformat(),
        "tx_to": None,
        "source_type": "user_statement",
        "source_trust_tier": "corroborated",
        "source_uri": None,
        "author_agent_id": "agent-v1",
    }

    call_index = 0

    def side_effect(sql: str, params: Any = None) -> MagicMock:
        nonlocal call_index
        cursor = MagicMock()
        # First call is for T1, second for T2
        if call_index == 0:
            cursor.fetchall.return_value = [t1_row]
        else:
            cursor.fetchall.return_value = [t2_row]
        call_index += 1
        return cursor

    conn = MagicMock()
    conn.execute.side_effect = side_effect

    result = engine.diff_beliefs(_TENANT, t1, t2, conn)

    assert "added" in result, f"Expected 'added' key: {result}"
    assert "removed" in result, f"Expected 'removed' key: {result}"
    assert "count_t1" in result, f"Expected 'count_t1' key: {result}"
    assert "count_t2" in result, f"Expected 'count_t2' key: {result}"
    assert result["count_t1"] == 1
    assert result["count_t2"] == 1
    assert len(result["added"]) == 1
    assert len(result["removed"]) == 1
