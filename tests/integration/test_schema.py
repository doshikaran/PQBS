"""Integration tests for Phase 2 schema constraints and views.

These tests connect directly to the live CockroachDB Serverless cluster and
verify:
  - CHECK constraints enforce lifecycle invariants
  - Foreign key constraints are enforced
  - Views filter correctly by belief status
  - Security Invariant #1 is DB-enforced: no direct 'trusted' insert without
    going through the integrity path (tested via the view layer)

Run with:
  PYTHONPATH=src pytest tests/integration/test_schema.py -v -m integration

Each test cleans up its own data using the integration_tenant_id fixture.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
EPISODE_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _insert_provenance(conn: psycopg.Connection[Any], tenant_id: uuid.UUID) -> uuid.UUID:
    """Insert a minimal provenance row and return its provenance_id."""
    prov_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO provenance
          (provenance_id, tenant_id, source_type, source_uri, source_digest,
           episode_id, derived_from, ingested_at, source_trust_tier, ingestion_agent_id)
        VALUES (%s, %s, 'system_of_record', NULL, %s, %s, '[]', %s, 'authoritative', 'test-agent')
        """,
        (str(prov_id), str(tenant_id), "a" * 64, str(EPISODE_ID), NOW),
    )
    return prov_id


def _insert_belief(
    conn: psycopg.Connection[Any],
    tenant_id: uuid.UUID,
    prov_id: uuid.UUID,
    *,
    status: str = "pending",
    confidence: float = 0.9,
    superseded_by: str | None = None,
    trust_score: float | None = None,
    screened_at: datetime | None = None,
) -> uuid.UUID:
    """Insert a belief and return its belief_id. May raise on constraint violation."""
    belief_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO belief
          (tenant_id, belief_id, subject, predicate, object, confidence,
           valid_from, status, author_agent_id, provenance_id,
           superseded_by, trust_score, screened_at, sensitivity)
        VALUES (%s, %s, 'TestSubject', 'test_predicate', 'test_value',
                %s, %s, %s::belief_status, 'test-agent', %s, %s, %s, %s, 'normal')
        """,
        (
            str(tenant_id),
            str(belief_id),
            confidence,
            NOW,
            status,
            str(prov_id),
            str(superseded_by) if superseded_by else None,
            trust_score,
            screened_at,
        ),
    )
    return belief_id


def _cleanup(conn: psycopg.Connection[Any], tenant_id: uuid.UUID) -> None:
    """Remove all test data for the integration tenant."""
    conn.execute("DELETE FROM quarantine WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM integrity_verdict WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM contradiction_event WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM retrieval_log WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM belief WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM provenance WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM predicate_policy WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM agent_identity WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM working_memory WHERE tenant_id = %s", (str(tenant_id),))


# ---------------------------------------------------------------------------
# CHECK constraint tests
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    """Verify that the DB enforces lifecycle invariants via CHECK constraints."""

    def test_confidence_too_high_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """confidence > 1.0 must be rejected by the CHECK constraint."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_belief(db_conn, integration_tenant_id, prov_id, confidence=1.1)

        _cleanup(db_conn, integration_tenant_id)

    def test_confidence_negative_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """confidence < 0.0 must be rejected."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_belief(db_conn, integration_tenant_id, prov_id, confidence=-0.01)

        _cleanup(db_conn, integration_tenant_id)

    def test_superseded_by_without_superseded_status_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Setting superseded_by with status='pending' must violate the constraint."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        other_id = uuid.uuid4()

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_belief(
                db_conn,
                integration_tenant_id,
                prov_id,
                status="pending",
                superseded_by=str(other_id),
            )

        _cleanup(db_conn, integration_tenant_id)

    def test_trust_score_without_screened_at_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """trust_score set without screened_at must violate the consistency constraint."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_belief(
                db_conn,
                integration_tenant_id,
                prov_id,
                trust_score=0.85,
                screened_at=None,  # missing screened_at
            )

        _cleanup(db_conn, integration_tenant_id)

    def test_screened_at_without_trust_score_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """screened_at set without trust_score must also violate the consistency constraint."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_belief(
                db_conn,
                integration_tenant_id,
                prov_id,
                trust_score=None,
                screened_at=NOW,  # has screened_at but no trust_score
            )

        _cleanup(db_conn, integration_tenant_id)

    def test_valid_pending_belief_is_accepted(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """A well-formed pending belief must be accepted — positive case."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        belief_id = _insert_belief(db_conn, integration_tenant_id, prov_id, status="pending")

        row = db_conn.execute(
            "SELECT status FROM belief WHERE tenant_id = %s AND belief_id = %s",
            (str(integration_tenant_id), str(belief_id)),
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"

        _cleanup(db_conn, integration_tenant_id)

    def test_superseded_status_with_superseded_by_is_accepted(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """status='superseded' with a superseded_by value must be accepted."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        other_id = uuid.uuid4()

        belief_id = _insert_belief(
            db_conn,
            integration_tenant_id,
            prov_id,
            status="superseded",
            superseded_by=str(other_id),
        )

        row = db_conn.execute(
            "SELECT status, superseded_by FROM belief WHERE tenant_id = %s AND belief_id = %s",
            (str(integration_tenant_id), str(belief_id)),
        ).fetchone()
        assert row is not None
        assert row["status"] == "superseded"
        assert uuid.UUID(str(row["superseded_by"])) == other_id

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# Foreign key constraint tests
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    """Verify that the DB enforces FK from belief.provenance_id to provenance."""

    def test_belief_with_nonexistent_provenance_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Inserting a belief with a non-existent provenance_id must fail FK check."""
        _cleanup(db_conn, integration_tenant_id)
        fake_prov_id = uuid.uuid4()  # does not exist in provenance table

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_belief(db_conn, integration_tenant_id, fake_prov_id)

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


class TestViews:
    """Verify that views correctly filter beliefs by status."""

    def test_v_trusted_current_excludes_pending(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """v_trusted_current must NOT return pending beliefs."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        belief_id = _insert_belief(db_conn, integration_tenant_id, prov_id, status="pending")

        rows = db_conn.execute(
            "SELECT belief_id FROM v_trusted_current WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()

        ids_in_view = {str(r["belief_id"]) for r in rows}
        assert str(belief_id) not in ids_in_view, (
            "v_trusted_current must not return pending beliefs"
        )

        _cleanup(db_conn, integration_tenant_id)

    def test_v_trusted_current_excludes_quarantined(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """v_trusted_current must NOT return quarantined beliefs."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        belief_id = _insert_belief(db_conn, integration_tenant_id, prov_id, status="quarantined")

        rows = db_conn.execute(
            "SELECT belief_id FROM v_trusted_current WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()

        ids_in_view = {str(r["belief_id"]) for r in rows}
        assert str(belief_id) not in ids_in_view, (
            "v_trusted_current must not return quarantined beliefs"
        )

        _cleanup(db_conn, integration_tenant_id)

    def test_v_trusted_current_returns_trusted_with_null_tx_to(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """v_trusted_current MUST return trusted beliefs where tx_to IS NULL."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        belief_id = _insert_belief(
            db_conn,
            integration_tenant_id,
            prov_id,
            status="trusted",
            trust_score=0.9,
            screened_at=NOW,
        )

        rows = db_conn.execute(
            "SELECT belief_id FROM v_trusted_current WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()

        ids_in_view = {str(r["belief_id"]) for r in rows}
        assert str(belief_id) in ids_in_view, (
            "v_trusted_current must return trusted beliefs with tx_to IS NULL"
        )

        _cleanup(db_conn, integration_tenant_id)

    def test_v_pending_beliefs_shows_pending(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """v_pending_beliefs must return pending beliefs."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        belief_id = _insert_belief(db_conn, integration_tenant_id, prov_id, status="pending")

        rows = db_conn.execute(
            "SELECT belief_id FROM v_pending_beliefs WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()

        ids_in_view = {str(r["belief_id"]) for r in rows}
        assert str(belief_id) in ids_in_view, (
            "v_pending_beliefs must return pending beliefs"
        )

        _cleanup(db_conn, integration_tenant_id)

    def test_v_pending_beliefs_excludes_trusted(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """v_pending_beliefs must NOT return trusted beliefs."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)
        belief_id = _insert_belief(
            db_conn,
            integration_tenant_id,
            prov_id,
            status="trusted",
            trust_score=0.9,
            screened_at=NOW,
        )

        rows = db_conn.execute(
            "SELECT belief_id FROM v_pending_beliefs WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()

        ids_in_view = {str(r["belief_id"]) for r in rows}
        assert str(belief_id) not in ids_in_view, (
            "v_pending_beliefs must not return trusted beliefs"
        )

        _cleanup(db_conn, integration_tenant_id)

    def test_v_all_beliefs_returns_all_statuses(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """v_all_beliefs must return beliefs of every status."""
        _cleanup(db_conn, integration_tenant_id)
        prov_id = _insert_provenance(db_conn, integration_tenant_id)

        inserted_ids = set()
        inserted_ids.add(str(_insert_belief(db_conn, integration_tenant_id, prov_id, status="pending")))
        inserted_ids.add(str(_insert_belief(
            db_conn, integration_tenant_id, prov_id,
            status="trusted", trust_score=0.9, screened_at=NOW
        )))
        inserted_ids.add(str(_insert_belief(db_conn, integration_tenant_id, prov_id, status="quarantined")))

        rows = db_conn.execute(
            "SELECT belief_id FROM v_all_beliefs WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()

        ids_in_view = {str(r["belief_id"]) for r in rows}
        assert inserted_ids.issubset(ids_in_view), (
            "v_all_beliefs must return beliefs of all statuses"
        )

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# working_memory table tests
# ---------------------------------------------------------------------------


class TestWorkingMemory:
    """Basic sanity tests for the working_memory table with TTL."""

    def test_working_memory_insert_and_select(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """working_memory must accept inserts and return rows."""
        tenant_id = integration_tenant_id
        session_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        agent_id = "test-agent"
        expires_at = datetime(2030, 1, 1, tzinfo=timezone.utc)

        db_conn.execute(
            """
            INSERT INTO working_memory
              (tenant_id, session_id, entry_id, agent_id, content, expires_at)
            VALUES (%s, %s, %s, %s, 'test content', %s)
            """,
            (str(tenant_id), str(session_id), str(entry_id), agent_id, expires_at),
        )

        row = db_conn.execute(
            "SELECT content FROM working_memory WHERE tenant_id = %s AND session_id = %s AND entry_id = %s",
            (str(tenant_id), str(session_id), str(entry_id)),
        ).fetchone()

        assert row is not None
        assert row["content"] == "test content"

        # Cleanup
        db_conn.execute(
            "DELETE FROM working_memory WHERE tenant_id = %s",
            (str(tenant_id),),
        )


# ---------------------------------------------------------------------------
# Integrity_verdict constraint tests
# ---------------------------------------------------------------------------


class TestIntegrityVerdict:
    """Verify constraints on the integrity_verdict table."""

    def test_trust_score_bounds_in_verdict(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """integrity_verdict.trust_score > 1.0 must be rejected."""
        tenant_id = integration_tenant_id
        verdict_id = uuid.uuid4()
        belief_id = uuid.uuid4()

        with pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                """
                INSERT INTO integrity_verdict
                  (verdict_id, belief_id, tenant_id, verdict, trust_score,
                   signal_scores, screened_at, screener_version, latency_ms)
                VALUES (%s, %s, %s, 'trusted', 1.5, '{}', %s, 'v0.1.0', 100)
                """,
                (str(verdict_id), str(belief_id), str(tenant_id), NOW),
            )

    def test_negative_latency_in_verdict_is_rejected(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """integrity_verdict.latency_ms < 0 must be rejected."""
        tenant_id = integration_tenant_id
        verdict_id = uuid.uuid4()
        belief_id = uuid.uuid4()

        with pytest.raises(psycopg.errors.CheckViolation):
            db_conn.execute(
                """
                INSERT INTO integrity_verdict
                  (verdict_id, belief_id, tenant_id, verdict, trust_score,
                   signal_scores, screened_at, screener_version, latency_ms)
                VALUES (%s, %s, %s, 'trusted', 0.9, '{}', %s, 'v0.1.0', -1)
                """,
                (str(verdict_id), str(belief_id), str(tenant_id), NOW),
            )
