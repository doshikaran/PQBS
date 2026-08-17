"""Integration tests for A9 RecallEngine.

Requires a live CockroachDB connection (COCKROACH_URL env var).
Skip automatically if COCKROACH_URL is not set.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pqbs.contracts import RecallRequest, TemporalContext

pytestmark = pytest.mark.integration

_NO_DB = not os.environ.get("COCKROACH_URL")


def _skip_if_no_db() -> None:
    if _NO_DB:
        pytest.skip("COCKROACH_URL not set — skipping integration test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_conn() -> Any:
    from pqbs.substrate.connection import get_connection
    return get_connection()


def _insert_belief(
    conn: Any,
    tenant_id: uuid.UUID,
    subject: str = "IntegTestAlice",
    status: str = "pending",
    trust_score: float | None = None,
    screened_at: datetime | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> uuid.UUID:
    """Insert a minimal belief row directly for test setup."""
    belief_id = uuid.uuid4()
    provenance_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    vfrom = valid_from or now
    vto = valid_to

    # Insert provenance first
    conn.execute(
        """
INSERT INTO provenance (provenance_id, tenant_id, source_type, source_trust_tier,
                        source_digest, episode_id, ingested_at, ingestion_agent_id)
VALUES (%s, %s, 'user_statement', 'unverified', %s, %s, %s, 'test-agent')
""",
        (
            str(provenance_id),
            str(tenant_id),
            "a" * 64,
            str(uuid.uuid4()),
            now,
        ),
    )

    # Build embedding zeros (1024 dims)
    embedding_str = "[" + ",".join(["0.0"] * 1024) + "]"

    conn.execute(
        """
INSERT INTO belief (belief_id, tenant_id, subject, predicate, object, object_normalized,
                    confidence, valid_from, valid_to, tx_from, tx_to, status,
                    author_agent_id, provenance_id, trust_score, screened_at,
                    sensitivity, embedding)
VALUES (%s, %s, %s, 'works_at', 'TestCorp', 'testcorp',
        0.9, %s, %s, %s, NULL, %s,
        'test-agent', %s, %s, %s,
        'normal', %s::vector)
""",
        (
            str(belief_id),
            str(tenant_id),
            subject,
            vfrom,
            vto,
            now,
            status,
            str(provenance_id),
            trust_score,
            screened_at,
            embedding_str,
        ),
    )
    conn.commit()
    return belief_id


# ---------------------------------------------------------------------------
# test_recall_returns_only_trusted
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_recall_returns_only_trusted() -> None:
    """
    Pending belief → recall returns 0.
    After trust status set → recall returns 1.

    NOTE: This test requires running as role_consumer or a superuser with
    access to v_trusted_current. The full structural enforcement of
    Security Invariant #2 is tested at the DB role level (see VERIFICATIONS.md).
    """
    _skip_if_no_db()
    from pqbs.recall.engine import RecallEngine

    tenant_id = uuid.uuid4()
    engine = RecallEngine()

    with _get_conn() as conn:
        belief_id = _insert_belief(conn, tenant_id, status="pending")

        # Recall with pending-only belief should return 0 from v_trusted_current
        # (v_trusted_current only shows status='trusted' AND tx_to IS NULL)
        fake_embedding = tuple(0.0 for _ in range(1024))
        import unittest.mock as mock
        with mock.patch("pqbs.recall.engine.embed_text", return_value=fake_embedding):
            request = RecallRequest(
                query="IntegTestAlice works at",
                tenant_id=tenant_id,
                limit=10,
            )
            result = engine.recall(request, conn)

        assert len(result.beliefs) == 0, (
            "Pending belief should not appear in recall (v_trusted_current excludes pending)"
        )

        # Promote to trusted
        now = datetime.now(tz=timezone.utc)
        conn.execute(
            """
