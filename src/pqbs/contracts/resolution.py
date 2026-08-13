"""Resolution outcome contract — produced by A7, consumed by write-path commit and telemetry."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import Resolution, ResolutionBasis


class ResolutionOutcome(BaseModel):
    """
    Produced by A7, consumed by telemetry and the write-path commit.
    Records what the resolution agent decided and how many retries it needed.
    """

    model_config = CONTRACT_CONFIG

    belief_id: UUID               # the challenger that was evaluated
    tenant_id: UUID
    resolution: Resolution
    basis: ResolutionBasis
    incumbent_id: UUID | None     # None when no prior belief existed
    challenger_id: UUID           # == belief_id; explicit for clarity
    retry_count: int = Field(ge=0, description="Serializable retries before commit")
    detected_at: datetime
    contradiction_event_id: UUID | None = None  # FK to contradiction_event row if written
