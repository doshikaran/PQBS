"""Audit record contract — produced by ALL agents for significant state transitions."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import AuditEventType


class AuditRecord(BaseModel):
    """
    Produced by ALL agents for significant state transitions, consumed by the WORM sink.
    WORM sink is append-only with retention lock — records cannot be deleted.
    The before/after fields capture the full state snapshot for forensic completeness.
    """

    model_config = CONTRACT_CONFIG

    audit_id: UUID = Field(default_factory=uuid4)
    event_type: AuditEventType
    agent_id: str = Field(min_length=1, description="Agent that caused this transition")
    tenant_id: UUID
    belief_id: UUID | None = Field(
        default=None,
        description="Subject belief, if applicable",
    )
    timestamp: datetime
    before: dict[str, str | float | bool | int | None] | None = Field(
        default=None,
        description="State before the transition; None for creation events",
    )
    after: dict[str, str | float | bool | int | None] | None = Field(
        default=None,
        description="State after the transition; None for deletion events (which should not occur)",
    )
    reason: str = Field(
        min_length=1,
        max_length=2048,
        description="Human-readable rationale",
    )
    verdict_id: UUID | None = Field(
        default=None,
        description="Associated verdict, if this event was triggered by screening",
    )
    checksum: str | None = Field(
        default=None,
        description="SHA-256 of the serialized record body, computed by the WORM sink for tamper detection",
    )
