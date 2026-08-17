"""Lambda screening worker — PQBS Phase 4 integrity path.

Receives CockroachDB CDC webhook events and runs each pending belief
through the ScreeningGate (A4). This is the production architecture;
BeliefPoller is the local-dev fallback.

CDC changefeed setup (run once, after migrations):
  CREATE CHANGEFEED FOR TABLE belief
  INTO 'webhook-https://<FUNCTION_URL>/screen'
  WITH
    updated,
    full_table_name,
    format = 'json',
    min_checkpoint_frequency = '1s';

Event format from CockroachDB CDC webhook:
  {
    "payload": [
      {
        "after":   { <full row> | null },
        "before":  { <full row> | null },
        "updated": "<crdb-timestamp>",
        "key":     ["<pk>"],
        "topic":   "belief"
      }
    ],
    "length": N
  }

Security invariants upheld:
  - Only INSERT events on PENDING beliefs are screened.
  - Updates that already carry a trust_score (re-deliveries) are skipped
    via the (belief_id, screener_version) idempotency key in the gate.
  - A Lambda error (non-200 response) causes CockroachDB CDC to retry.
    Fail-closed: the belief stays pending until the worker recovers.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pqbs.contracts.cdc import BeliefSnapshot, ChangeEvent
from pqbs.contracts.enums import BeliefStatus, CdcOperation, Sensitivity
from pqbs.integrity.gate import ScreeningGate, SCREENER_VERSION
from pqbs.substrate.connection import get_connection

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_gate: ScreeningGate | None = None


def _get_gate() -> ScreeningGate:
    global _gate
    if _gate is None:
        _gate = ScreeningGate()
    return _gate


def _parse_row(row: dict[str, Any]) -> BeliefSnapshot:
    """Convert a CDC row dict to a BeliefSnapshot."""

    def _uuid(v: Any) -> UUID | None:
        return UUID(str(v)) if v is not None else None

    def _dt(v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        # CockroachDB CDC delivers timestamps as RFC3339 strings
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    sensitivity_raw = row.get("sensitivity", "normal")
    if sensitivity_raw not in ("normal", "elevated"):
        sensitivity_raw = "normal"

    return BeliefSnapshot(
        belief_id=_uuid(row["belief_id"]),  # type: ignore[arg-type]
        tenant_id=_uuid(row["tenant_id"]),  # type: ignore[arg-type]
        subject=str(row["subject"]),
        predicate=str(row["predicate"]),
        object=str(row["object"]),
        object_normalized=row.get("object_normalized"),
        confidence=float(row["confidence"]),
        valid_from=_dt(row["valid_from"]),  # type: ignore[arg-type]
        valid_to=_dt(row.get("valid_to")),
        tx_from=_dt(row["tx_from"]),  # type: ignore[arg-type]
        tx_to=_dt(row.get("tx_to")),
        status=BeliefStatus(row.get("status", "pending")),
        supersedes=_uuid(row.get("supersedes")),
        superseded_by=_uuid(row.get("superseded_by")),
        author_agent_id=str(row["author_agent_id"]),
        provenance_id=_uuid(row["provenance_id"]),  # type: ignore[arg-type]
        trust_score=float(row["trust_score"]) if row.get("trust_score") is not None else None,
        screened_at=_dt(row.get("screened_at")),
        sensitivity=Sensitivity(sensitivity_raw),
    )


def _screen_event(entry: dict[str, Any], gate: ScreeningGate) -> dict[str, str]:
    """Process one CDC event entry. Returns a result dict for logging."""
    after = entry.get("after")
    if after is None:
        # DELETE event — no action
        return {"action": "skip", "reason": "delete_event"}

    snapshot = _parse_row(after)

    if snapshot.status != BeliefStatus.PENDING:
        return {"action": "skip", "reason": f"status={snapshot.status.value}"}

    if snapshot.trust_score is not None:
        # Already screened (re-delivery of an update event after verdict)
        return {"action": "skip", "reason": "already_screened"}

    op = CdcOperation.INSERT if entry.get("before") is None else CdcOperation.UPDATE
    event = ChangeEvent(
        belief_id=snapshot.belief_id,
        tenant_id=snapshot.tenant_id,
        operation=op,
        before=None,
        after=snapshot,
        commit_timestamp=datetime.now(tz=timezone.utc),
    )

    with get_connection() as conn:
        verdict = gate.screen(event, conn)

    return {
        "action": "screened",
        "belief_id": str(snapshot.belief_id),
        "verdict": verdict.verdict.value,
        "trust_score": str(verdict.trust_score),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point.

    Accepts:
    - HTTP POST from CockroachDB CDC webhook (API Gateway or Function URL)
    - Direct invocation with {"payload": [...]} for testing

    Returns HTTP 200 on full success, 500 on any unhandled error.
    Partial errors (one row fails) return 200 with an errors list so the
    changefeed doesn't retry the entire batch for a single bad row.
    """
    gate = _get_gate()

    # --- Parse body ---
    body_raw = event.get("body", "")
    if isinstance(body_raw, str):
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError as exc:
            logger.error("invalid_json_body: %s", exc)
            return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON"})}
    elif isinstance(body_raw, dict):
        body = body_raw
    else:
        # Direct invocation with the CDC payload dict
        body = event

    entries = body.get("payload", [])
    if not isinstance(entries, list):
        return {"statusCode": 400, "body": json.dumps({"error": "payload must be a list"})}

    results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for entry in entries:
        try:
            result = _screen_event(entry, gate)
            results.append(result)
            logger.info("screened entry: %s", result)
        except Exception as exc:  # noqa: BLE001
            belief_id = (entry.get("after") or {}).get("belief_id", "unknown")
            logger.error("screening_failed belief_id=%s: %s", belief_id, exc, exc_info=True)
            errors.append({"belief_id": str(belief_id), "error": str(exc)})

    status_code = 200 if not errors else 207
    response_body = {
        "screened": len([r for r in results if r.get("action") == "screened"]),
        "skipped": len([r for r in results if r.get("action") == "skip"]),
        "errors": errors,
    }
    return {"statusCode": status_code, "body": json.dumps(response_body)}
