"""BeliefPoller — polling-based belief screener (fallback path).

This is the polling fallback — it guarantees screening within the poll
interval, not instantaneous screening. Documented as a known gap vs
log-driven CDC.

CockroachDB CDC with a webhook sink is the production architecture (V2
verified). The poller is used in dev/test environments where a live CDC
sink URL is unavailable.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot, ChangeEvent
from pqbs.contracts.enums import BeliefStatus, CdcOperation, Sensitivity
from pqbs.integrity.gate import ScreeningGate, SCREENER_VERSION

logger = structlog.get_logger(__name__)


def _row_to_belief_snapshot(row: dict[str, Any]) -> BeliefSnapshot:
    """Construct a BeliefSnapshot from a belief table row dict."""

    def _to_uuid_or_none(val: Any) -> Any:
        from uuid import UUID
        if val is None:
            return None
        return UUID(str(val))

    def _to_dt_or_none(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=timezone.utc)
            return val
        return val

    sensitivity_val = row.get("sensitivity", "normal")
    if sensitivity_val not in ("normal", "elevated"):
        sensitivity_val = "normal"

    status_val = row.get("status", "pending")

    return BeliefSnapshot(
        belief_id=_to_uuid_or_none(row["belief_id"]),
        tenant_id=_to_uuid_or_none(row["tenant_id"]),
        subject=str(row["subject"]),
        predicate=str(row["predicate"]),
        object=str(row["object"]),
        object_normalized=row.get("object_normalized"),
        confidence=float(row["confidence"]),
        valid_from=_to_dt_or_none(row["valid_from"]),
        valid_to=_to_dt_or_none(row.get("valid_to")),
        tx_from=_to_dt_or_none(row["tx_from"]),
        tx_to=_to_dt_or_none(row.get("tx_to")),
        status=BeliefStatus(status_val),
        supersedes=_to_uuid_or_none(row.get("supersedes")),
        superseded_by=_to_uuid_or_none(row.get("superseded_by")),
        author_agent_id=str(row["author_agent_id"]),
        provenance_id=_to_uuid_or_none(row["provenance_id"]),
        trust_score=float(row["trust_score"]) if row.get("trust_score") is not None else None,
        screened_at=_to_dt_or_none(row.get("screened_at")),
        sensitivity=Sensitivity(sensitivity_val),
    )


class BeliefPoller:
    """
    Polling-based belief screener. Polls for pending beliefs and submits to
    ScreeningGate.

    This is the polling fallback — it guarantees screening within the poll
    interval, not instantaneous screening. Documented in README as a known
    gap vs log-driven CDC.
    """

    def __init__(
        self,
        gate: ScreeningGate,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        self._gate = gate
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size

    def poll_once(self, conn: psycopg.Connection[Any]) -> int:
        """
        Screen all unscreened pending beliefs. Returns count screened.

        Selects the oldest unscreened pending beliefs (screened_at IS NULL),
        builds a ChangeEvent for each, and calls gate.screen().
        """
        rows = conn.execute(
            """
            SELECT
                belief_id, tenant_id, subject, predicate, object,
                object_normalized, confidence, valid_from, valid_to,
                tx_from, tx_to, status, supersedes, superseded_by,
                author_agent_id, provenance_id, trust_score, screened_at,
                sensitivity
            FROM belief
            WHERE status = 'pending'
              AND screened_at IS NULL
            ORDER BY tx_from ASC
            LIMIT %s
            """,
            (self._batch_size,),
        ).fetchall()

        screened_count = 0
        for row in rows:
            try:
                snapshot = _row_to_belief_snapshot(dict(row))
                event = ChangeEvent(
                    belief_id=snapshot.belief_id,
                    tenant_id=snapshot.tenant_id,
                    operation=CdcOperation.INSERT,
                    before=None,
                    after=snapshot,
                    commit_timestamp=snapshot.tx_from,
                    screener_version=SCREENER_VERSION,
                )
                self._gate.screen(event, conn)
                screened_count += 1
                logger.debug(
                    "poller_screened",
                    belief_id=str(snapshot.belief_id),
                    tenant_id=str(snapshot.tenant_id),
                )
            except Exception as exc:
                logger.error(
                    "poller_screen_error",
                    belief_id=str(row.get("belief_id", "unknown")),
                    error=str(exc),
                )
                # Continue — do not let one failure block the rest of the batch

        if screened_count:
            logger.info("poller_batch_done", screened=screened_count, total_rows=len(rows))

        return screened_count

    def run(self, stop_event: threading.Event | None = None) -> None:
        """Run poll loop until stop_event is set or KeyboardInterrupt."""
        from pqbs.substrate.connection import get_connection

        logger.info(
            "poller_started",
            poll_interval_seconds=self._poll_interval,
            batch_size=self._batch_size,
        )

        try:
            with get_connection() as conn:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        break
                    try:
                        count = self.poll_once(conn)
                        if count == 0:
                            logger.debug("poller_idle")
                    except Exception as exc:
                        logger.error("poller_iteration_error", error=str(exc))

                    # Sleep in short increments to respond quickly to stop_event
                    deadline = time.monotonic() + self._poll_interval
                    while time.monotonic() < deadline:
                        if stop_event is not None and stop_event.is_set():
                            return
                        time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("poller_stopped_keyboard_interrupt")
        finally:
            logger.info("poller_stopped")
