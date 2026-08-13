"""Recall contracts — RecallRequest (from caller), RecallResult (from A9)."""
from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from pqbs.contracts.base import CONTRACT_CONFIG


class TemporalContext(BaseModel):
    """Optional temporal context for recall — restricts to beliefs valid at a point in time."""

    model_config = CONTRACT_CONFIG

    valid_at: datetime | None = None    # world-time filter: valid_from <= T <= valid_to
    known_at: datetime | None = None    # system-time filter: tx_from <= T and (tx_to IS NULL or tx_to > T)


class RecalledBelief(BaseModel):
    """A single belief in a recall result, with its screening evidence."""

    model_config = CONTRACT_CONFIG

    belief_id: UUID
    tenant_id: UUID
    subject: str
    predicate: str
    object: str
    object_normalized: str
    confidence: float
    trust_score: float
    valid_from: datetime
    valid_to: datetime | None
    tx_from: datetime
    author_agent_id: str
    provenance_id: UUID


class RecallRequest(BaseModel):
    """
    Produced by the caller (user-facing agent or demo), consumed by A9.
    A9 is the only agent that processes this — it has no write authority.
    """

    model_config = CONTRACT_CONFIG

    request_id: UUID = Field(default_factory=uuid4)
    query: str = Field(min_length=1, max_length=4096)
    tenant_id: UUID
    temporal_context: TemporalContext = Field(default_factory=TemporalContext)
    limit: int = Field(default=10, ge=1, le=100)
    min_trust_score: float = Field(default=0.0, ge=0.0, le=1.0)


class RecallResult(BaseModel):
    """
    Produced by A9, consumed by the calling agent.
    Always includes retrieval_id for the audit log.
    """

    model_config = CONTRACT_CONFIG

    retrieval_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    beliefs: tuple[RecalledBelief, ...] = Field(default=())
    provenance_ids: tuple[UUID, ...] = Field(
        default=(),
        description="Provenance FK for each returned belief, parallel array",
    )
    trust_scores: tuple[float, ...] = Field(
        default=(),
        description="Parallel to beliefs",
    )
    retrieved_at: datetime
    query_latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def parallel_arrays_consistent(self) -> Self:
        n = len(self.beliefs)
        if len(self.provenance_ids) != n:
            raise ValueError("provenance_ids length must match beliefs length")
        if len(self.trust_scores) != n:
            raise ValueError("trust_scores length must match beliefs length")
        return self
