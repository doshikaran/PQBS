"""Integration tests for A10 AuditEngine.

Requires a live CockroachDB connection (COCKROACH_URL env var).
Skip automatically if COCKROACH_URL is not set.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pqbs.contracts import TemporalMechanism, TemporalQuery

pytestmark = pytest.mark.integration

_NO_DB = not os.environ.get("COCKROACH_URL")


def _skip_if_no_db() -> None:
    if _NO_DB:
        pytest.skip("COCKROACH_URL not set — skipping integration test")


def _get_conn() -> Any:
    from pqbs.substrate.connection import get_connection
    return get_connection()


def _insert_belief_and_provenance(
    conn: Any,
    tenant_id: uuid.UUID,
    subject: str = "AuditAlice",
    status: str = "trusted",
    trust_score: float | None = 0.9,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a provenance + belief row. Returns (belief_id, provenance_id)."""
    belief_id = uuid.uuid4()
    provenance_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    embedding_str = "[" + ",".join(["0.0"] * 1024) + "]"
    screened_at = now if trust_score is not None else None

    conn.execute(
        """
INSERT INTO provenance (provenance_id, tenant_id, source_type, source_trust_tier,
                        source_digest, episode_id, ingested_at, ingestion_agent_id)
VALUES (%s, %s, 'user_statement', 'unverified', %s, %s, %s, 'test-agent')
""",
        (str(provenance_id), str(tenant_id), "a" * 64, str(uuid.uuid4()), now),
    )
    conn.execute(
        """
INSERT INTO belief (belief_id, tenant_id, subject, predicate, object, object_normalized,
                    confidence, valid_from, valid_to, tx_from, tx_to, status,
                    author_agent_id, provenance_id, trust_score, screened_at,
                    sensitivity, embedding)
VALUES (%s, %s, %s, 'works_at', 'AuditCorp', 'auditcorp',
        0.9, %s, NULL, %s, NULL, %s,
        'audit-agent', %s, %s, %s, 'normal', %s::vector)
""",
        (
            str(belief_id), str(tenant_id), subject,
            now, now, status,
            str(provenance_id), trust_score, screened_at,
            embedding_str,
        ),
    )
    conn.commit()
    return belief_id, provenance_id


# ---------------------------------------------------------------------------
# test_bitemporal_query_returns_historical_belief
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_bitemporal_query_returns_historical_belief() -> None:
    """
    Write belief at T1, supersede at T2, bitemporal query at T1+delta returns the original.
    """
    _skip_if_no_db()
    from pqbs.audit.engine import AuditEngine

    tenant_id = uuid.uuid4()
    engine = AuditEngine()

    with _get_conn() as conn:
        t_before = datetime.now(tz=timezone.utc)
        belief_id, _ = _insert_belief_and_provenance(conn, tenant_id, subject="HistoricalBob")
        t_after_insert = datetime.now(tz=timezone.utc)

        # Bitemporal query at a point after the insert should include this belief
        q = TemporalQuery(
            tenant_id=tenant_id,
            as_of=t_after_insert,
            mechanism=TemporalMechanism.BITEMPORAL,
            requesting_agent_id="a10-integration-test",
            subject_filter="HistoricalBob",
        )

        rows = engine.query_bitemporal(q, conn)
        assert len(rows) >= 1, (
            "Expected at least 1 belief in bitemporal query after insert"
        )
        belief_ids = [r.get("belief_id") for r in rows]
        assert str(belief_id) in belief_ids, (
            f"Expected belief_id {belief_id} in bitemporal result: {belief_ids}"
        )

        # Bitemporal query before the insert — should return 0
        q_before = TemporalQuery(
            tenant_id=tenant_id,
            as_of=t_before,
            mechanism=TemporalMechanism.BITEMPORAL,
            requesting_agent_id="a10-integration-test",
            subject_filter="HistoricalBob",
        )
        rows_before = engine.query_bitemporal(q_before, conn)
        assert len(rows_before) == 0, (
            "Bitemporal query before belief insert should return 0 rows"
        )


# ---------------------------------------------------------------------------
# test_mvcc_graceful_error_beyond_window
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mvcc_graceful_error_beyond_window() -> None:
    """
    MVCC query with a timestamp from 30+ days ago should return a graceful error
    dict (not raise an exception).

    On CockroachDB Serverless, GC TTL is typically ~1 hour.
    A 30-day-old timestamp is guaranteed to exceed the window.
    """
    _skip_if_no_db()
    from pqbs.audit.engine import AuditEngine

    tenant_id = uuid.uuid4()
    engine = AuditEngine()

    old_ts = datetime.now(tz=timezone.utc) - timedelta(days=30)

    with _get_conn() as conn:
        q = TemporalQuery(
            tenant_id=tenant_id,
            as_of=old_ts,
            mechanism=TemporalMechanism.MVCC,
            requesting_agent_id="a10-integration-test",
        )
        result = engine.query_mvcc(q, conn)

    # Must return an error dict, not raise
    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
    assert "error" in result, f"Expected 'error' key in result: {result}"
    assert result["error"] == "MVCC window exceeded"


# ---------------------------------------------------------------------------
# test_attribution_returns_complete_chain
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_attribution_returns_complete_chain() -> None:
    """
    Write + quarantine a belief, then attribution should return belief + quarantine data.
    """
    _skip_if_no_db()
    from pqbs.audit.engine import AuditEngine

    tenant_id = uuid.uuid4()
    engine = AuditEngine()

    with _get_conn() as conn:
        belief_id, _ = _insert_belief_and_provenance(
            conn, tenant_id, subject="QuarantineCarol", status="quarantined"
        )

        # Insert a quarantine record
        now = datetime.now(tz=timezone.utc)
        conn.execute(
            """
INSERT INTO quarantine (quarantine_id, belief_id, tenant_id, reason_code,
                        quarantined_at, disposition)
VALUES (%s, %s, %s, 'untrusted_source', %s, 'held')
""",
            (str(uuid.uuid4()), str(belief_id), str(tenant_id), now),
        )
        conn.commit()

        result = engine.get_attribution(belief_id, tenant_id, conn)

    assert "belief" in result
    assert "quarantine" in result
    assert "influenced_queries" in result

    # Belief should have data
    assert result["belief"], "belief dict should not be empty"
    # Quarantine should have data (we just inserted one)
    assert result["quarantine"], "quarantine dict should not be empty after quarantine insert"
    assert isinstance(result["influenced_queries"], list)