UPDATE belief SET status = 'trusted', trust_score = 0.9, screened_at = %s
WHERE belief_id = %s AND tenant_id = %s
""",
            (now, str(belief_id), str(tenant_id)),
        )
        conn.commit()

        with mock.patch("pqbs.recall.engine.embed_text", return_value=fake_embedding):
            result2 = engine.recall(request, conn)

        # Now it should appear
        assert len(result2.beliefs) >= 1, (
            "Trusted belief should appear in recall after status promotion"
        )


# ---------------------------------------------------------------------------
# test_recall_logs_retrieval_id
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_recall_logs_retrieval_id() -> None:
    """After recall, retrieval_log should have an entry for this tenant."""
    _skip_if_no_db()
    from pqbs.recall.engine import RecallEngine
    import unittest.mock as mock

    tenant_id = uuid.uuid4()
    engine = RecallEngine()

    with _get_conn() as conn:
        fake_embedding = tuple(0.0 for _ in range(1024))

        request = RecallRequest(
            query="retrieval log test query",
            tenant_id=tenant_id,
            limit=5,
        )
        with mock.patch("pqbs.recall.engine.embed_text", return_value=fake_embedding):
            result = engine.recall(request, conn)

        # Check retrieval_log has an entry
        rows = conn.execute(
            "SELECT retrieval_id, query_digest FROM retrieval_log WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchall()

        assert len(rows) >= 1, "Expected at least one retrieval_log entry"

        expected_digest = hashlib.sha256(request.query.encode()).hexdigest()
        digests = [r["query_digest"] for r in rows]
        assert expected_digest in digests, (
            f"Expected query_digest {expected_digest} in retrieval_log, got {digests}"
        )
        assert str(result.retrieval_id) in [str(r["retrieval_id"]) for r in rows], (
            "RecallResult.retrieval_id must match retrieval_log entry"
        )


# ---------------------------------------------------------------------------
# test_recall_temporal_filter
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_recall_temporal_filter() -> None:
    """
    Write belief with past valid_from; query with valid_at in that window → returns it.
    Query with valid_at before valid_from → returns 0.
    """
    _skip_if_no_db()
    from pqbs.recall.engine import RecallEngine
    import unittest.mock as mock

    tenant_id = uuid.uuid4()
    engine = RecallEngine()
    now = datetime.now(tz=timezone.utc)
    valid_from = now - timedelta(hours=10)
    valid_to = now + timedelta(hours=10)

    with _get_conn() as conn:
        _insert_belief(
            conn, tenant_id,
            subject="TemporalTestBob",
            status="trusted",
            trust_score=0.85,
            screened_at=now,
            valid_from=valid_from,
            valid_to=valid_to,
        )

        fake_embedding = tuple(0.0 for _ in range(1024))

        # Query with valid_at inside window
        tc_inside = TemporalContext(valid_at=now)
        request_inside = RecallRequest(
            query="TemporalTestBob at work",
            tenant_id=tenant_id,
            temporal_context=tc_inside,
            limit=10,
        )
        with mock.patch("pqbs.recall.engine.embed_text", return_value=fake_embedding):
            result_inside = engine.recall(request_inside, conn)

        assert len(result_inside.beliefs) >= 1, (
            "Belief should appear when valid_at is within valid_from..valid_to"
        )

        # Query with valid_at before valid_from → should not appear
        tc_before = TemporalContext(valid_at=valid_from - timedelta(hours=1))
        request_before = RecallRequest(
            query="TemporalTestBob at work",
            tenant_id=tenant_id,
            temporal_context=tc_before,
            limit=10,
        )
        with mock.patch("pqbs.recall.engine.embed_text", return_value=fake_embedding):
            result_before = engine.recall(request_before, conn)

        assert len(result_before.beliefs) == 0, (
            "Belief should not appear when valid_at is before valid_from"
        )


# ---------------------------------------------------------------------------
# test_role_consumer_cannot_read_quarantined
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_role_consumer_cannot_read_quarantined() -> None:
    """
    NOTE: This test is DOCUMENTED (not programmatically verified here) because
    connecting as role_consumer requires separate connection credentials.

    The structural enforcement is:
    - v_trusted_current excludes status='quarantined' and status='pending'
    - role_consumer has SELECT only on v_trusted_current (see migration 0012_views)
    - RecallEngine queries v_trusted_current, not the belief table directly

    Verification procedure (manual / CI with role_consumer credentials):
    1. Connect as role_consumer (COCKROACH_URL with role_consumer user)
    2. Run: SELECT * FROM belief WHERE status = 'quarantined'
    3. Expected: permission denied (role_consumer has no grant on belief table)
    4. Run: SELECT * FROM v_trusted_current WHERE tenant_id = '<uuid>'
    5. Expected: returns 0 rows for quarantined beliefs (view filters them)

    This verification is recorded in docs/VERIFICATIONS.md (Phase 6).
    """
    pytest.skip(
        "Role-consumer permission test requires dedicated role_consumer credentials. "
        "See docs/VERIFICATIONS.md — Phase 6 for the verification procedure."
    )
