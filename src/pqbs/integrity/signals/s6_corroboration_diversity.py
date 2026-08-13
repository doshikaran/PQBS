"""S6 — Corroboration Diversity Signal.

Checks how many distinct source digests corroborate the same
(tenant_id, subject, predicate, object_normalized) belief. Low
corroboration diversity is suspicious.
Weight: 0.05.
"""
from __future__ import annotations

import time
from typing import Any

import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot
from pqbs.contracts.enums import SignalId
from pqbs.contracts.signals import SignalEvidence, SignalScore

logger = structlog.get_logger(__name__)

_SIGNAL_ID = SignalId.S6_CORROBORATION_DIVERSITY


def _score_from_distinct(n: int) -> float:
    if n < 2:
        return 0.7
    if n <= 4:
        return 0.3
    return 0.0


def compute(snapshot: BeliefSnapshot, conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S6: corroboration diversity via distinct source digests."""
    t0 = time.perf_counter()

    # If object_normalized is None, fall back to object for matching
    normalized_value = snapshot.object_normalized if snapshot.object_normalized else snapshot.object

    row = conn.execute(
        """
        SELECT COUNT(DISTINCT p.source_digest) AS distinct_digests
        FROM belief b
        JOIN provenance p ON p.provenance_id = b.provenance_id
                          AND p.tenant_id = b.tenant_id
        WHERE b.tenant_id = %s
          AND b.subject = %s
          AND b.predicate = %s
          AND b.object_normalized = %s
        """,
        (
            str(snapshot.tenant_id),
            snapshot.subject,
            snapshot.predicate,
            normalized_value,
        ),
    ).fetchone()

    latency_ms = int((time.perf_counter() - t0) * 1000)

    distinct_count: int = int(row["distinct_digests"]) if row else 0
    score = _score_from_distinct(distinct_count)
    fired = score >= 0.7

    logger.debug(
        "s6_computed",
        belief_id=str(snapshot.belief_id),
        distinct_digests=distinct_count,
        score=score,
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=score,
        evidence=SignalEvidence(
            description=(
                f"{distinct_count} distinct source digest(s) corroborate "
                f"(subject={snapshot.subject!r}, predicate={snapshot.predicate!r})"
            ),
            raw_values={
                "distinct_source_digests": distinct_count,
                "subject": snapshot.subject,
                "predicate": snapshot.predicate,
                "object_normalized": normalized_value,
            },
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
