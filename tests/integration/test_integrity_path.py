"""Integration tests for the Phase 4 integrity path.

Requires a live CockroachDB connection (COCKROACH_URL env var).
Tests are marked @pytest.mark.integration and skipped when DB is unavailable.

All tests use the INTEGRATION_TENANT_ID and clean up after themselves.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from pqbs.contracts.cdc import BeliefSnapshot, ChangeEvent
from pqbs.contracts.enums import (
    BeliefStatus,
    CdcOperation,
    Disposition,
    Sensitivity,
    SourceType,
    TrustTier,
    VerdictValue,
)
from pqbs.integrity.gate import SCREENER_VERSION, ScreeningGate

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_agent(conn: psycopg.Connection[Any], tenant_id: UUID, agent_id: str) -> None:
    conn.execute(
        """
        INSERT INTO agent_identity (agent_id, tenant_id, agent_class, db_role, credential_ref, status, trust_multiplier)
        VALUES (%s, %s, 'integrity', 'role_integrity', 'test-credential', 'active', 1.0)
        ON CONFLICT (tenant_id, agent_id) DO NOTHING
        """,
        (agent_id, str(tenant_id)),
    )


def _insert_provenance(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    provenance_id: UUID,
    tier: TrustTier = TrustTier.AUTHORITATIVE,
    derived_from: list[UUID] | None = None,
) -> None:
    digest = uuid4().hex + uuid4().hex  # 64-char hex
    conn.execute(
        """
        INSERT INTO provenance
            (provenance_id, tenant_id, source_type, source_uri, source_digest,
             episode_id, ingested_at, source_trust_tier, ingestion_agent_id, derived_from)
        VALUES (%s, %s, 'user_statement', NULL, %s, %s, NOW(), %s, 'test-agent', %s)
        ON CONFLICT (tenant_id, provenance_id) DO NOTHING
        """,
        (
            str(provenance_id),
            str(tenant_id),
            digest,
            str(uuid4()),
            tier.value,
            json.dumps([str(d) for d in (derived_from or [])]),
        ),
    )


def _insert_belief(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    belief_id: UUID,
    provenance_id: UUID,
    *,
    status: str = "pending",
    trust_score: float | None = None,
    screened_at: datetime | None = None,
    object: str = "Acme Corp",
    predicate: str = "works_at",
    subject: str = "Alice",
) -> None:
    conn.execute(
        """
        INSERT INTO belief
            (belief_id, tenant_id, subject, predicate, object, object_normalized,
             confidence, valid_from, valid_to, tx_from, tx_to, status,
             supersedes, superseded_by, author_agent_id, provenance_id,
             trust_score, screened_at, sensitivity)
        VALUES (%s,%s,%s,%s,%s,%s, 0.9,%s,NULL,%s,NULL,%s::belief_status,
                NULL,NULL,'test-agent',%s, %s,%s,'normal')
        ON CONFLICT (tenant_id, belief_id) DO NOTHING
        """,
        (
            str(belief_id),
            str(tenant_id),
            subject,
            predicate,
            object,
            object.lower(),
            _NOW,
            _NOW,
            status,
            str(provenance_id),
            trust_score,
            screened_at,
        ),
    )


def _make_snapshot(
    belief_id: UUID,
    tenant_id: UUID,
    provenance_id: UUID,
    *,
    object: str = "Acme Corp",
    predicate: str = "works_at",
    subject: str = "Alice",
) -> BeliefSnapshot:
    return BeliefSnapshot(
        belief_id=belief_id,
        tenant_id=tenant_id,
        subject=subject,
        predicate=predicate,
        object=object,
        object_normalized=object.lower(),
        confidence=0.9,
        valid_from=_NOW,
        valid_to=None,
        tx_from=_NOW,
        tx_to=None,
        status=BeliefStatus.PENDING,
        supersedes=None,
        superseded_by=None,
        author_agent_id="test-agent",
        provenance_id=provenance_id,
        trust_score=None,
        screened_at=None,
        sensitivity=Sensitivity.NORMAL,
    )


def _make_event(snapshot: BeliefSnapshot) -> ChangeEvent:
    return ChangeEvent(
        belief_id=snapshot.belief_id,
        tenant_id=snapshot.tenant_id,
        operation=CdcOperation.INSERT,
        before=None,
        after=snapshot,
        commit_timestamp=snapshot.tx_from,
        screener_version=SCREENER_VERSION,
    )


def _cleanup(conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
    """Remove all test data for integration_tenant_id."""
    conn.execute("DELETE FROM integrity_verdict WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM quarantine WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM belief WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM provenance WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM contradiction_event WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute(
        "DELETE FROM agent_identity WHERE tenant_id = %s AND agent_id = 'test-agent'",
        (str(tenant_id),),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestScreenPendingBelief:
    def test_screen_pending_belief(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
    ) -> None:
        """Write a pending belief, screen it, verify status changes and verdict row exists."""
        tenant_id = integration_tenant_id
        belief_id = uuid4()
        provenance_id = uuid4()

        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")
        _insert_provenance(db_conn, tenant_id, provenance_id, TrustTier.AUTHORITATIVE)
        _insert_belief(db_conn, tenant_id, belief_id, provenance_id)

        snapshot = _make_snapshot(belief_id, tenant_id, provenance_id)
        event = _make_event(snapshot)
        gate = ScreeningGate()

        verdict = gate.screen(event, db_conn)

        # Verdict is well-formed
        assert verdict.belief_id == belief_id
        assert verdict.tenant_id == tenant_id
        assert verdict.verdict in (VerdictValue.TRUSTED, VerdictValue.INCONCLUSIVE, VerdictValue.QUARANTINED)
        assert len(verdict.signal_scores) == 8
        assert verdict.screener_version == SCREENER_VERSION

        # Verify verdict row in DB
        row = db_conn.execute(
            "SELECT * FROM integrity_verdict WHERE belief_id = %s AND tenant_id = %s",
            (str(belief_id), str(tenant_id)),
        ).fetchone()
        assert row is not None
        assert row["screener_version"] == SCREENER_VERSION

        # Verify belief status changed (no longer pending after screening)
        belief_row = db_conn.execute(
            "SELECT status, trust_score, screened_at FROM belief WHERE belief_id = %s AND tenant_id = %s",
            (str(belief_id), str(tenant_id)),
        ).fetchone()
        assert belief_row is not None
        assert belief_row["screened_at"] is not None
        assert belief_row["trust_score"] is not None
        # Status should be trusted, quarantined, or pending (inconclusive stays pending)
        assert belief_row["status"] in ("trusted", "quarantined", "pending")

        _cleanup(db_conn, tenant_id)


@pytest.mark.integration
class TestIdempotentScreening:
    def test_screen_twice_yields_one_verdict_row(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
    ) -> None:
        """Screening the same belief twice produces exactly one verdict row."""
        tenant_id = integration_tenant_id
        belief_id = uuid4()
        provenance_id = uuid4()

        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")
        _insert_provenance(db_conn, tenant_id, provenance_id, TrustTier.AUTHORITATIVE)
        _insert_belief(db_conn, tenant_id, belief_id, provenance_id)

        snapshot = _make_snapshot(belief_id, tenant_id, provenance_id)
        event = _make_event(snapshot)
        gate = ScreeningGate()

        verdict1 = gate.screen(event, db_conn)
        verdict2 = gate.screen(event, db_conn)

        # Both calls return the same verdict_id
        assert verdict1.verdict_id == verdict2.verdict_id

        # Only one row in integrity_verdict
        count_row = db_conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM integrity_verdict
            WHERE belief_id = %s AND tenant_id = %s AND screener_version = %s
            """,
            (str(belief_id), str(tenant_id), SCREENER_VERSION),
        ).fetchone()
        assert count_row is not None
        assert int(count_row["cnt"]) == 1

        _cleanup(db_conn, tenant_id)


