"""
pqbs.contracts — frozen interface contracts for the PQBS system.

All public classes are exported from this module so consumers can write:

    from pqbs.contracts import CandidateBelief, ChangeEvent, Verdict

Contracts are frozen at the end of Phase 1 (tag: interface-freeze-v1).
Any change after freeze requires the full interface-change protocol
documented in CLAUDE.md § Interface Freeze.
"""
from __future__ import annotations

# Base config — re-exported for implementation layers that need it
from pqbs.contracts.base import CONTRACT_CONFIG, EMBEDDING_DIM

# Enums
from pqbs.contracts.enums import (
    AgentClass,
    AgentStatus,
    AuditEventType,
    BeliefStatus,
    Cardinality,
    CdcOperation,
    Disposition,
    ReasonCode,
    Resolution,
    ResolutionBasis,
    Sensitivity,
    SignalId,
    SourceType,
    TemporalMechanism,
    TrustTier,
    VerdictValue,
)

# Provenance
from pqbs.contracts.provenance import ProvenanceRecord, ProvenanceStub

# Belief pipeline
from pqbs.contracts.beliefs import CandidateBelief, EmbeddedBelief, NormalizedBelief

# Resolution
from pqbs.contracts.resolution import ResolutionOutcome

# CDC — the sync/async boundary
from pqbs.contracts.cdc import BeliefSnapshot, ChangeEvent

# Signals
from pqbs.contracts.signals import SignalEvidence, SignalScore

# Verdicts and quarantine
from pqbs.contracts.verdicts import QuarantineRecord, Verdict

# Cascade
from pqbs.contracts.cascade import CascadeRequest

# Recall surface
from pqbs.contracts.recall import RecalledBelief, RecallRequest, RecallResult, TemporalContext

# Temporal reconstruction
from pqbs.contracts.temporal import TemporalQuery

# Audit
from pqbs.contracts.audit import AuditRecord

# Domain exceptions
from pqbs.contracts.exceptions import (
    AuditSinkError,
    BeliefValidationError,
    ContractionError,
    EmbeddingError,
    InsufficientTrustError,
    PQBSError,
    QuarantineError,
    RetryExhaustedError,
    ScreeningError,
    TenantIsolationError,
)

__all__ = [
    # Base
    "CONTRACT_CONFIG",
    "EMBEDDING_DIM",
    # Enums
    "AgentClass",
    "AgentStatus",
    "AuditEventType",
    "BeliefStatus",
    "Cardinality",
    "CdcOperation",
    "Disposition",
    "ReasonCode",
    "Resolution",
    "ResolutionBasis",
    "Sensitivity",
    "SignalId",
    "SourceType",
    "TemporalMechanism",
    "TrustTier",
    "VerdictValue",
    # Provenance
    "ProvenanceRecord",
    "ProvenanceStub",
    # Belief pipeline
    "CandidateBelief",
    "EmbeddedBelief",
    "NormalizedBelief",
    # Resolution
    "ResolutionOutcome",
    # CDC
    "BeliefSnapshot",
    "ChangeEvent",
    # Signals
    "SignalEvidence",
    "SignalScore",
    # Verdicts
    "QuarantineRecord",
    "Verdict",
    # Cascade
    "CascadeRequest",
    # Recall
    "RecalledBelief",
    "RecallRequest",
    "RecallResult",
    "TemporalContext",
    # Temporal
    "TemporalQuery",
    # Audit
    "AuditRecord",
    # Exceptions
    "AuditSinkError",
    "BeliefValidationError",
    "ContractionError",
    "EmbeddingError",
    "InsufficientTrustError",
    "PQBSError",
    "QuarantineError",
    "RetryExhaustedError",
    "ScreeningError",
    "TenantIsolationError",
]
