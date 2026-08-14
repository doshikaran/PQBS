"""A17 — Telemetry aggregation agent."""
from __future__ import annotations

import threading
from typing import Any, Callable
from uuid import UUID

import psycopg
import structlog

from pqbs.telemetry.metrics import MetricsCollector

logger = structlog.get_logger(__name__)


class TelemetryAgent:
    """
    Aggregates metrics and produces the snapshot for the demo UI health endpoint.
    Runs as a background thread; snapshot is refreshed every refresh_interval_seconds.
    """

    def __init__(
        self,
        collector: MetricsCollector,
        refresh_interval_seconds: float = 30.0,
    ) -> None:
        self._collector = collector
        self._interval = refresh_interval_seconds
        self._latest_snapshot: dict[str, Any] = {}
        self._stop = threading.Event()

    def get_snapshot(self) -> dict[str, Any]:
        """Return the most recent snapshot, or compute one if none exists yet."""
        return self._latest_snapshot or self._collector.snapshot()

    def refresh(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: UUID,
    ) -> dict[str, Any]:
        """Refresh metrics from DB and return snapshot."""
        self._collector.load_from_db(conn, tenant_id)
        snap = self._collector.snapshot()
        self._latest_snapshot = snap
        logger.debug(
            "telemetry_refreshed",
            tenant_id=str(tenant_id),
            belief_total=snap.get("belief_counts", {}).get("total", 0),
        )
        return snap

    def run(
        self,
        get_conn: Callable[[], psycopg.Connection[Any]],
        tenant_id: UUID,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Background loop — call refresh() every interval.

        Args:
            get_conn: Callable that returns an open psycopg3 connection.
                      Called once per iteration so connections are not held
                      across the sleep interval.
            tenant_id: Tenant to load metrics for.
            stop_event: Optional external stop signal (in addition to self._stop).
        """
        stopper = stop_event or self._stop

        logger.info(
            "telemetry_agent_started",
            tenant_id=str(tenant_id),
            interval_seconds=self._interval,
        )

        while not stopper.is_set():
            try:
                conn = get_conn()
                try:
                    self.refresh(conn, tenant_id)
                finally:
                    conn.close()
            except Exception as exc:
                logger.warning(
                    "telemetry_refresh_failed",
                    tenant_id=str(tenant_id),
                    error=str(exc),
                )

            stopper.wait(timeout=self._interval)

        logger.info("telemetry_agent_stopped", tenant_id=str(tenant_id))

    def stop(self) -> None:
        """Signal the background loop to stop."""
        self._stop.set()
