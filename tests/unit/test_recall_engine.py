"""Unit tests for RecallEngine (A9).

All tests use mock connections — no external dependencies.
Embedding is mocked to avoid Bedrock calls.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pqbs.contracts import RecallRequest, RecallResult, TemporalContext
from pqbs.recall.engine import RecallEngine

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_EMBEDDING = tuple(0.1 for _ in range(1024))


def _mock_conn(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    """Build a mock psycopg connection that returns the given rows on execute."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows or []
    conn.execute.return_value = cursor
    return conn


def _make_request(**kwargs: Any) -> RecallRequest:
    defaults = dict(
        query="What does Alice do?",
        tenant_id=_TENANT,
        limit=5,
    )
    defaults.update(kwargs)
    return RecallRequest(**defaults)


# ---------------------------------------------------------------------------
# test_recall_builds_vector_string
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_builds_vector_string() -> None:
    """Embedding must be formatted as '[x,y,...]' string for the vector param."""
    engine = RecallEngine()
    conn = _mock_conn()

    captured_sqls: list[str] = []

    def capture_execute(sql: str, params: Any = None) -> MagicMock:
        captured_sqls.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture_execute

    with patch(
        "pqbs.recall.engine.embed_text",
        return_value=_EMBEDDING,
    ):
        engine.recall(_make_request(), conn)

    # The first SELECT should contain the vector string param
    # Find the call that included the vector string
    assert any(captured_sqls), "No SQL was executed"

    # The embed_text return value should be formatted into a vector string
    # Verify by checking the parameters passed to execute
    call_args_list = conn.execute.call_args_list
    # The main SELECT call should have the vector string as a parameter
    found_vector_param = False
    for call in call_args_list:
        args = call[0]
        if len(args) >= 2:
            params = args[1]
            if isinstance(params, (list, tuple)):
                for p in params:
                    if isinstance(p, str) and p.startswith("[") and "," in p and p.endswith("]"):
                        found_vector_param = True
                        # Verify format: [0.1,0.1,...,0.1]
                        inner = p[1:-1]
                        values = inner.split(",")
                        assert len(values) == 1024, f"Expected 1024 values, got {len(values)}"
                        break

    assert found_vector_param, "No vector string '[x,y,...]' found in execute call params"


# ---------------------------------------------------------------------------
# test_recall_queries_trusted_current_view
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_queries_trusted_current_view() -> None:
    """The recall SQL must query v_trusted_current, not the belief table directly."""
    engine = RecallEngine()
    conn = _mock_conn()

    sqls_executed: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls_executed.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    with patch("pqbs.recall.engine.embed_text", return_value=_EMBEDDING):
        engine.recall(_make_request(), conn)

    main_select = next((s for s in sqls_executed if "SELECT" in s.upper() and "FROM" in s.upper()), None)
    assert main_select is not None, "No SELECT statement executed"
    assert "v_trusted_current" in main_select, (
        f"Expected v_trusted_current in SQL, got: {main_select[:200]}"
    )
    assert "FROM belief" not in main_select.replace("v_trusted_current", ""), (
        "SQL must not query 'belief' table directly"
    )


# ---------------------------------------------------------------------------
# test_recall_logs_retrieval
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_logs_retrieval() -> None:
    """An INSERT into retrieval_log must happen on every recall call."""
    engine = RecallEngine()
    conn = _mock_conn()

    sqls_executed: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls_executed.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    with patch("pqbs.recall.engine.embed_text", return_value=_EMBEDDING):
        engine.recall(_make_request(), conn)

    insert_sqls = [s for s in sqls_executed if "INSERT INTO retrieval_log" in s]
    assert len(insert_sqls) >= 1, (
        f"Expected INSERT INTO retrieval_log, got SQLs: {sqls_executed}"
    )
    # Commit must be called after the INSERT
    conn.commit.assert_called()


# ---------------------------------------------------------------------------
# test_recall_result_parallel_arrays
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_result_parallel_arrays() -> None:
    """beliefs, provenance_ids, trust_scores must all have the same length."""
    engine = RecallEngine()

    belief_id = uuid.uuid4()
    provenance_id = uuid.uuid4()
    tenant_id = _TENANT

    row = {
        "belief_id": belief_id,
        "tenant_id": tenant_id,
        "subject": "Alice",
        "predicate": "works_at",
        "object": "Acme Corp",
        "object_normalized": "acme corp",
        "confidence": 0.9,
        "valid_from": _NOW,
        "valid_to": None,
        "tx_from": _NOW,
        "author_agent_id": "agent-v1",
        "provenance_id": provenance_id,
        "trust_score": 0.85,
        "screened_at": _NOW,
    }

    call_count = 0

    def side_effect(sql: str, params: Any = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        cursor = MagicMock()
        if "v_trusted_current" in sql or ("SELECT" in sql.upper() and call_count == 1):
            cursor.fetchall.return_value = [row]
        else:
            cursor.fetchall.return_value = []
        return cursor

    conn = _mock_conn()
    conn.execute.side_effect = side_effect

    with patch("pqbs.recall.engine.embed_text", return_value=_EMBEDDING):
        result = engine.recall(_make_request(), conn)

    assert isinstance(result, RecallResult)
    n = len(result.beliefs)
    assert len(result.provenance_ids) == n, "provenance_ids length mismatch"
    assert len(result.trust_scores) == n, "trust_scores length mismatch"


# ---------------------------------------------------------------------------
# test_recall_applies_temporal_filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_applies_temporal_filter() -> None:
    """When temporal_context.valid_at is set, SQL must contain valid_from filter."""
    engine = RecallEngine()
    conn = _mock_conn()

    sqls_executed: list[str] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls_executed.append(sql)
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    tc = TemporalContext(valid_at=_NOW)
    request = _make_request(temporal_context=tc)

    with patch("pqbs.recall.engine.embed_text", return_value=_EMBEDDING):
        engine.recall(request, conn)

    main_select = next((s for s in sqls_executed if "v_trusted_current" in s), None)
    assert main_select is not None, "No SELECT from v_trusted_current found"
    assert "valid_from" in main_select.lower(), (
        f"Expected valid_from filter in SQL when valid_at is set: {main_select}"
    )


# ---------------------------------------------------------------------------
# test_recall_applies_min_trust_score
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_applies_min_trust_score() -> None:
    """When min_trust_score > 0, SQL must filter on trust_score."""
    engine = RecallEngine()
    conn = _mock_conn()

    sqls_executed: list[str] = []
    params_captured: list[Any] = []

    def capture(sql: str, params: Any = None) -> MagicMock:
        sqls_executed.append(sql)
        if params:
            params_captured.extend(params if isinstance(params, (list, tuple)) else [params])
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = capture

    request = _make_request(min_trust_score=0.7)

    with patch("pqbs.recall.engine.embed_text", return_value=_EMBEDDING):
        engine.recall(request, conn)

    main_select = next((s for s in sqls_executed if "v_trusted_current" in s), None)
    assert main_select is not None
    # The SQL must contain trust_score filter
    assert "trust_score" in main_select.lower(), (
        "Expected trust_score filter in SQL when min_trust_score > 0"
    )
    # The value 0.7 must appear in the params
    assert any(
        isinstance(p, float) and abs(p - 0.7) < 1e-9 for p in params_captured
    ), f"Expected 0.7 in params, got: {params_captured}"
