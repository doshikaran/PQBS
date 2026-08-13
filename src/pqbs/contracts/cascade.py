"""Cascade contract — triggers re-screening of beliefs derived from a quarantined root."""
from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from pqbs.contracts.base import CONTRACT_CONFIG


class CascadeRequest(BaseModel):
    """
    Produced by A4/A6, consumed by A6 (recursive) and the screening queue.
    Triggers re-screening of all beliefs derived from a quarantined root.
    """

    model_config = CONTRACT_CONFIG

    request_id: UUID = Field(default_factory=uuid4)
    root_quarantine_id: UUID
    belief_ids: tuple[UUID, ...] = Field(
        min_length=1,
        description="IDs to re-screen in this batch",
    )
    reason: str = Field(min_length=1, description="Why cascade was triggered")
    depth: int = Field(
        ge=0,
        description="Traversal depth from quarantine root; depth 0 = direct children",
    )
    max_depth: int = Field(default=20, description="Safety limit to prevent unbounded traversal")
    initiated_at: datetime
    tenant_id: UUID

    @model_validator(mode="after")
    def depth_within_limit(self) -> Self:
        if self.depth > self.max_depth:
            raise ValueError(
                f"depth {self.depth} exceeds max_depth {self.max_depth}"
            )
        return self
