"""CDC (Change Data Capture) contracts — the critical sync/async boundary between E1 and E2."""
from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import BeliefStatus, CdcOperation, Sensitivity


class BeliefSnapshot(BaseModel):
    """
    Immutable snapshot of a belief row as captured in the CDC event.
    Contains every field A4 needs to screen: content, provenance FK,
    author identity, sensitivity, and current status.
    """

    model_config = CONTRACT_CONFIG

    belief_id: UUID
    tenant_id: UUID
    subject: str
    predicate: str
    object: str
    object_normalized: str | None
    confidence: float
    valid_from: datetime
    valid_to: datetime | None
    tx_from: datetime
    tx_to: datetime | None
    status: BeliefStatus
    supersedes: UUID | None
    superseded_by: UUID | None
    author_agent_id: str
    provenance_id: UUID
    trust_score: float | None
    screened_at: datetime | None
    sensitivity: Sensitivity


class ChangeEvent(BaseModel):
    """
    Produced by CDC (E1 side), consumed by A4 (E2 side).

    CRITICAL: `after` is the full belief snapshot — not just the primary key.
    A4 requires the full content, provenance FK, author, and sensitivity
    to compute all 8 signals. Emitting only PKs here would stall Phase 4.

    `before` is None for INSERT operations.
    `after` is None for DELETE operations (rare — beliefs are never hard-deleted).
    """

    model_config = CONTRACT_CONFIG

    event_id: UUID = Field(default_factory=uuid4)
    belief_id: UUID
    tenant_id: UUID
    operation: CdcOperation
    before: BeliefSnapshot | None   # None for INSERT
    after: BeliefSnapshot | None    # None for DELETE
    commit_timestamp: datetime
    screener_version: str = Field(
        description="Version of the screening gate at event-capture time"
    )

    @model_validator(mode="after")
    def snapshots_consistent_with_operation(self) -> Self:
        if self.operation == CdcOperation.INSERT and self.before is not None:
            raise ValueError("INSERT events must have before=None")
        if self.operation == CdcOperation.DELETE and self.after is not None:
            raise ValueError("DELETE events must have after=None")
        if self.operation == CdcOperation.UPDATE and (
            self.before is None or self.after is None
        ):
            raise ValueError("UPDATE events must have both before and after")
        return self

    @property
    def current_snapshot(self) -> BeliefSnapshot:
        """The snapshot that reflects the current DB state after this event."""
        snap = self.after if self.after is not None else self.before
        if snap is None:
            raise RuntimeError("ChangeEvent has neither before nor after snapshot")
        return snap
