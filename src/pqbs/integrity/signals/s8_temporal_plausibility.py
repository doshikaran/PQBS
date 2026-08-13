"""S8 — Temporal Plausibility Signal.

Checks whether the belief's validity period is temporally plausible:
- valid_from far in the future (> now + 1 year)
- valid_to before valid_from
- valid_from before 1970-01-01
- validity span > 200 years
Weight: 0.05.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot
from pqbs.contracts.enums import SignalId
from pqbs.contracts.signals import SignalEvidence, SignalScore

logger = structlog.get_logger(__name__)

_SIGNAL_ID = SignalId.S8_TEMPORAL_PLAUSIBILITY

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SPAN_YEARS = 200
_MAX_SPAN_DAYS = _MAX_SPAN_YEARS * 365


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def compute(snapshot: BeliefSnapshot, _conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S8: temporal plausibility checks on valid_from / valid_to."""
    t0 = time.perf_counter()
    now = datetime.now(tz=timezone.utc)

    valid_from = _ensure_utc(snapshot.valid_from)
    valid_to = _ensure_utc(snapshot.valid_to) if snapshot.valid_to else None

    violation: str | None = None
    score = 0.0

    # Check 1: valid_from more than 1 year in the future
    if valid_from > now + timedelta(days=365):
        violation = (
            f"valid_from ({valid_from.isoformat()}) is more than 1 year in the future"
        )
        score = 1.0

    # Check 2: valid_to before valid_from
    elif valid_to is not None and valid_to < valid_from:
        violation = (
            f"valid_to ({valid_to.isoformat()}) is before "
            f"valid_from ({valid_from.isoformat()})"
        )
        score = 1.0

    # Check 3: valid_from before 1970-01-01
    elif valid_from < _EPOCH:
        violation = (
            f"valid_from ({valid_from.isoformat()}) is before 1970-01-01"
        )
        score = 0.8

    # Check 4: span > 200 years
    elif valid_to is not None and (valid_to - valid_from).days > _MAX_SPAN_DAYS:
        span_years = (valid_to - valid_from).days / 365.0
        violation = (
            f"Validity span of {span_years:.1f} years exceeds {_MAX_SPAN_YEARS}-year maximum"
        )
        score = 0.5

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if violation is not None:
        fired = True
        logger.debug(
            "s8_violation",
            belief_id=str(snapshot.belief_id),
            violation=violation,
            score=score,
        )
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=score,
            evidence=SignalEvidence(
                description=violation,
                raw_values={
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat() if valid_to else "",
                    "now": now.isoformat(),
                },
            ),
            latency_ms=latency_ms,
            fired=fired,
        )

    logger.debug(
        "s8_computed",
        belief_id=str(snapshot.belief_id),
        score=0.0,
        fired=False,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=0.0,
        evidence=SignalEvidence(
            description="Temporal validity period is plausible",
            raw_values={
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat() if valid_to else "",
            },
        ),
        latency_ms=latency_ms,
        fired=False,
    )
