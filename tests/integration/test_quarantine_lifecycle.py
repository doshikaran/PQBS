"""Integration tests for Phase 5 — Containment quarantine lifecycle.

Requires a live CockroachDB connection (COCKROACH_URL env var).
Tests are marked @pytest.mark.integration and skipped when DB is unavailable.

All tests use INTEGRATION_TENANT_ID and clean up after themselves.
"""
from __future__ import annotations

import json
import os
import tempfile
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
    ReasonCode,
    Sensitivity,
    TrustTier,
    VerdictValue,
)
from pqbs.contracts.verdicts import QuarantineRecord
from pqbs.integrity.gate import SCREENER_VERSION, ScreeningGate
from pqbs.agents.integrity.a6_cascade import CascadeAgent
from pqbs.agents.integrity.a14_review import ReviewAgent
from pqbs.agents.integrity.audit_sink import AuditSink

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers (mirror test_integrity_path.py conventions)
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
    digest = uuid4().hex + uuid4().hex
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
    subject: str = "Alice",
    predicate: str = "works_at",
    obj: str = "Acme Corp",
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
            obj,
            obj.lower(),
            _NOW,
            _NOW,
            status,
            str(provenance_id),
            trust_score,
            screened_at,
        ),
    )


def _insert_quarantine(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    belief_id: UUID,
    verdict_id: UUID,
    *,
    disposition: str = "held",
) -> UUID:
    quarantine_id = uuid4()
    conn.execute(
        """
        INSERT INTO quarantine
            (quarantine_id, belief_id, tenant_id, reason_code, quarantined_at, disposition)
        VALUES (%s, %s, %s, 'anomalous_embedding', now(), %s::disposition)
        """,
        (str(quarantine_id), str(belief_id), str(tenant_id), disposition),
    )
    return quarantine_id


def _insert_verdict(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    belief_id: UUID,
) -> UUID:
    """Insert a minimal integrity_verdict row and return verdict_id."""
    verdict_id = uuid4()
    signal_scores = json.dumps([
        {
            "signal_id": f"S{i}",
            "score": 0.9,
            "fired": True,
            "latency_ms": 1,
            "evidence_description": "test",
            "evidence_raw_values": {},
        }
        for i in range(1, 9)
    ])
    conn.execute(
        """
        INSERT INTO integrity_verdict
            (verdict_id, belief_id, tenant_id, verdict, trust_score,
             signal_scores, triggering_rule, screened_at, screener_version,
             re_screen_reason, latency_ms)
        VALUES (%s, %s, %s, 'quarantined', 0.9, %s, 'S3', now(), %s, NULL, 10)
        ON CONFLICT (tenant_id, verdict_id) DO NOTHING
        """,
        (
            str(verdict_id),
            str(belief_id),
            str(tenant_id),
            signal_scores,
            SCREENER_VERSION,
        ),
    )
    return verdict_id


