"""Belief pipeline contracts: CandidateBelief → NormalizedBelief → EmbeddedBelief."""
from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from pqbs.contracts.base import CONTRACT_CONFIG, EMBEDDING_DIM
from pqbs.contracts.enums import Sensitivity
from pqbs.contracts.provenance import ProvenanceStub


class CandidateBelief(BaseModel):
    """
    Stage 1: produced by A1/A2/A3/A16, consumed by A11.
    Status is always PENDING — enforced here, enforced by DB.
    """

    model_config = CONTRACT_CONFIG

    belief_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    subject: str = Field(min_length=1, max_length=1024)
    predicate: str = Field(min_length=1, max_length=256)
    object: str = Field(min_length=1, max_length=4096)
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: datetime
    valid_to: datetime | None = None
    provenance_stub: ProvenanceStub
    author_agent_id: str = Field(min_length=1, max_length=256)
    sensitivity: Sensitivity = Sensitivity.NORMAL

    @model_validator(mode="after")
    def valid_to_after_valid_from(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be strictly after valid_from")
        return self


class NormalizedBelief(BaseModel):
    """
    Stage 2: produced by A11, consumed by A12.
    Adds normalized object for contradiction detection.
    """

    model_config = CONTRACT_CONFIG

    candidate: CandidateBelief
    object_normalized: str = Field(min_length=1, max_length=4096)
    sensitivity: Sensitivity  # may be upgraded to ELEVATED if normalization was ambiguous


class EmbeddedBelief(BaseModel):
    """
    Stage 3: produced by A12, consumed by A7.
    Adds the vector embedding. Embedding computed OUTSIDE the transaction.
    """

    model_config = CONTRACT_CONFIG

    normalized: NormalizedBelief
    embedding: tuple[float, ...] = Field(
        description=f"Must be exactly {EMBEDDING_DIM}-dimensional"
    )

    @field_validator("embedding")
    @classmethod
    def check_embedding_dim(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if len(v) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding must be {EMBEDDING_DIM}-dimensional, got {len(v)}"
            )
        return v
