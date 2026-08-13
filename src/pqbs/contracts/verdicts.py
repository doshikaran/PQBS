"""Verdict and quarantine contracts — produced by A4, consumed by A6, A14, and the audit sink."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from pqbs.contracts.base import CONTRACT_CONFIG
from pqbs.contracts.enums import Disposition, ReasonCode, VerdictValue
from pqbs.contracts.signals import SignalId, SignalScore


class Verdict(BaseModel):
    """
    Produced by A4, consumed by A6, A14, and the audit sink.
    Contains the composite trust score AND the per-signal breakdown.
    Per-signal breakdown is required by design principle P8.
    """

    model_config = CONTRACT_CONFIG

    verdict_id: UUID = Field(default_factory=uuid4)
    belief_id: UUID
    tenant_id: UUID
    verdict: VerdictValue
    trust_score: float = Field(ge=0.0, le=1.0)
    signal_scores: tuple[SignalScore, ...] = Field(
        description="All 8 signal scores, always present"
    )
    triggering_rule: str | None = Field(
        default=None,
        description="Dominant rule identifier, if verdict is deterministic",
    )
    screened_at: datetime
    screener_version: str
    re_screen_reason: str | None = None
    latency_ms: int = Field(ge=0)

    @field_validator("signal_scores")
    @classmethod
    def all_signals_present(
        cls, v: tuple[SignalScore, ...]
    ) -> tuple[SignalScore, ...]:
        if len(v) != len(SignalId):
            raise ValueError(
                f"Verdict must include all {len(SignalId)} signal scores, got {len(v)}"
            )
        present = {s.signal_id for s in v}
        missing = set(SignalId) - present
        if missing:
            raise ValueError(f"Missing signal scores for: {missing}")
        return v


class QuarantineRecord(BaseModel):
    """
    Produced by A4 when verdict == QUARANTINED, consumed by A6 and A14.
    Separate from Verdict so that cascade can be triggered by a single FK.
    """

    model_config = CONTRACT_CONFIG

    quarantine_id: UUID = Field(default_factory=uuid4)
    belief_id: UUID
    tenant_id: UUID
    reason_code: ReasonCode
    quarantined_at: datetime
    disposition: Disposition = Disposition.HELD
    triggering_verdict_id: UUID
