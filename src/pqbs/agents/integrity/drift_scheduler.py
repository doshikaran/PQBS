"""DriftScheduler — scheduled execution of A5 DriftAgent.

Runs DriftAgent.run_drift_scan() on a configurable interval for one or more
tenants, emitting POSTURE_DRIFT_DETECTED audit records for any alerts found,
and recording cascade/drift metrics via the MetricsCollector singleton.

Design:
- One-shot mode (run_once) for Lambda invocation or manual testing.
- Loop mode (run) for long-running background process or threading.Thread.
- Authority: A5 may not quarantine directly. DriftScheduler enforces this by
  only calling run_drift_scan() — never quarantine() directly.
"""
from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.agents.integrity.a5_drift import DriftAgent
from pqbs.agents.integrity.audit_sink import AuditSink
from pqbs.telemetry import get_metrics

logger = structlog.get_logger(__name__)


class DriftScheduler:
    """Periodic runner for A5 DriftAgent across a set of tenants.

    Usage (background thread)::

        scheduler = DriftScheduler(
            drift_agent=DriftAgent(),
            audit_sink=audit_sink,
            tenant_ids=[UUID("...")],
            scan_interval_seconds=300,
        )
        stop = threading.Event()
        t = threading.Thread(target=scheduler.run, kwargs={"stop_event": stop}, daemon=True)
        t.start()
        # ...
        stop.set(); t.join()

    Usage (one-shot Lambda)::

        scheduler = DriftScheduler(...)
        with psycopg.connect(url, row_factory=dict_row) as conn:
            scheduler.run_once(conn)
    """

    def __init__(
        self,
        drift_agent: DriftAgent,
        audit_sink: AuditSink,
        tenant_ids: list[UUID],
        scan_interval_seconds: float = 300.0,
        contradiction_threshold: int = 5,
        write_shift_window_hours: int = 24,
        write_shift_baseline_hours: int = 168,
        min_cluster_size: int = 5,
        dormant_days: int = 7,
        burst_hours: int = 2,
        burst_threshold: int = 3,
    ) -> None:
        self._agent = drift_agent
        self._audit_sink = audit_sink
        self._tenant_ids = list(tenant_ids)
        self._interval = scan_interval_seconds
        self._contradiction_threshold = contradiction_threshold
        self._write_shift_window_hours = write_shift_window_hours
        self._write_shift_baseline_hours = write_shift_baseline_hours
        self._min_cluster_size = min_cluster_size
        self._dormant_days = dormant_days
        self._burst_hours = burst_hours
        self._burst_threshold = burst_threshold

    # ------------------------------------------------------------------
    # One-shot scan (Lambda entry-point / testing)
    # ------------------------------------------------------------------

    def run_once(self, conn: psycopg.Connection[Any]) -> dict[UUID, int]:
        """Run a single drift scan across all configured tenants.

        Returns:
            Mapping of tenant_id → alert count detected.
        """
        results: dict[UUID, int] = {}
        for tenant_id in self._tenant_ids:
            log = logger.bind(tenant_id=str(tenant_id))
            try:
                result = self._agent.run_drift_scan(
                    tenant_id=tenant_id,
                    conn=conn,
                    audit_sink=self._audit_sink,
                    contradiction_threshold=self._contradiction_threshold,
                    write_shift_window_hours=self._write_shift_window_hours,
                    write_shift_baseline_hours=self._write_shift_baseline_hours,
                    min_cluster_size=self._min_cluster_size,
                    dormant_days=self._dormant_days,
                    burst_hours=self._burst_hours,
                    burst_threshold=self._burst_threshold,
                )
                results[tenant_id] = len(result.alerts)

                for alert in result.alerts:
                    try:
                        get_metrics().record_drift_alert(
                            detection_type=alert.detection_type,
                            severity=alert.severity,
                        )
                    except Exception:
                        pass

                if result.has_alerts:
                    log.warning(
                        "drift_scan_alerts_found",
                        alert_count=len(result.alerts),
                        high_severity=result.high_severity_count,
                    )
                else:
                    log.info("drift_scan_clean")

            except Exception as exc:
                log.error("drift_scan_failed", error=str(exc))
                results[tenant_id] = -1  # sentinel for error

        return results

    # ------------------------------------------------------------------
    # Loop mode (background thread / ECS task)
    # ------------------------------------------------------------------

    def run(
        self,
        stop_event: threading.Event | None = None,
        *,
        get_conn: Any = None,
    ) -> None:
        """Run continuous drift scan loop until stop_event is set.

        Args:
            stop_event: Threading event to request graceful shutdown.
            get_conn: Optional callable returning a psycopg.Connection.
                      If not supplied, uses pqbs.substrate.connection.get_connection.
        """
        if get_conn is None:
            from pqbs.substrate.connection import get_connection
            get_conn = get_connection

        logger.info(
            "drift_scheduler_started",
            tenant_count=len(self._tenant_ids),
            scan_interval_seconds=self._interval,
        )

        try:
            with get_conn() as conn:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        break

                    self.run_once(conn)

                    # Sleep in short increments to remain responsive to stop_event.
                    deadline = time.monotonic() + self._interval
                    while time.monotonic() < deadline:
                        if stop_event is not None and stop_event.is_set():
                            return
                        time.sleep(1.0)

        except KeyboardInterrupt:
            pass
        finally:
            logger.info("drift_scheduler_stopped")
