"""Temporal query contract — produced by caller, consumed by A10 (audit agent)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import TemporalMechanism


class TemporalQuery(BaseModel):
    """
    Produced by the caller, consumed by A10.
    A10 is the audit agent — has elevated read authority including quarantined content.
    mechanism selects which reconstruction path to use.
    """

    model_config = CONTRACT_CONFIG

    query_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    as_of: datetime = Field(description="Point in time to reconstruct")
    mechanism: TemporalMechanism = Field(description="Which reconstruction path to use")
    requesting_agent_id: str = Field(
        min_length=1,
        description="Must have audit-tier role",
    )
    subject_filter: str | None = Field(
        default=None,
        description="Optional: restrict to beliefs about this subject",
    )
    predicate_filter: str | None = Field(
        default=None,
        description="Optional: restrict to this predicate",
    )
