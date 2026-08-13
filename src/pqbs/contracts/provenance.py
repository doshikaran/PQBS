"""Provenance contracts — ProvenanceStub (used in CandidateBelief) and ProvenanceRecord (full row)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import SourceType, TrustTier


class ProvenanceStub(BaseModel):
    """Minimal provenance the producer knows at write time."""

    model_config = CONTRACT_CONFIG

    source_type: SourceType
    source_uri: str | None = None
    source_digest: str  # SHA-256 hex of source content
    episode_id: UUID    # interaction context
    ingestion_agent_id: str


class ProvenanceRecord(BaseModel):
    """Full provenance row — created by E1, consumed by A4."""

    model_config = CONTRACT_CONFIG

    provenance_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    source_type: SourceType
    source_uri: str | None = None
    source_digest: str = Field(min_length=64, max_length=64, description="SHA-256 hex")
    episode_id: UUID
    derived_from: tuple[UUID, ...] = Field(
        default=(),
        description="Parent belief IDs if this is inferred",
    )
    ingested_at: datetime
    source_trust_tier: TrustTier
    ingestion_agent_id: str = Field(min_length=1)