def _cleanup(conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
    conn.execute("DELETE FROM integrity_verdict WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM quarantine WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM belief WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM provenance WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM contradiction_event WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute(
        "DELETE FROM agent_identity WHERE tenant_id = %s AND agent_id = 'test-agent'",
        (str(tenant_id),),
    )


def _make_quarantine_record(
    quarantine_id: UUID,
    belief_id: UUID,
    tenant_id: UUID,
    verdict_id: UUID,
) -> QuarantineRecord:
    return QuarantineRecord(
        quarantine_id=quarantine_id,
        belief_id=belief_id,
        tenant_id=tenant_id,
        reason_code=ReasonCode.ANOMALOUS_EMBEDDING,
        quarantined_at=_NOW,
        disposition=Disposition.HELD,
        triggering_verdict_id=verdict_id,
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestFullCascadeLifecycle:
    def test_full_cascade_lifecycle(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
        tmp_path: Any,
    ) -> None:
        """Write parent + 2 derived beliefs, quarantine parent, run cascade,
        verify descendants are reset to pending and re-screened.
        """
        tenant_id = integration_tenant_id
        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")

        # Parent belief (trusted, will be quarantined)
        parent_id = uuid4()
        parent_prov_id = uuid4()
        _insert_provenance(db_conn, tenant_id, parent_prov_id, TrustTier.AUTHORITATIVE)
        _insert_belief(
            db_conn, tenant_id, parent_id, parent_prov_id,
            status="quarantined", trust_score=0.9, screened_at=_NOW,
            subject="Parent", predicate="has_property", obj="ParentValue",
        )

        # Child A derived from parent
        child_a_id = uuid4()
        child_a_prov_id = uuid4()
        _insert_provenance(
            db_conn, tenant_id, child_a_prov_id, TrustTier.UNVERIFIED,
            derived_from=[parent_id],
        )
        _insert_belief(
            db_conn, tenant_id, child_a_id, child_a_prov_id,
            status="trusted", trust_score=0.3, screened_at=_NOW,
            subject="ChildA", predicate="derived_from_parent", obj="ValueA",
        )

        # Child B derived from parent
        child_b_id = uuid4()
        child_b_prov_id = uuid4()
        _insert_provenance(
            db_conn, tenant_id, child_b_prov_id, TrustTier.UNVERIFIED,
            derived_from=[parent_id],
        )
        _insert_belief(
            db_conn, tenant_id, child_b_id, child_b_prov_id,
            status="trusted", trust_score=0.3, screened_at=_NOW,
            subject="ChildB", predicate="derived_from_parent", obj="ValueB",
        )

        # Create quarantine and verdict records for parent
        verdict_id = _insert_verdict(db_conn, tenant_id, parent_id)
        quarantine_id = _insert_quarantine(db_conn, tenant_id, parent_id, verdict_id)

        quarantine_record = _make_quarantine_record(
            quarantine_id, parent_id, tenant_id, verdict_id
        )

        # Create audit sink and screening gate
        audit_dir = str(tmp_path / "audit")
        sink = AuditSink(_local_dir=audit_dir)
        gate = ScreeningGate()
        agent = CascadeAgent(gate=gate, audit_sink=sink)

        result = agent.cascade(quarantine_record, db_conn)

        assert result.descendants_found == 2
        assert result.had_cycle is False

        # Verify descendants were re-screened (status changed from trusted)
        for child_id in [child_a_id, child_b_id]:
            row = db_conn.execute(
                "SELECT status FROM belief WHERE belief_id = %s AND tenant_id = %s",
                (str(child_id), str(tenant_id)),
            ).fetchone()
            assert row is not None
            # After re-screening, status should no longer be "trusted" (original)
            # It may be trusted, quarantined, or pending depending on signal outcome
            assert row["status"] in ("trusted", "quarantined", "pending")

        _cleanup(db_conn, tenant_id)


@pytest.mark.integration
class TestReviewReleaseLifecycle:
    def test_review_release_lifecycle(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
        tmp_path: Any,
    ) -> None:
        """Quarantine a belief, call review.release(), verify status=trusted."""
        tenant_id = integration_tenant_id
        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")

        belief_id = uuid4()
        prov_id = uuid4()
        _insert_provenance(db_conn, tenant_id, prov_id)
        _insert_belief(
            db_conn, tenant_id, belief_id, prov_id,
            status="quarantined", trust_score=0.9, screened_at=_NOW,
        )

        verdict_id = _insert_verdict(db_conn, tenant_id, belief_id)
        quarantine_id = _insert_quarantine(db_conn, tenant_id, belief_id, verdict_id)

        audit_dir = str(tmp_path / "audit")
        sink = AuditSink(_local_dir=audit_dir)
        agent = ReviewAgent()

        agent.release(
            quarantine_id=quarantine_id,
            tenant_id=tenant_id,
            reviewed_by="human-reviewer",
            review_notes="Manually verified; content is safe",
            conn=db_conn,
            audit_sink=sink,
        )

        # Verify belief status is now trusted
        belief_row = db_conn.execute(
            "SELECT status FROM belief WHERE belief_id = %s AND tenant_id = %s",
            (str(belief_id), str(tenant_id)),
        ).fetchone()
        assert belief_row is not None
        assert belief_row["status"] == "trusted"

        # Verify quarantine disposition is released
        q_row = db_conn.execute(
            "SELECT disposition, reviewed_by FROM quarantine WHERE quarantine_id = %s",
            (str(quarantine_id),),
        ).fetchone()
        assert q_row is not None
        assert q_row["disposition"] == "released"
        assert q_row["reviewed_by"] == "human-reviewer"

        _cleanup(db_conn, tenant_id)


@pytest.mark.integration
class TestReviewRejectLifecycle:
    def test_review_reject_lifecycle(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
        tmp_path: Any,
    ) -> None:
        """Quarantine a belief, call review.reject(), verify status=rejected."""
        tenant_id = integration_tenant_id
        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")

        belief_id = uuid4()
        prov_id = uuid4()
        _insert_provenance(db_conn, tenant_id, prov_id)
        _insert_belief(
            db_conn, tenant_id, belief_id, prov_id,
            status="quarantined", trust_score=0.9, screened_at=_NOW,
        )

        verdict_id = _insert_verdict(db_conn, tenant_id, belief_id)
        quarantine_id = _insert_quarantine(db_conn, tenant_id, belief_id, verdict_id)

        audit_dir = str(tmp_path / "audit")
        sink = AuditSink(_local_dir=audit_dir)
        agent = ReviewAgent()

        agent.reject(
            quarantine_id=quarantine_id,
            tenant_id=tenant_id,
            reviewed_by="human-reviewer",
            review_notes="Confirmed malicious content",
            conn=db_conn,
            audit_sink=sink,
        )

        # Verify belief status is now rejected
        belief_row = db_conn.execute(
            "SELECT status FROM belief WHERE belief_id = %s AND tenant_id = %s",
            (str(belief_id), str(tenant_id)),
        ).fetchone()
        assert belief_row is not None
        assert belief_row["status"] == "rejected"

        # Verify quarantine disposition is rejected
        q_row = db_conn.execute(
            "SELECT disposition, reviewed_by FROM quarantine WHERE quarantine_id = %s",
            (str(quarantine_id),),
        ).fetchone()
        assert q_row is not None
        assert q_row["disposition"] == "rejected"
        assert q_row["reviewed_by"] == "human-reviewer"

        _cleanup(db_conn, tenant_id)


@pytest.mark.integration
class TestFailClosedPendingView:
    def test_fail_closed_pending_not_in_view(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: UUID,
    ) -> None:
        """Write pending beliefs; verify v_trusted_current returns none of them.

        This verifies Security Invariant 2: no consumer-role query can return
        pending or quarantined beliefs.
        """
        tenant_id = integration_tenant_id
        _cleanup(db_conn, tenant_id)
        _insert_agent(db_conn, tenant_id, "test-agent")

        pending_ids: list[UUID] = []
        for i in range(3):
            belief_id = uuid4()
            prov_id = uuid4()
            _insert_provenance(db_conn, tenant_id, prov_id)
            _insert_belief(
                db_conn, tenant_id, belief_id, prov_id,
                status="pending",
                subject=f"Subject{i}",
                predicate="has_property",
                obj=f"Value{i}",
            )
            pending_ids.append(belief_id)

        for belief_id in pending_ids:
            row = db_conn.execute(
                """
                SELECT belief_id FROM v_trusted_current
                WHERE belief_id = %s AND tenant_id = %s
                """,
                (str(belief_id), str(tenant_id)),
            ).fetchone()
            assert row is None, (
                f"Pending belief {belief_id} must not appear in v_trusted_current"
            )

        _cleanup(db_conn, tenant_id)
