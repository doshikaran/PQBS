"""A6 CascadeAgent — re-screens all beliefs derived from a quarantined root.

Phase 5, E3 — Containment.

Security Invariant 7: This agent does not write beliefs autonomously; it
re-screens (updates status to pending, then calls A4 inline) and writes
quarantine/audit records only.

Traversal: BFS through derived_from provenance graph, cycle-safe via visited set.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.contracts import (
    AuditRecord,
    BeliefSnapshot,
    ChangeEvent,
    QuarantineRecord,
    AuditEventType,
    BeliefStatus,
    CdcOperation,
    Sensitivity,
)
from pqbs.integrity.gate import ScreeningGate, SCREENER_VERSION
from pqbs.agents.integrity.audit_sink import AuditSink, emit_or_block

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CascadeResult:
    """Outcome of a cascade traversal."""
    root_belief_id: UUID
    tenant_id: UUID
    descendants_found: int
    descendants_rescreened: int
    max_depth: int
    had_cycle: bool
    cascade_duration_ms: int


# ---------------------------------------------------------------------------
# CascadeAgent
# ---------------------------------------------------------------------------

class CascadeAgent:
    """A6 — Cascade traversal agent.

    Re-screens all beliefs derived (directly or transitively) from a
    quarantined root belief. Uses BFS with a visited set for cycle safety.

    Idempotency: beliefs already in 'pending' status are skipped by the
    UPDATE's WHERE clause (status != 'pending'), so replaying the cascade
    is harmless.
    """

    def __init__(self, gate: ScreeningGate, audit_sink: AuditSink) -> None:
        self.gate = gate
        self.audit_sink = audit_sink

    def cascade(
        self,
        quarantine_record: QuarantineRecord,
        conn: psycopg.Connection[Any],
    ) -> CascadeResult:
        """Traverse derived_from graph from quarantined belief, re-screen all descendants.

        Args:
            quarantine_record: The quarantine event that triggered the cascade.
            conn: Open psycopg3 connection (autocommit mode recommended for
                  per-belief commits; caller controls transaction boundary).

        Returns:
            CascadeResult with traversal statistics.

        Raises:
            QuarantineError: If the quarantine record does not exist in the DB.
            AuditSinkError: If audit emission fails (fail-closed).
        """
        t_start = time.perf_counter()
        root_belief_id = quarantine_record.belief_id
        tenant_id = quarantine_record.tenant_id

        logger.info(
            "cascade_initiated",
            root_belief_id=str(root_belief_id),
            tenant_id=str(tenant_id),
            quarantine_id=str(quarantine_record.quarantine_id),
        )

        # Emit CASCADE_INITIATED audit record
        initiation_record = AuditRecord(
            event_type=AuditEventType.CASCADE_INITIATED,
            agent_id="a6_cascade",
            tenant_id=tenant_id,
            belief_id=root_belief_id,
            timestamp=datetime.now(tz=timezone.utc),
            before=None,
            after={
                "quarantine_id": str(quarantine_record.quarantine_id),
                "reason_code": quarantine_record.reason_code.value,
            },
            reason=(
                f"Cascade triggered by quarantine of belief {root_belief_id}; "
                f"reason: {quarantine_record.reason_code.value}"
            ),
            verdict_id=quarantine_record.triggering_verdict_id,
        )
        emit_or_block(self.audit_sink, initiation_record)

        # BFS traversal
        descendants, max_depth, had_cycle = self._find_descendants(
            root_belief_id, tenant_id, conn
        )

        descendants_rescreened = 0
        for desc_id in descendants:
            rescreened = self._reset_and_rescreen(desc_id, tenant_id, conn)
            if rescreened:
                descendants_rescreened += 1

        elapsed_ms = int((time.perf_counter() - t_start) * 1000)
        result = CascadeResult(
            root_belief_id=root_belief_id,
            tenant_id=tenant_id,
            descendants_found=len(descendants),
            descendants_rescreened=descendants_rescreened,
            max_depth=max_depth,
            had_cycle=had_cycle,
            cascade_duration_ms=elapsed_ms,
        )

        # Emit CASCADE_COMPLETED audit record
        completion_record = AuditRecord(
            event_type=AuditEventType.CASCADE_COMPLETED,
            agent_id="a6_cascade",
            tenant_id=tenant_id,
            belief_id=root_belief_id,
            timestamp=datetime.now(tz=timezone.utc),
            before={
                "quarantine_id": str(quarantine_record.quarantine_id),
            },
            after={
                "descendants_found": result.descendants_found,
                "descendants_rescreened": result.descendants_rescreened,
                "max_depth": result.max_depth,
                "had_cycle": result.had_cycle,
                "duration_ms": result.cascade_duration_ms,
            },
            reason=(
                f"Cascade completed: {result.descendants_rescreened} of "
                f"{result.descendants_found} descendants re-screened "
                f"(max_depth={result.max_depth}, had_cycle={result.had_cycle})"
            ),
            verdict_id=quarantine_record.triggering_verdict_id,
        )
        emit_or_block(self.audit_sink, completion_record)

        logger.info(
            "cascade_completed",
            root_belief_id=str(root_belief_id),
            descendants_found=result.descendants_found,
            descendants_rescreened=result.descendants_rescreened,
            max_depth=result.max_depth,
            had_cycle=result.had_cycle,
            duration_ms=elapsed_ms,
        )

        return result

    # ------------------------------------------------------------------
    # BFS graph traversal
    # ------------------------------------------------------------------

    def _find_descendants(
        self,
        root_belief_id: UUID,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> tuple[set[UUID], int, bool]:
        """BFS through derived_from provenance graph.

        Returns:
            (descendant_ids, max_depth_reached, had_cycle)
            descendant_ids excludes root_belief_id.
        """
        visited: set[UUID] = set()
        queue: deque[tuple[UUID, int]] = deque([(root_belief_id, 0)])
        max_depth = 0
        had_cycle = False

        while queue:
            current_id, depth = queue.popleft()
            if current_id in visited:
                # Already processed — indicates a cycle or diamond convergence
                had_cycle = True
                continue
            visited.add(current_id)
            max_depth = max(max_depth, depth)

            # Find beliefs whose provenance.derived_from contains current_id
            rows = conn.execute(
                """
                SELECT b.belief_id
                FROM belief b
                JOIN provenance p
                  ON p.provenance_id = b.provenance_id
                  AND p.tenant_id = b.tenant_id
                WHERE b.tenant_id = %s
                  AND p.derived_from @> %s::jsonb
                """,
                (str(tenant_id), json.dumps([str(current_id)])),
            ).fetchall()

            for row in rows:
                child_id = UUID(str(row["belief_id"]))
                if child_id in visited:
                    # Back-edge: this child was already visited → cycle detected
                    had_cycle = True
                else:
                    queue.append((child_id, depth + 1))

        # Return descendants only (exclude root)
        return visited - {root_belief_id}, max_depth, had_cycle

    # ------------------------------------------------------------------
    # Reset and re-screen a single descendant
    # ------------------------------------------------------------------

    def _reset_and_rescreen(
        self,
        desc_id: UUID,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> bool:
        """Reset belief to pending and re-screen it inline.

        Returns True if the belief was found and the re-screen was attempted.
        """
        # Read full belief snapshot (needed for ChangeEvent)
        row = conn.execute(
            """
            SELECT b.belief_id, b.tenant_id, b.subject, b.predicate, b.object,
                   b.object_normalized, b.confidence, b.valid_from, b.valid_to,
                   b.tx_from, b.tx_to, b.status, b.supersedes, b.superseded_by,
                   b.author_agent_id, b.provenance_id, b.trust_score,
                   b.screened_at, b.sensitivity
            FROM belief b
            WHERE b.belief_id = %s AND b.tenant_id = %s
            """,
            (str(desc_id), str(tenant_id)),
        ).fetchone()

        if row is None:
            logger.warning(
                "cascade_descendant_not_found",
                belief_id=str(desc_id),
                tenant_id=str(tenant_id),
            )
            return False

        # Reset to pending — idempotent: only updates if not already pending
        conn.execute(
            """
            UPDATE belief
            SET status = 'pending', screened_at = NULL, trust_score = NULL
            WHERE belief_id = %s AND tenant_id = %s AND status != 'pending'
            """,
            (str(desc_id), str(tenant_id)),
        )

        # Build snapshot from refreshed (now pending) state
        sensitivity_val = row.get("sensitivity") or "normal"
        try:
            sensitivity = Sensitivity(sensitivity_val)
        except ValueError:
            sensitivity = Sensitivity.NORMAL

        snapshot = BeliefSnapshot(
            belief_id=UUID(str(row["belief_id"])),
            tenant_id=UUID(str(row["tenant_id"])),
            subject=str(row["subject"]),
            predicate=str(row["predicate"]),
            object=str(row["object"]),
            object_normalized=row.get("object_normalized"),
            confidence=float(row["confidence"]),
            valid_from=row["valid_from"],
            valid_to=row.get("valid_to"),
            tx_from=row["tx_from"],
            tx_to=row.get("tx_to"),
            status=BeliefStatus.PENDING,
            supersedes=(
                UUID(str(row["supersedes"])) if row.get("supersedes") else None
            ),
            superseded_by=(
                UUID(str(row["superseded_by"])) if row.get("superseded_by") else None
            ),
            author_agent_id=str(row["author_agent_id"]),
            provenance_id=UUID(str(row["provenance_id"])),
            trust_score=None,
            screened_at=None,
            sensitivity=sensitivity,
        )

        # Use INSERT operation so before=None is valid — the cascade resets the
        # belief to pending (effectively re-enqueuing it as if newly inserted).
        change_event = ChangeEvent(
            belief_id=snapshot.belief_id,
            tenant_id=snapshot.tenant_id,
            operation=CdcOperation.INSERT,
            before=None,
            after=snapshot,
            commit_timestamp=datetime.now(tz=timezone.utc),
            screener_version=SCREENER_VERSION,
        )

        try:
            self.gate.screen(change_event, conn)
            logger.info(
                "cascade_descendant_rescreened",
                belief_id=str(desc_id),
                tenant_id=str(tenant_id),
            )
        except Exception as exc:
            logger.error(
                "cascade_descendant_rescreen_failed",
                belief_id=str(desc_id),
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            # Continue with remaining descendants; don't abort the cascade.

        return True
