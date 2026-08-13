"""S7 — Derivation Integrity Signal.

Checks the provenance derivation chain: if any parent belief is quarantined,
this belief is immediately flagged. Tainted derivation chains propagate risk.
Weight: 0.05.
"""
from __future__ import annotations

import json
import time
from typing import Any

import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot
from pqbs.contracts.enums import SignalId
from pqbs.contracts.signals import SignalEvidence, SignalScore

logger = structlog.get_logger(__name__)

_SIGNAL_ID = SignalId.S7_DERIVATION_INTEGRITY


def compute(snapshot: BeliefSnapshot, conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S7: derivation chain integrity check."""
    t0 = time.perf_counter()

    # Fetch derived_from JSONB array from provenance
    prov_row = conn.execute(
        """
        SELECT derived_from
        FROM provenance
        WHERE provenance_id = %s AND tenant_id = %s
        """,
        (str(snapshot.provenance_id), str(snapshot.tenant_id)),
    ).fetchone()

    latency_ms_check = time.perf_counter()

    if prov_row is None:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.0,
            evidence=SignalEvidence(
                description="Provenance not found — no derivation chain to check",
                raw_values={"provenance_id": str(snapshot.provenance_id)},
            ),
            latency_ms=latency_ms,
            fired=False,
        )

    derived_from_raw = prov_row["derived_from"]

    # Handle both list (from JSONB) and JSON string
    if derived_from_raw is None:
        parent_ids: list[str] = []
    elif isinstance(derived_from_raw, list):
        parent_ids = [str(p) for p in derived_from_raw]
    elif isinstance(derived_from_raw, str):
        try:
            parsed = json.loads(derived_from_raw)
            parent_ids = [str(p) for p in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            parent_ids = []
    else:
        parent_ids = []

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if not parent_ids:
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.0,
            evidence=SignalEvidence(
                description="No derivation chain — belief is an independent assertion",
                raw_values={"derived_from_count": 0},
            ),
            latency_ms=latency_ms,
            fired=False,
        )

    # Check status of all parent beliefs
    parent_rows = conn.execute(
        """
        SELECT belief_id::text AS belief_id, status
        FROM belief
        WHERE tenant_id = %s
          AND belief_id::text = ANY(%s)
        """,
        (str(snapshot.tenant_id), parent_ids),
    ).fetchall()

    latency_ms = int((time.perf_counter() - t0) * 1000)

    parent_statuses: dict[str, str] = {r["belief_id"]: r["status"] for r in parent_rows}

    # Classify
    has_quarantined = any(s == "quarantined" for s in parent_statuses.values())
    all_trusted = (
        len(parent_statuses) == len(parent_ids)
        and all(s == "trusted" for s in parent_statuses.values())
    )
    has_pending_or_inconclusive = any(
        s in ("pending", "inconclusive") for s in parent_statuses.values()
    )

    if has_quarantined:
        score = 1.0
        fired = True
        quarantined_parents = [pid for pid, s in parent_statuses.items() if s == "quarantined"]
        description = (
            f"Derived from quarantined belief(s): {quarantined_parents}"
        )
        raw_values: dict[str, Any] = {
            "parent_ids": parent_ids,
            "parent_statuses": parent_statuses,
            "quarantined_parents": quarantined_parents,
            "reason_code": "DERIVED_FROM_QUARANTINED",
        }
    elif all_trusted:
        score = 0.0
        fired = False
        description = f"All {len(parent_ids)} parent belief(s) are trusted"
        raw_values = {
            "parent_ids": parent_ids,
            "parent_statuses": parent_statuses,
        }
    elif has_pending_or_inconclusive:
        score = 0.5
        fired = True
        pending_parents = [pid for pid, s in parent_statuses.items() if s in ("pending", "inconclusive")]
        description = (
            f"Derived from unscreened belief(s): {pending_parents}"
        )
        raw_values = {
            "parent_ids": parent_ids,
            "parent_statuses": parent_statuses,
            "pending_parents": pending_parents,
        }
    else:
        # Some parents not found in DB — treat as unknown
        score = 0.5
        fired = True
        missing_parents = [pid for pid in parent_ids if pid not in parent_statuses]
        description = (
            f"Some parent belief(s) not found in DB: {missing_parents}"
        )
        raw_values = {
            "parent_ids": parent_ids,
            "parent_statuses": parent_statuses,
            "missing_parents": missing_parents,
        }

    logger.debug(
        "s7_computed",
        belief_id=str(snapshot.belief_id),
        parent_count=len(parent_ids),
        score=score,
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=score,
        evidence=SignalEvidence(
            description=description,
            raw_values={k: str(v) if not isinstance(v, (int, float, bool)) else v for k, v in raw_values.items()},
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
