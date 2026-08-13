"""A7 Resolution agent.

Runs inside a serializable transaction. Detects contradiction between the
challenger belief and any incumbent, applies resolution precedence, writes
provenance and belief rows, and optionally writes a contradiction_event row.

Security Invariant 1: Every belief is written with status='pending'.
Security Invariant 7: This agent writes beliefs — it does NOT issue verdicts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog

from pqbs.contracts import (
    EmbeddedBelief,
    ProvenanceRecord,
    Resolution,
    ResolutionBasis,
    ResolutionOutcome,
    Sensitivity,
)
from pqbs.contracts.enums import Cardinality, TrustTier

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Trust-tier ordering for resolution precedence
# ---------------------------------------------------------------------------

TRUST_TIER_ORDER: dict[TrustTier, int] = {
    TrustTier.AUTHORITATIVE: 3,
    TrustTier.CORROBORATED: 2,
    TrustTier.UNVERIFIED: 1,
    TrustTier.UNTRUSTED: 0,
}


# ---------------------------------------------------------------------------
# Resolution precedence logic
# ---------------------------------------------------------------------------

def _resolve_precedence(
    challenger_tier: TrustTier,
    challenger_confidence: float,
    challenger_valid_from: datetime,
    incumbent_tier: TrustTier,
    incumbent_confidence: float,
    incumbent_valid_from: datetime,
    override: ResolutionBasis | None,
) -> tuple[Resolution, ResolutionBasis]:
    """Determine which belief wins according to the resolution precedence rules.

    Precedence (highest priority first):
    1. EXPLICIT_INVALIDATION override
    2. Source trust tier
    3. Recency (valid_from)
    4. Confidence
    5. Tie → DEFERRED/POLICY
    """
    if override == ResolutionBasis.EXPLICIT_INVALIDATION:
        return Resolution.CHALLENGER_SUPERSEDES, ResolutionBasis.EXPLICIT_INVALIDATION

    c_rank = TRUST_TIER_ORDER[challenger_tier]
    i_rank = TRUST_TIER_ORDER.get(incumbent_tier, 0)

    if c_rank > i_rank:
        return Resolution.CHALLENGER_SUPERSEDES, ResolutionBasis.SOURCE_TIER
    if c_rank < i_rank:
        return Resolution.INCUMBENT_RETAINED, ResolutionBasis.SOURCE_TIER

    if challenger_valid_from > incumbent_valid_from:
        return Resolution.CHALLENGER_SUPERSEDES, ResolutionBasis.RECENCY
    if challenger_valid_from < incumbent_valid_from:
        return Resolution.INCUMBENT_RETAINED, ResolutionBasis.RECENCY

    if challenger_confidence > incumbent_confidence + 1e-9:
        return Resolution.CHALLENGER_SUPERSEDES, ResolutionBasis.CONFIDENCE
    if incumbent_confidence > challenger_confidence + 1e-9:
        return Resolution.INCUMBENT_RETAINED, ResolutionBasis.CONFIDENCE

    return Resolution.DEFERRED, ResolutionBasis.POLICY


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def _write_provenance(
    conn: psycopg.Connection[Any],
    provenance_record: ProvenanceRecord,
    tenant_id: UUID,
) -> UUID:
    """Insert a provenance row and return provenance_id."""
    derived_from_json = json.dumps(
        [str(uid) for uid in provenance_record.derived_from]
    )
    conn.execute(
        """
        INSERT INTO provenance
            (provenance_id, tenant_id, source_type, source_uri, source_digest,
             episode_id, derived_from, ingested_at, source_trust_tier, ingestion_agent_id)
        VALUES (%s, %s, %s::source_type, %s, %s, %s, %s::jsonb, %s, %s::trust_tier, %s)
        """,
        (
            str(provenance_record.provenance_id),
            str(tenant_id),
            provenance_record.source_type.value,
            provenance_record.source_uri,
            provenance_record.source_digest,
            str(provenance_record.episode_id),
            derived_from_json,
            provenance_record.ingested_at,
            provenance_record.source_trust_tier.value,
            provenance_record.ingestion_agent_id,
        ),
    )
    return provenance_record.provenance_id


def _write_belief(
    conn: psycopg.Connection[Any],
    embedded: EmbeddedBelief,
    provenance_id: UUID,
) -> UUID:
    """Insert a belief row with status='pending' and return belief_id.

    Security Invariant 1: status is always 'pending' here.
    """
    candidate = embedded.normalized.candidate
    sensitivity = embedded.normalized.sensitivity

    # Format embedding as pgvector literal: '[0.1,0.2,...]'
    embedding_str = "[" + ",".join(str(x) for x in embedded.embedding) + "]"

    belief_id = candidate.belief_id

    conn.execute(
        """
        INSERT INTO belief
            (tenant_id, belief_id, subject, predicate, object, object_normalized,
             embedding, confidence, valid_from, valid_to, status,
             author_agent_id, provenance_id, sensitivity)
        VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, 'pending', %s, %s, %s::sensitivity)
        """,
        (
            str(candidate.tenant_id),
            str(belief_id),
            candidate.subject,
            candidate.predicate,
            candidate.object,
            embedded.normalized.object_normalized,
            embedding_str,
            candidate.confidence,
            candidate.valid_from,
            candidate.valid_to,
            candidate.author_agent_id,
            str(provenance_id),
            sensitivity.value,
        ),
    )
    return belief_id


def _write_contradiction_event(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    incumbent_belief_id: UUID,
    challenger_belief_id: UUID,
    resolution: Resolution,
    resolution_basis: ResolutionBasis,
    retry_count: int,
    detected_at: datetime,
) -> UUID:
    """Insert a contradiction_event row and return event_id."""
    event_id = uuid4()
    conn.execute(
        """
        INSERT INTO contradiction_event
            (event_id, tenant_id, incumbent_belief_id, challenger_belief_id,
             resolution, resolution_basis, retry_count, detected_at)
        VALUES (%s, %s, %s, %s, %s::resolution, %s::resolution_basis, %s, %s)
        """,
        (
            str(event_id),
            str(tenant_id),
            str(incumbent_belief_id),
            str(challenger_belief_id),
            resolution.value,
            resolution_basis.value,
            retry_count,
            detected_at,
        ),
    )
    return event_id


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def resolve(
    conn: psycopg.Connection[Any],
    embedded: EmbeddedBelief,
    provenance_record: ProvenanceRecord,
    retry_count: int = 0,
    resolution_basis_override: ResolutionBasis | None = None,
) -> ResolutionOutcome:
    """Run inside a serializable transaction.

    Steps:
    1. Fetch predicate_policy for cardinality and resolution strategy.
    2. Write provenance row first (belief FK depends on it).
    3. If multi_valued: write belief as pending, return BOTH_RETAINED/POLICY.
    4. Query for incumbent (trusted, current, same subject+predicate).
    5. If no incumbent: write belief, return BOTH_RETAINED/RECENCY (first write).
    6. Apply resolution precedence.
    7. Write contradiction_event row.
    8. If challenger supersedes: mark incumbent superseded.
    9. Write challenger belief as pending.
    10. Return ResolutionOutcome.
    """
    candidate = embedded.normalized.candidate
    tenant_id = candidate.tenant_id
    now = datetime.now(tz=timezone.utc)

    # Step 1: Fetch predicate policy
    policy_row = conn.execute(
        """
        SELECT cardinality, resolution_strategy
        FROM predicate_policy
        WHERE tenant_id = %s AND predicate = %s
        """,
        (str(tenant_id), candidate.predicate),
    ).fetchone()

    if policy_row is None:
        cardinality = Cardinality.SINGLE_VALUED
        # Default resolution strategy — recency — handled via TRUST_TIER_ORDER
    else:
        cardinality = Cardinality(policy_row["cardinality"])

    # Step 2: Write provenance row
    provenance_id = _write_provenance(conn, provenance_record, tenant_id)

    # Step 3: Multi-valued predicate — no contradiction possible, just append
    if cardinality == Cardinality.MULTI_VALUED:
        belief_id = _write_belief(conn, embedded, provenance_id)
        logger.info(
            "resolve_multi_valued",
            tenant_id=str(tenant_id),
            belief_id=str(belief_id),
            predicate=candidate.predicate,
        )
        return ResolutionOutcome(
            belief_id=belief_id,
            tenant_id=tenant_id,
            resolution=Resolution.BOTH_RETAINED,
            basis=ResolutionBasis.POLICY,
            incumbent_id=None,
            challenger_id=belief_id,
            retry_count=retry_count,
            detected_at=now,
            contradiction_event_id=None,
        )

    # Step 4: Query for incumbent (single_valued path)
    incumbent_row = conn.execute(
        """
        SELECT b.belief_id, b.confidence, b.valid_from, b.valid_to, p.source_trust_tier
        FROM belief b
        JOIN provenance p
            ON (b.tenant_id = p.tenant_id AND b.provenance_id = p.provenance_id)
        WHERE b.tenant_id = %s
          AND b.subject = %s
          AND b.predicate = %s
          AND b.status = 'trusted'
          AND b.tx_to IS NULL
          AND (b.valid_to IS NULL OR b.valid_to > %s)
        ORDER BY b.valid_from DESC
        LIMIT 1
        """,
        (str(tenant_id), candidate.subject, candidate.predicate, now),
    ).fetchone()

    # Step 5: No incumbent — first write
    if incumbent_row is None:
        belief_id = _write_belief(conn, embedded, provenance_id)
        logger.info(
            "resolve_first_write",
            tenant_id=str(tenant_id),
            belief_id=str(belief_id),
            predicate=candidate.predicate,
        )
        return ResolutionOutcome(
            belief_id=belief_id,
            tenant_id=tenant_id,
            resolution=Resolution.BOTH_RETAINED,
            basis=ResolutionBasis.RECENCY,
            incumbent_id=None,
            challenger_id=belief_id,
            retry_count=retry_count,
            detected_at=now,
            contradiction_event_id=None,
        )

    # We have an incumbent — apply full resolution precedence
    incumbent_belief_id = UUID(str(incumbent_row["belief_id"]))
    incumbent_confidence: float = float(incumbent_row["confidence"])
    incumbent_valid_from: datetime = incumbent_row["valid_from"]
    incumbent_tier = TrustTier(incumbent_row["source_trust_tier"])

    # Determine challenger trust tier from its provenance record
    challenger_tier = provenance_record.source_trust_tier

    # Step 6: Resolution precedence
    resolution, basis = _resolve_precedence(
        challenger_tier=challenger_tier,
        challenger_confidence=candidate.confidence,
        challenger_valid_from=candidate.valid_from,
        incumbent_tier=incumbent_tier,
        incumbent_confidence=incumbent_confidence,
        incumbent_valid_from=incumbent_valid_from,
        override=resolution_basis_override,
    )

    # Step 8: Mark incumbent superseded if challenger wins (done before writing
    # challenger so the superseded_by FK references an existing belief_id)
    # We write the challenger FIRST to establish its belief_id, then update
    # the incumbent. But belief_id is generated by us (not the DB default here),
    # so we know it before the INSERT.
    challenger_belief_id = candidate.belief_id

    if resolution == Resolution.CHALLENGER_SUPERSEDES:
        # Step 9: Write challenger belief first
        _write_belief(conn, embedded, provenance_id)

        # Then mark incumbent superseded with reference to challenger
        conn.execute(
            """
            UPDATE belief
            SET status = 'superseded', superseded_by = %s, tx_to = NOW()
            WHERE tenant_id = %s AND belief_id = %s
            """,
            (
                str(challenger_belief_id),
                str(tenant_id),
                str(incumbent_belief_id),
            ),
        )
    else:
        # INCUMBENT_RETAINED or DEFERRED — write challenger as pending regardless
        _write_belief(conn, embedded, provenance_id)

    # Step 7: Write contradiction_event (always for single_valued with an incumbent)
    event_id = _write_contradiction_event(
        conn=conn,
        tenant_id=tenant_id,
        incumbent_belief_id=incumbent_belief_id,
        challenger_belief_id=challenger_belief_id,
        resolution=resolution,
        resolution_basis=basis,
        retry_count=retry_count,
        detected_at=now,
    )

    logger.info(
        "resolve_contradiction",
        tenant_id=str(tenant_id),
        challenger_id=str(challenger_belief_id),
        incumbent_id=str(incumbent_belief_id),
        resolution=resolution.value,
        basis=basis.value,
        event_id=str(event_id),
    )

    return ResolutionOutcome(
        belief_id=challenger_belief_id,
        tenant_id=tenant_id,
        resolution=resolution,
        basis=basis,
        incumbent_id=incumbent_belief_id,
        challenger_id=challenger_belief_id,
        retry_count=retry_count,
        detected_at=now,
        contradiction_event_id=event_id,
    )
