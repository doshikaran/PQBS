"""S5 — Contradiction Burst Signal.

Detects a surge of contradiction events for the same (tenant_id, predicate)
within a 300-second window, which may indicate a coordinated poisoning attempt.
Weight: 0.10.
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

_SIGNAL_ID = SignalId.S5_CONTRADICTION_BURST
_WINDOW_SECONDS = 300


def _score_from_count(n: int) -> float:
    if n == 0:
        return 0.0
    if n <= 2:
        return 0.2
    if n <= 5:
        return 0.5
    return 0.9


def compute(snapshot: BeliefSnapshot, conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S5: contradiction burst detection for (tenant_id, predicate)."""
    t0 = time.perf_counter()

    # contradiction_event has: tenant_id, incumbent_belief_id, challenger_belief_id,
    # resolution, resolution_basis, retry_count, detected_at
    # We join to belief to filter by predicate on the challenger side.
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM contradiction_event ce
        JOIN belief b ON b.belief_id = ce.challenger_belief_id
                      AND b.tenant_id = ce.tenant_id
        WHERE ce.tenant_id = %s
          AND b.predicate = %s
          AND ce.detected_at >= now() - INTERVAL '300 seconds'
        """,
        (str(snapshot.tenant_id), snapshot.predicate),
    ).fetchone()

    latency_ms = int((time.perf_counter() - t0) * 1000)

    count: int = int(row["cnt"]) if row else 0
    score = _score_from_count(count)
    fired = score > 0.3

    logger.debug(
        "s5_computed",
        belief_id=str(snapshot.belief_id),
        predicate=snapshot.predicate,
        contradiction_count=count,
        score=score,
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=score,
        evidence=SignalEvidence(
            description=(
                f"{count} contradictions for predicate '{snapshot.predicate}' "
                f"in last {_WINDOW_SECONDS}s"
            ),
            raw_values={
                "contradiction_count": count,
                "window_seconds": _WINDOW_SECONDS,
                "predicate": snapshot.predicate,
            },
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
