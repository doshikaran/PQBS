"""S2 — Source Trust Tier Signal.

Maps the provenance trust tier to a risk score. Untrusted sources score 1.0;
authoritative sources score 0.0.
Weight: 0.20.
"""
from __future__ import annotations

import time
from typing import Any

import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot
from pqbs.contracts.enums import SignalId, TrustTier
from pqbs.contracts.signals import SignalEvidence, SignalScore

logger = structlog.get_logger(__name__)

_SIGNAL_ID = SignalId.S2_SOURCE_TRUST_TIER

_TIER_SCORE: dict[str, float] = {
    TrustTier.AUTHORITATIVE.value: 0.0,
    TrustTier.CORROBORATED.value: 0.3,
    TrustTier.UNVERIFIED.value: 0.7,
    TrustTier.UNTRUSTED.value: 1.0,
}


def compute(snapshot: BeliefSnapshot, conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S2: risk score based on provenance source trust tier."""
    t0 = time.perf_counter()

    row = conn.execute(
        """
        SELECT source_trust_tier, source_uri
        FROM provenance
        WHERE provenance_id = %s AND tenant_id = %s
        """,
        (str(snapshot.provenance_id), str(snapshot.tenant_id)),
    ).fetchone()

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if row is None:
        logger.warning(
            "s2_provenance_not_found",
            provenance_id=str(snapshot.provenance_id),
            belief_id=str(snapshot.belief_id),
        )
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.7,  # Unknown provenance → treat as unverified
            evidence=SignalEvidence(
                description="Provenance record not found — treating as unverified",
                raw_values={"provenance_id": str(snapshot.provenance_id)},
            ),
            latency_ms=latency_ms,
            fired=True,
        )

    tier: str = row["source_trust_tier"]
    source_uri: str | None = row["source_uri"]
    score = _TIER_SCORE.get(tier, 0.7)  # Unknown tier → unverified
    fired = score >= 0.7

    logger.debug(
        "s2_computed",
        belief_id=str(snapshot.belief_id),
        tier=tier,
        score=score,
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=score,
        evidence=SignalEvidence(
            description=f"Source trust tier: {tier} → risk score {score}",
            raw_values={
                "trust_tier": tier,
                "source_uri": str(source_uri) if source_uri else "",
                "provenance_id": str(snapshot.provenance_id),
            },
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
