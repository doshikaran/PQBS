"""Signal scoring contracts — produced by S1–S8, consumed by A4 for verdict composition."""
from __future__ import annotations

from pydantic import BaseModel, Field

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import SignalId


class SignalEvidence(BaseModel):
    """Structured evidence supporting a signal's score."""

    model_config = CONTRACT_CONFIG

    description: str
    raw_values: dict[str, float | str | int | bool] = Field(default_factory=dict)


class SignalScore(BaseModel):
    """
    Produced by each of S1–S8, consumed by A4 for verdict composition.
    One instance per signal per screening event.
    """

    model_config = CONTRACT_CONFIG

    signal_id: SignalId
    score: float = Field(
        ge=0.0,
        le=1.0,
        description="0 = clearly benign, 1 = clearly malicious",
    )
    evidence: SignalEvidence
    latency_ms: int = Field(ge=0, description="Signal computation wall-clock time")
    fired: bool = Field(
        description="True if the signal contributed to a non-trusted verdict"
    )
