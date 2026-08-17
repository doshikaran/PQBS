"""
A17 Telemetry — Four metric families per design §23.

Metrics are stored as in-process counters/histograms and can be
serialized to JSON for the demo UI or pushed to CloudWatch.
No external metrics backend required for demo — in-process is sufficient.
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Health metrics
# ---------------------------------------------------------------------------

@dataclass
class HealthMetrics:
    write_latency_ms: list[float] = field(default_factory=list)
    screening_lag_ms: list[float] = field(default_factory=list)
    recall_latency_ms: list[float] = field(default_factory=list)
    retry_count: int = 0
    retry_total_attempts: int = 0
    cdc_lag_ms: list[float] = field(default_factory=list)

    def p50(self, values: Sequence[float]) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * 0.50)
        idx = min(idx, len(sorted_vals) - 1)
        return sorted_vals[idx]

    def p99(self, values: Sequence[float]) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * 0.99)
        idx = min(idx, len(sorted_vals) - 1)
        return sorted_vals[idx]

    @property
    def retry_rate(self) -> float:
        if self.retry_total_attempts == 0:
            return 0.0
        return self.retry_count / self.retry_total_attempts


# ---------------------------------------------------------------------------
# Integrity metrics
# ---------------------------------------------------------------------------

@dataclass
class IntegrityMetrics:
    quarantine_by_reason: dict[str, int] = field(default_factory=dict)
    trust_score_samples: list[float] = field(default_factory=list)
    inconclusive_count: int = 0
    rescreening_count: int = 0
    cascade_depths: list[int] = field(default_factory=list)
    review_queue_depth: int = 0


# ---------------------------------------------------------------------------
# Security metrics
# ---------------------------------------------------------------------------

@dataclass
class SecurityMetrics:
    write_anomaly_scores: dict[str, list[float]] = field(default_factory=dict)
    contradiction_burst_by_predicate: dict[str, int] = field(default_factory=dict)
    quarantine_by_agent: dict[str, int] = field(default_factory=dict)
    imperative_detections: int = 0


# ---------------------------------------------------------------------------
# Belief count summary
# ---------------------------------------------------------------------------

@dataclass
class BeliefCounts:
    total: int = 0
    trusted: int = 0
    quarantined: int = 0
    pending: int = 0
    inconclusive: int = 0
    superseded: int = 0


# ---------------------------------------------------------------------------
# MetricsCollector (singleton-friendly)
# ---------------------------------------------------------------------------

class MetricsCollector:
    """Thread-safe in-process metrics collector.

    Record metrics from application code via record_* methods.
    Populate from DB with load_from_db().
    Serialize to JSON-safe dict with snapshot().
    """

    def __init__(self) -> None:
        self.health = HealthMetrics()
        self.integrity = IntegrityMetrics()
        self.security = SecurityMetrics()
        self.belief_counts = BeliefCounts()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Recording methods
    # ------------------------------------------------------------------

    def record_write(self, latency_ms: float, retry_count: int = 0) -> None:
        """Record a completed write with its latency and retry count."""
        with self._lock:
            self.health.write_latency_ms.append(latency_ms)
            self.health.retry_total_attempts += 1 + retry_count
            self.health.retry_count += retry_count

    def record_screening(
        self,
        lag_ms: float,
        verdict: str,
        trust_score: float,
        reason_code: str | None = None,
    ) -> None:
        """Record a completed screening event."""
        with self._lock:
            self.health.screening_lag_ms.append(lag_ms)
            self.integrity.trust_score_samples.append(trust_score)
            if verdict == "quarantined" and reason_code:
                self.integrity.quarantine_by_reason[reason_code] = (
                    self.integrity.quarantine_by_reason.get(reason_code, 0) + 1
                )
            elif verdict == "inconclusive":
                self.integrity.inconclusive_count += 1

    def record_recall(self, latency_ms: float) -> None:
        """Record a recall query latency."""
        with self._lock:
            self.health.recall_latency_ms.append(latency_ms)

    def record_cascade(self, depth: int) -> None:
        """Record a cascade traversal depth."""
        with self._lock:
            self.integrity.cascade_depths.append(depth)

    def record_cdc_lag(self, lag_ms: float) -> None:
        """Record observed CDC/polling lag (time from belief commit to screening pickup)."""
        with self._lock:
            self.health.cdc_lag_ms.append(lag_ms)

    def record_drift_alert(self, detection_type: str, severity: str) -> None:
        """Record a drift alert detection."""
        with self._lock:
            if detection_type == "contradiction_burst":
                # increment a generic counter; predicate detail is in alert.detail
                self.security.imperative_detections += 1
        logger.debug(
            "drift_alert_recorded",
            detection_type=detection_type,
            severity=severity,
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-serializable snapshot of all metrics."""
        with self._lock:
            h = self.health
            i = self.integrity
            s = self.security
            bc = self.belief_counts
            return {
                "health": {
                    "write_latency_p50_ms": round(h.p50(h.write_latency_ms), 2),
                    "write_latency_p99_ms": round(h.p99(h.write_latency_ms), 2),
                    "write_count": len(h.write_latency_ms),
                    "screening_lag_p50_ms": round(h.p50(h.screening_lag_ms), 2),
                    "screening_lag_p99_ms": round(h.p99(h.screening_lag_ms), 2),
                    "recall_latency_p50_ms": round(h.p50(h.recall_latency_ms), 2),
                    "recall_latency_p99_ms": round(h.p99(h.recall_latency_ms), 2),
                    "retry_count": h.retry_count,
                    "retry_total_attempts": h.retry_total_attempts,
                    "retry_rate": round(h.retry_rate, 4),
                    "cdc_lag_p50_ms": round(h.p50(h.cdc_lag_ms), 2),
                    "cdc_lag_p99_ms": round(h.p99(h.cdc_lag_ms), 2),
                },
                "integrity": {
                    "quarantine_by_reason": dict(i.quarantine_by_reason),
                    "trust_score_p50": round(h.p50(i.trust_score_samples), 4),
                    "trust_score_p99": round(h.p99(i.trust_score_samples), 4),
                    "trust_score_sample_count": len(i.trust_score_samples),
                    "inconclusive_count": i.inconclusive_count,
                    "rescreening_count": i.rescreening_count,
                    "cascade_depth_max": max(i.cascade_depths) if i.cascade_depths else 0,
                    "cascade_depth_avg": (
                        round(sum(i.cascade_depths) / len(i.cascade_depths), 2)
                        if i.cascade_depths else 0.0
                    ),
                    "review_queue_depth": i.review_queue_depth,
                },
                "security": {
                    "contradiction_burst_by_predicate": dict(
                        s.contradiction_burst_by_predicate
                    ),
                    "quarantine_by_agent": dict(s.quarantine_by_agent),
                    "imperative_detections": s.imperative_detections,
                },
                "belief_counts": {
                    "total": bc.total,
                    "trusted": bc.trusted,
                    "quarantined": bc.quarantined,
                    "pending": bc.pending,
                    "inconclusive": bc.inconclusive,
                    "superseded": bc.superseded,
                },
            }

    # ------------------------------------------------------------------
    # DB population
    # ------------------------------------------------------------------

    def load_from_db(
        self,
        conn: Any,  # psycopg.Connection[Any] — avoid import cycle
        tenant_id: UUID,
    ) -> None:
        """Populate metrics from live DB queries (for demo UI health endpoint)."""
        try:
            self._load_screening_lag(conn, tenant_id)
        except Exception as exc:
            logger.warning("metrics_load_screening_lag_failed", error=str(exc))

        try:
            self._load_quarantine_by_reason(conn, tenant_id)
        except Exception as exc:
            logger.warning("metrics_load_quarantine_reason_failed", error=str(exc))

        try:
            self._load_review_queue_depth(conn, tenant_id)
        except Exception as exc:
            logger.warning("metrics_load_review_queue_failed", error=str(exc))

        try:
            self._load_belief_counts(conn, tenant_id)
        except Exception as exc:
            logger.warning("metrics_load_belief_counts_failed", error=str(exc))

    def _load_screening_lag(self, conn: Any, tenant_id: UUID) -> None:
        """Load screening lag and trust score samples from recent beliefs."""
        rows = conn.execute(
            """
            SELECT
                EXTRACT(EPOCH FROM (screened_at - tx_from)) * 1000 AS lag_ms,
                trust_score,
                status
            FROM belief
            WHERE tenant_id = %s
              AND screened_at IS NOT NULL
            ORDER BY tx_from DESC
            LIMIT 500
            """,
            (str(tenant_id),),
        ).fetchall()

        with self._lock:
            for row in rows:
                lag = float(row["lag_ms"]) if row.get("lag_ms") is not None else 0.0
                self.health.screening_lag_ms.append(lag)
                trust = row.get("trust_score")
                if trust is not None:
                    self.integrity.trust_score_samples.append(float(trust))

    def _load_quarantine_by_reason(self, conn: Any, tenant_id: UUID) -> None:
        """Load quarantine counts by reason_code."""
        rows = conn.execute(
            """
            SELECT reason_code, COUNT(*) AS cnt
            FROM quarantine
            WHERE tenant_id = %s
            GROUP BY reason_code
            """,
            (str(tenant_id),),
        ).fetchall()

        with self._lock:
            for row in rows:
                reason = str(row["reason_code"])
                count = int(row["cnt"])
                self.integrity.quarantine_by_reason[reason] = count

    def _load_review_queue_depth(self, conn: Any, tenant_id: UUID) -> None:
        """Load held quarantine queue depth."""
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM quarantine
            WHERE tenant_id = %s AND disposition = 'held'
            """,
            (str(tenant_id),),
        ).fetchone()

        if row is not None:
            with self._lock:
                self.integrity.review_queue_depth = int(row["cnt"])

    def _load_belief_counts(self, conn: Any, tenant_id: UUID) -> None:
        """Load belief counts by status."""
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM belief
            WHERE tenant_id = %s
            GROUP BY status
            """,
            (str(tenant_id),),
        ).fetchall()

        counts = BeliefCounts()
        for row in rows:
            status = str(row["status"])
            cnt = int(row["cnt"])
            counts.total += cnt
            if status == "trusted":
                counts.trusted = cnt
            elif status == "quarantined":
                counts.quarantined = cnt
            elif status == "pending":
                counts.pending = cnt
            elif status == "inconclusive":
                counts.inconclusive = cnt
            elif status == "superseded":
                counts.superseded = cnt

        with self._lock:
            self.belief_counts = counts
