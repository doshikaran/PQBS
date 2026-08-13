"""S4 — Author Behavior Signal.

Detects anomalous write velocity from an author agent: burst detection over
1-hour and 24-hour windows.
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

_SIGNAL_ID = SignalId.S4_AUTHOR_BEHAVIOR

_BURST_1H_THRESHOLD = 20    # writes in last 1h → score=1.0
_MODERATE_24H_THRESHOLD = 100  # writes in last 24h → score=0.7


def compute(snapshot: BeliefSnapshot, conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S4: author write-velocity burst detection."""
    t0 = time.perf_counter()

    # Count writes in last 1h
    row_1h = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM belief
        WHERE tenant_id = %s
          AND author_agent_id = %s
          AND tx_from >= now() - INTERVAL '1 hour'
        """,
        (str(snapshot.tenant_id), snapshot.author_agent_id),
    ).fetchone()

    count_1h: int = int(row_1h["cnt"]) if row_1h else 0

    # Count writes in last 24h
    row_24h = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM belief
        WHERE tenant_id = %s
          AND author_agent_id = %s
          AND tx_from >= now() - INTERVAL '24 hours'
        """,
        (str(snapshot.tenant_id), snapshot.author_agent_id),
    ).fetchone()

    count_24h: int = int(row_24h["cnt"]) if row_24h else 0

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if count_1h > _BURST_1H_THRESHOLD:
        score = 1.0
        fired = True
        description = (
            f"Burst detected: {count_1h} writes in last 1h "
            f"(threshold={_BURST_1H_THRESHOLD})"
        )
    elif count_24h > _MODERATE_24H_THRESHOLD:
        score = 0.7
        fired = True
        description = (
            f"Elevated activity: {count_24h} writes in last 24h "
            f"(threshold={_MODERATE_24H_THRESHOLD})"
        )
    else:
        score = 0.1
        fired = False
        description = (
            f"Normal activity: {count_1h} writes in 1h, "
            f"{count_24h} writes in 24h"
        )

    logger.debug(
        "s4_computed",
        belief_id=str(snapshot.belief_id),
        author_agent_id=snapshot.author_agent_id,
        count_1h=count_1h,
        count_24h=count_24h,
        score=score,
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=score,
        evidence=SignalEvidence(
            description=description,
            raw_values={
                "count_1h": count_1h,
                "count_24h": count_24h,
                "burst_1h_threshold": _BURST_1H_THRESHOLD,
                "moderate_24h_threshold": _MODERATE_24H_THRESHOLD,
                "author_agent_id": snapshot.author_agent_id,
            },
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