@pytest.mark.integration
class TestS7QuarantinedParent:
    def test_derived_from_quarantined_parent_fires_s7(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
    ) -> None:
        """Write a quarantined parent, write derived belief, screen it → S7 fires."""
        tenant_id = integration_tenant_id
        parent_belief_id = uuid4()
        parent_provenance_id = uuid4()
        child_belief_id = uuid4()
        child_provenance_id = uuid4()

        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")

        # Parent provenance (no derivation)
        _insert_provenance(db_conn, tenant_id, parent_provenance_id, TrustTier.AUTHORITATIVE)
        # Write parent belief in quarantined state
        _insert_belief(
            db_conn, tenant_id, parent_belief_id, parent_provenance_id,
            status="quarantined",
            trust_score=0.9,
            screened_at=_NOW,
        )

        # Child provenance derived from parent belief
        _insert_provenance(
            db_conn, tenant_id, child_provenance_id, TrustTier.AUTHORITATIVE,
            derived_from=[parent_belief_id],
        )
        _insert_belief(db_conn, tenant_id, child_belief_id, child_provenance_id)

        snapshot = _make_snapshot(child_belief_id, tenant_id, child_provenance_id)
        event = _make_event(snapshot)
        gate = ScreeningGate()

        verdict = gate.screen(event, db_conn)

        # S7 should have fired
        s7_score = next(
            s for s in verdict.signal_scores
            if s.signal_id.value == "S7"
        )
        assert s7_score.fired is True
        assert s7_score.score == pytest.approx(1.0)

        _cleanup(db_conn, tenant_id)


@pytest.mark.integration
class TestFailClosed:
    def test_pending_belief_not_visible_via_trusted_view(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
    ) -> None:
        """
        A pending (unscreened) belief must not appear in v_trusted_current.
        This verifies the fail-closed property enforced by the DB view:
        only status='trusted' beliefs are visible to role_consumer.
        """
        tenant_id = integration_tenant_id
        belief_id = uuid4()
        provenance_id = uuid4()

        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")
        _insert_provenance(db_conn, tenant_id, provenance_id, TrustTier.UNVERIFIED)
        _insert_belief(
            db_conn, tenant_id, belief_id, provenance_id,
            status="pending",  # unscreened
        )

        # Pending belief is NOT visible in v_trusted_current
        row = db_conn.execute(
            "SELECT belief_id FROM v_trusted_current WHERE belief_id = %s AND tenant_id = %s",
            (str(belief_id), str(tenant_id)),
        ).fetchone()
        assert row is None, "Pending belief must not appear in v_trusted_current"

        # Write a trusted belief — it SHOULD appear
        trusted_belief_id = uuid4()
        trusted_prov_id = uuid4()
        _insert_provenance(db_conn, tenant_id, trusted_prov_id, TrustTier.AUTHORITATIVE)
        _insert_belief(
            db_conn, tenant_id, trusted_belief_id, trusted_prov_id,
            status="trusted",
            trust_score=0.1,
            screened_at=_NOW,
        )

        trusted_row = db_conn.execute(
            "SELECT belief_id FROM v_trusted_current WHERE belief_id = %s AND tenant_id = %s",
            (str(trusted_belief_id), str(tenant_id)),
        ).fetchone()
        assert trusted_row is not None, "Trusted belief must appear in v_trusted_current"

        _cleanup(db_conn, tenant_id)
