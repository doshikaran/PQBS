"""A3 Correction agent.

Creates a correction belief with EXPLICIT_INVALIDATION override, which
guarantees the challenger supersedes any incumbent regardless of trust tier,
recency, or confidence.

The correction path: CandidateBelief → canonicalize → embed → resolve(
    resolution_basis_override=EXPLICIT_INVALIDATION
)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog

from pqbs.contracts import (
    CandidateBelief,
    ProvenanceRecord,
    ProvenanceStub,
    ResolutionBasis,
    ResolutionOutcome,
    Sensitivity,
)
from pqbs.contracts.enums import TrustTier
from pqbs.agents.semantics.canonicalize import canonicalize
from pqbs.agents.semantics.embed import embed_normalized
from pqbs.agents.semantics.resolve import resolve
from pqbs.substrate.retry import with_serializable_retry
from pqbs.substrate.transaction import begin_serializable, commit, rollback

logger = structlog.get_logger(__name__)


def _resolve_correction_txn(
    conn: psycopg.Connection[Any],
    embedded_belief: Any,
    provenance_record: ProvenanceRecord,
) -> ResolutionOutcome:
    """Transaction function: begin serializable, resolve with explicit invalidation, commit."""
    begin_serializable(conn)
    try:
        outcome = resolve(
            conn,
            embedded_belief,
            provenance_record,
            resolution_basis_override=ResolutionBasis.EXPLICIT_INVALIDATION,
        )
        commit(conn)
        return outcome
    except Exception:
        rollback(conn)
        raise


def correct(
    conn: psycopg.Connection[Any],
    subject: str,
    predicate: str,
    correct_value: str,
    provenance_stub: ProvenanceStub,
    tenant_id: UUID,
    author_agent_id: str,
    confidence: float = 1.0,
    valid_from: datetime | None = None,
) -> ResolutionOutcome:
    """Apply an explicit correction to a belief.

    Creates a CandidateBelief with the corrected value, runs it through
    the full pipeline (A11 → A12 → A7) with EXPLICIT_INVALIDATION override.
    The incumbent is superseded regardless of its trust tier, recency, or
    confidence.

    Args:
        conn: Open psycopg3 connection.
        subject: The entity being corrected (e.g., "Alice").
        predicate: The attribute being corrected (e.g., "works_at").
        correct_value: The authoritative correct value.
        provenance_stub: Source provenance for this correction.
        tenant_id: Tenant UUID.
        author_agent_id: Agent performing the correction.
        confidence: Confidence in the correction (default 1.0).
        valid_from: When this fact became true (default: now).

    Returns:
        ResolutionOutcome with resolution=CHALLENGER_SUPERSEDES and
        basis=EXPLICIT_INVALIDATION when an incumbent exists.
    """
    now = datetime.now(tz=timezone.utc)
    effective_valid_from = valid_from if valid_from is not None else now

    # Build the correction as a CandidateBelief
    candidate = CandidateBelief(
        belief_id=uuid4(),
        tenant_id=tenant_id,
        subject=subject,
        predicate=predicate,
        object=correct_value,
        confidence=confidence,
        valid_from=effective_valid_from,
        valid_to=None,
        provenance_stub=provenance_stub,
        author_agent_id=author_agent_id,
        sensitivity=Sensitivity.NORMAL,
    )

    # A11: Canonicalize
    normalized = canonicalize(conn, candidate)

    # A12: Embed OUTSIDE the transaction
    embedded = embed_normalized(normalized)

    # Build provenance record — corrections are AUTHORITATIVE by design
    provenance_record = ProvenanceRecord(
        provenance_id=uuid4(),
        tenant_id=tenant_id,
        source_type=provenance_stub.source_type,
        source_uri=provenance_stub.source_uri,
        source_digest=provenance_stub.source_digest,
        episode_id=provenance_stub.episode_id,
        derived_from=(),
        ingested_at=now,
        source_trust_tier=TrustTier.AUTHORITATIVE,
        ingestion_agent_id=provenance_stub.ingestion_agent_id,
    )

    # A7: Resolve with EXPLICIT_INVALIDATION override inside serializable txn
    outcome, retry_count = with_serializable_retry(
        conn,
        _resolve_correction_txn,
        embedded,
        provenance_record,
    )

    # Rebuild outcome with the actual retry_count from the wrapper
    outcome = ResolutionOutcome(
        belief_id=outcome.belief_id,
        tenant_id=outcome.tenant_id,
        resolution=outcome.resolution,
        basis=outcome.basis,
        incumbent_id=outcome.incumbent_id,
        challenger_id=outcome.challenger_id,
        retry_count=retry_count,
        detected_at=outcome.detected_at,
        contradiction_event_id=outcome.contradiction_event_id,
    )

    logger.info(
        "correction_applied",
        tenant_id=str(tenant_id),
        belief_id=str(outcome.belief_id),
        subject=subject,
        predicate=predicate,
        resolution=outcome.resolution.value,
        incumbent_id=str(outcome.incumbent_id) if outcome.incumbent_id else None,
        retry_count=retry_count,
    )

    return outcome
