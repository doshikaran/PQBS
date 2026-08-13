"""A14 ReviewAgent — disposition for quarantined and inconclusive beliefs.

Phase 5, E3 — Containment.

Security constraints:
- Release requires non-empty reviewed_by. No autonomous release.
- Once rejected, a belief cannot be re-released (disposition is terminal).
- Every disposition change emits an audit record to the WORM sink.

Security Invariant 7: This agent updates belief status; it does NOT issue verdicts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.contracts import AuditRecord, AuditEventType
from pqbs.contracts.enums import Disposition
from pqbs.contracts.exceptions import QuarantineError
from pqbs.agents.integrity.audit_sink import AuditSink, emit_or_block

logger = structlog.get_logger(__name__)


class ReviewAgent:
    """A14 — Review disposition for quarantined and inconclusive beliefs.

    All disposition changes are permanent and audited.
    No autonomous release is permitted — reviewed_by is mandatory.
    """

    def list_pending_review(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> list[dict[str, Any]]:
        """Return quarantined (held) beliefs with their verdict evidence.

        Rows are ordered by quarantined_at DESC (most recent first).
        """
        rows = conn.execute(
            """
            SELECT q.quarantine_id, q.belief_id, q.reason_code, q.quarantined_at,
                   b.subject, b.predicate, b.object, b.author_agent_id,
                   iv.verdict, iv.trust_score, iv.signal_scores, iv.screened_at
            FROM quarantine q
            JOIN belief b ON b.belief_id = q.belief_id AND b.tenant_id = q.tenant_id
            LEFT JOIN integrity_verdict iv
              ON iv.belief_id = q.belief_id AND iv.tenant_id = q.tenant_id
            WHERE q.tenant_id = %s AND q.disposition = 'held'
            ORDER BY q.quarantined_at DESC
            """,
            (str(tenant_id),),
        ).fetchall()

        return [dict(r) for r in rows]

    def release(
        self,
        quarantine_id: UUID,
        tenant_id: UUID,
        reviewed_by: str,
        review_notes: str,
        conn: psycopg.Connection[Any],
        audit_sink: AuditSink,
    ) -> None:
        """Release a quarantined belief back to 'trusted' status.

        Args:
            quarantine_id: The quarantine record to resolve.
            tenant_id: Tenant context.
            reviewed_by: Identity of the human reviewer (must be non-empty).
            review_notes: Justification for the release.
            conn: Open psycopg3 connection.
            audit_sink: WORM audit sink for recording the disposition.

        Raises:
            ValueError: If reviewed_by is empty.
            QuarantineError: If quarantine record not found or not in HELD state.
            AuditSinkError: If audit emission fails (fail-closed).
        """
        if not reviewed_by or not reviewed_by.strip():
            raise ValueError(
                "reviewed_by is required for release — no autonomous release allowed"
            )

        row = self._fetch_quarantine(quarantine_id, tenant_id, conn)

        if row["disposition"] != Disposition.HELD.value:
            raise QuarantineError(
                f"Cannot release: disposition is already '{row['disposition']}'"
            )

        belief_id = UUID(str(row["belief_id"]))
        now = datetime.now(tz=timezone.utc)

        # Update quarantine record
        conn.execute(
            """
            UPDATE quarantine
            SET disposition = 'released',
                reviewed_by = %s,
                review_notes = %s,
                reviewed_at = now()
            WHERE quarantine_id = %s AND tenant_id = %s
            """,
            (reviewed_by, review_notes, str(quarantine_id), str(tenant_id)),
        )

        # Update belief status from quarantined → trusted
        conn.execute(
            """
            UPDATE belief
            SET status = 'trusted', screened_at = %s
            WHERE belief_id = %s AND tenant_id = %s AND status = 'quarantined'
            """,
            (now, str(belief_id), str(tenant_id)),
        )

        logger.info(
            "belief_released",
            quarantine_id=str(quarantine_id),
            belief_id=str(belief_id),
            tenant_id=str(tenant_id),
            reviewed_by=reviewed_by,
        )

        # Emit audit record — fail-closed; if this raises the caller aborts
        audit_record = AuditRecord(
            event_type=AuditEventType.BELIEF_RELEASED,
            agent_id=reviewed_by,
            tenant_id=tenant_id,
            belief_id=belief_id,
            timestamp=now,
            before={"status": "quarantined", "disposition": "held"},
            after={
                "status": "trusted",
                "disposition": "released",
                "reviewed_by": reviewed_by,
            },
            reason=review_notes or "Manual release by reviewer",
        )
        emit_or_block(audit_sink, audit_record)

    def reject(
        self,
        quarantine_id: UUID,
        tenant_id: UUID,
        reviewed_by: str,
        review_notes: str,
        conn: psycopg.Connection[Any],
        audit_sink: AuditSink,
    ) -> None:
        """Permanently reject a quarantined belief.

        Args:
            quarantine_id: The quarantine record to resolve.
            tenant_id: Tenant context.
            reviewed_by: Identity of the human reviewer (must be non-empty).
            review_notes: Justification for the rejection.
            conn: Open psycopg3 connection.
            audit_sink: WORM audit sink for recording the disposition.

        Raises:
            ValueError: If reviewed_by is empty.
            QuarantineError: If quarantine record not found or not in HELD state.
            AuditSinkError: If audit emission fails (fail-closed).
        """
        if not reviewed_by or not reviewed_by.strip():
            raise ValueError(
                "reviewed_by is required for reject — no autonomous rejection allowed"
            )

        row = self._fetch_quarantine(quarantine_id, tenant_id, conn)

        if row["disposition"] != Disposition.HELD.value:
            raise QuarantineError(
                f"Cannot reject: disposition is already '{row['disposition']}'"
            )

        belief_id = UUID(str(row["belief_id"]))
        now = datetime.now(tz=timezone.utc)

        # Update quarantine record
        conn.execute(
            """
            UPDATE quarantine
            SET disposition = 'rejected',
                reviewed_by = %s,
                review_notes = %s,
                reviewed_at = now()
            WHERE quarantine_id = %s AND tenant_id = %s
            """,
            (reviewed_by, review_notes, str(quarantine_id), str(tenant_id)),
        )

        # Update belief status from quarantined → rejected
        conn.execute(
            """
            UPDATE belief
            SET status = 'rejected'
            WHERE belief_id = %s AND tenant_id = %s AND status = 'quarantined'
            """,
            (str(belief_id), str(tenant_id)),
        )

        logger.info(
            "belief_rejected",
            quarantine_id=str(quarantine_id),
            belief_id=str(belief_id),
            tenant_id=str(tenant_id),
            reviewed_by=reviewed_by,
        )

        # Emit audit record — fail-closed
        audit_record = AuditRecord(
            event_type=AuditEventType.BELIEF_REJECTED,
            agent_id=reviewed_by,
            tenant_id=tenant_id,
            belief_id=belief_id,
            timestamp=now,
            before={"status": "quarantined", "disposition": "held"},
            after={
                "status": "rejected",
                "disposition": "rejected",
                "reviewed_by": reviewed_by,
            },
            reason=review_notes or "Manual rejection by reviewer",
        )
        emit_or_block(audit_sink, audit_record)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_quarantine(
        self,
        quarantine_id: UUID,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> dict[str, Any]:
        """Fetch quarantine row or raise QuarantineError."""
        row = conn.execute(
            """
            SELECT quarantine_id, belief_id, disposition
            FROM quarantine
            WHERE quarantine_id = %s AND tenant_id = %s
            """,
            (str(quarantine_id), str(tenant_id)),
        ).fetchone()

        if row is None:
            raise QuarantineError(
                f"Quarantine record {quarantine_id} not found for tenant {tenant_id}"
            )
        return dict(row)
