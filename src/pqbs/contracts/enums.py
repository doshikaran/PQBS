"""All enums used across the PQBS system. Imported by contracts and implementation layers."""
from __future__ import annotations

from enum import Enum


class BeliefStatus(str, Enum):
    PENDING = "pending"
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"
    INCONCLUSIVE = "inconclusive"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class SourceType(str, Enum):
    USER_STATEMENT = "user_statement"
    DOCUMENT = "document"
    TOOL_RESULT = "tool_result"
    WEB_CONTENT = "web_content"
    AGENT_INFERENCE = "agent_inference"
    SYSTEM_OF_RECORD = "system_of_record"


class TrustTier(str, Enum):
    AUTHORITATIVE = "authoritative"
    CORROBORATED = "corroborated"
    UNVERIFIED = "unverified"
    UNTRUSTED = "untrusted"


class ReasonCode(str, Enum):
    ANOMALOUS_EMBEDDING = "anomalous_embedding"
    UNTRUSTED_SOURCE = "untrusted_source"
    IMPERATIVE_CONTENT = "imperative_content"
    CONTRADICTION_BURST = "contradiction_burst"
    IDENTITY_ANOMALY = "identity_anomaly"
    DERIVED_FROM_QUARANTINED = "derived_from_quarantined"
    TEMPORAL_IMPLAUSIBLE = "temporal_implausible"
    MANUAL = "manual"


class Resolution(str, Enum):
    CHALLENGER_SUPERSEDES = "challenger_supersedes"
    INCUMBENT_RETAINED = "incumbent_retained"
    BOTH_RETAINED = "both_retained"
    DEFERRED = "deferred"


class ResolutionBasis(str, Enum):
    RECENCY = "recency"
    CONFIDENCE = "confidence"
    SOURCE_TIER = "source_tier"
    EXPLICIT_INVALIDATION = "explicit_invalidation"
    POLICY = "policy"


class Cardinality(str, Enum):
    SINGLE_VALUED = "single_valued"
    MULTI_VALUED = "multi_valued"
    TEMPORAL_SEQUENCE = "temporal_sequence"


class Disposition(str, Enum):
    HELD = "held"
    RELEASED = "released"
    REJECTED = "rejected"


class AgentClass(str, Enum):
    PRODUCER = "producer"
    INTEGRITY = "integrity"
    SEMANTICS = "semantics"
    CONSUMER = "consumer"
    PLATFORM = "platform"


class AgentStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class VerdictValue(str, Enum):
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"
    INCONCLUSIVE = "inconclusive"


class Sensitivity(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"


class TemporalMechanism(str, Enum):
    BITEMPORAL = "bitemporal"
    MVCC = "mvcc"
    BACKUP_ANCHORED = "backup_anchored"


class SignalId(str, Enum):
    S1_EMBEDDING_ANOMALY = "S1"
    S2_SOURCE_TRUST_TIER = "S2"
    S3_IMPERATIVE_CONTENT = "S3"
    S4_AUTHOR_BEHAVIOR = "S4"
    S5_CONTRADICTION_BURST = "S5"
    S6_CORROBORATION_DIVERSITY = "S6"
    S7_DERIVATION_INTEGRITY = "S7"
    S8_TEMPORAL_PLAUSIBILITY = "S8"


class CdcOperation(str, Enum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


class AuditEventType(str, Enum):
    BELIEF_CREATED = "belief_created"
    BELIEF_SUPERSEDED = "belief_superseded"
    BELIEF_SCREENED = "belief_screened"
    BELIEF_QUARANTINED = "belief_quarantined"
    BELIEF_RELEASED = "belief_released"
    BELIEF_REJECTED = "belief_rejected"
    CASCADE_INITIATED = "cascade_initiated"
    CASCADE_COMPLETED = "cascade_completed"
    AGENT_REGISTERED = "agent_registered"
    AGENT_SUSPENDED = "agent_suspended"
    POSTURE_DRIFT_DETECTED = "posture_drift_detected"
    POSTURE_ATTESTED = "posture_attested"
    CONTROL_PLANE_AUDIT_INGESTED = "control_plane_audit_ingested"
    TEMPORAL_QUERY = "temporal_query"
    RECALL_EXECUTED = "recall_executed"
