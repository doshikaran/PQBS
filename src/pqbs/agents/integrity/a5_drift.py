"""
A5 — Drift Detection Agent.

Scheduled population-level analysis. Detects patterns that per-write
screening structurally cannot see (burst patterns over time windows,
agent write-character shift, corroboration clusters from one origin).

Authority: MAY NOT quarantine directly. Can only:
  - Request re-screening of suspicious beliefs (reset status to pending)
  - Adjust trust_multiplier on agent_identity rows
  - Emit audit records for detected anomalies
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.contracts import AuditRecord, AuditEventType
from pqbs.agents.integrity.audit_sink import AuditSink, emit_or_block

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# DriftAlert dataclass
# ---------------------------------------------------------------------------

@dataclass
class DriftAlert:
    tenant_id: UUID
    detection_type: str  # "contradiction_burst", "write_shift", "single_origin_cluster", "sleeper"
    predicate: str | None
    agent_id: str | None
    severity: str  # "low", "medium", "high"
    detail: dict[str, Any]
    detected_at: datetime


# ---------------------------------------------------------------------------
# DriftScanResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class DriftScanResult:
    tenant_id: UUID
    alerts: list[DriftAlert]
    scanned_at: datetime

    @property
    def has_alerts(self) -> bool:
        return len(self.alerts) > 0

    @property
    def high_severity_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == "high")


# ---------------------------------------------------------------------------
# DriftAgent
# ---------------------------------------------------------------------------

class DriftAgent:
    """A5 — Drift Detection Agent.

    Runs population-level detection algorithms that per-write screening
    cannot see. Emits POSTURE_DRIFT_DETECTED audit records for each alert.
    Does NOT quarantine directly.
    """

    # ------------------------------------------------------------------
    # D1 — Contradiction burst by predicate
    # ------------------------------------------------------------------

    def detect_contradiction_bursts(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        *,
        window_seconds: int = 3600,
        threshold: int = 5,
    ) -> list[DriftAlert]:
        """Count contradiction_events per predicate in the last window_seconds.

        If any predicate has >= threshold contradictions: DriftAlert with
        severity "high" (>= 2x threshold) or "medium" (threshold..2x).
        """
        sql = """
            SELECT predicate, COUNT(*) AS cnt
            FROM contradiction_event
            WHERE tenant_id = %s
              AND occurred_at >= now() - (%s * interval '1 second')
            GROUP BY predicate
            HAVING COUNT(*) >= %s
        """
        rows = conn.execute(sql, (str(tenant_id), window_seconds, threshold)).fetchall()

        alerts: list[DriftAlert] = []
        now = datetime.now(tz=timezone.utc)
        for row in rows:
            count = int(row["cnt"])
            predicate = str(row["predicate"])
            severity = "high" if count >= threshold * 2 else "medium"
            alerts.append(
                DriftAlert(
                    tenant_id=tenant_id,
                    detection_type="contradiction_burst",
                    predicate=predicate,
                    agent_id=None,
                    severity=severity,
                    detail={
                        "predicate": predicate,
                        "contradiction_count": count,
                        "window_seconds": window_seconds,
                        "threshold": threshold,
                    },
                    detected_at=now,
                )
            )

        logger.debug(
            "drift_contradiction_burst_check",
            tenant_id=str(tenant_id),
            alerts_found=len(alerts),
        )
        return alerts

    # ------------------------------------------------------------------
    # D2 — Agent write-character shift
    # ------------------------------------------------------------------

    def detect_write_shift(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        *,
        window_hours: int = 24,
        baseline_hours: int = 168,  # 7 days
    ) -> list[DriftAlert]:
        """Compare recent write volume (last window_hours) to baseline (baseline_hours).

        Flag agents where recent_rate > 3x baseline_rate and recent_count >= 3.
        """
        sql = """
            SELECT
                author_agent_id,
                COUNT(*) FILTER (WHERE tx_from >= now() - (%s * interval '1 hour')) AS recent_count,
                COUNT(*) FILTER (WHERE tx_from >= now() - (%s * interval '1 hour')) AS baseline_count
            FROM belief
            WHERE tenant_id = %s
            GROUP BY author_agent_id
            HAVING COUNT(*) FILTER (WHERE tx_from >= now() - (%s * interval '1 hour')) > 0
        """
        rows = conn.execute(
            sql, (window_hours, baseline_hours, str(tenant_id), window_hours)
        ).fetchall()

        alerts: list[DriftAlert] = []
        now = datetime.now(tz=timezone.utc)
        for row in rows:
            recent_count = int(row["recent_count"])
            baseline_count = int(row["baseline_count"])
            agent_id = str(row["author_agent_id"])

            if baseline_count == 0:
                # No baseline → cannot compute shift
                continue

            recent_rate = recent_count / window_hours
            baseline_rate = baseline_count / baseline_hours

            if baseline_rate == 0:
                continue

            if recent_rate > 3 * baseline_rate and recent_count >= 3:
                ratio = recent_rate / baseline_rate
                severity = "high" if ratio >= 10 else "medium"
                alerts.append(
                    DriftAlert(
                        tenant_id=tenant_id,
                        detection_type="write_shift",
                        predicate=None,
                        agent_id=agent_id,
                        severity=severity,
                        detail={
                            "author_agent_id": agent_id,
                            "recent_count": recent_count,
                            "baseline_count": baseline_count,
                            "recent_rate_per_hour": round(recent_rate, 4),
                            "baseline_rate_per_hour": round(baseline_rate, 4),
                            "ratio": round(ratio, 2),
                            "window_hours": window_hours,
                            "baseline_hours": baseline_hours,
                        },
                        detected_at=now,
                    )
                )

        logger.debug(
            "drift_write_shift_check",
            tenant_id=str(tenant_id),
            alerts_found=len(alerts),
        )
        return alerts

    # ------------------------------------------------------------------
    # D3 — Corroboration cluster from single origin
    # ------------------------------------------------------------------

    def detect_single_origin_clusters(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        *,
        min_cluster_size: int = 5,
    ) -> list[DriftAlert]:
        """Find predicates where >= min_cluster_size beliefs share a single source_digest.

        Single-origin corroboration is a T4 threat signal.
        """
        sql = """
            SELECT b.predicate, p.source_digest, COUNT(*) AS cnt
            FROM belief b
            JOIN provenance p ON p.provenance_id = b.provenance_id AND p.tenant_id = b.tenant_id
            WHERE b.tenant_id = %s
              AND b.status = 'trusted'
              AND p.source_digest IS NOT NULL
            GROUP BY b.predicate, p.source_digest
            HAVING COUNT(*) >= %s
        """
        rows = conn.execute(sql, (str(tenant_id), min_cluster_size)).fetchall()

        alerts: list[DriftAlert] = []
        now = datetime.now(tz=timezone.utc)
        for row in rows:
            count = int(row["cnt"])
            predicate = str(row["predicate"])
            source_digest = str(row["source_digest"])
            severity = "high" if count >= min_cluster_size * 3 else "medium"
            alerts.append(
                DriftAlert(
                    tenant_id=tenant_id,
                    detection_type="single_origin_cluster",
                    predicate=predicate,
                    agent_id=None,
                    severity=severity,
                    detail={
                        "predicate": predicate,
                        "source_digest": source_digest,
                        "cluster_size": count,
                        "min_cluster_size": min_cluster_size,
                    },
                    detected_at=now,
                )
            )

        logger.debug(
            "drift_single_origin_check",
            tenant_id=str(tenant_id),
            alerts_found=len(alerts),
        )
        return alerts

    # ------------------------------------------------------------------
    # D4 — Sleeper detection (temporally delayed surface)
    # ------------------------------------------------------------------

    def detect_sleepers(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        *,
        dormant_days: int = 7,
        burst_hours: int = 2,
        burst_threshold: int = 3,
    ) -> list[DriftAlert]:
        """Agents with no writes for dormant_days suddenly writing burst_threshold+
        beliefs in burst_hours — T3 sleeper pattern.
        """
        sql = """
            WITH agent_recent AS (
                SELECT
                    author_agent_id,
                    COUNT(*) FILTER (
                        WHERE tx_from >= now() - (%s * interval '1 hour')
                    ) AS burst_count,
                    MAX(tx_from) FILTER (
                        WHERE tx_from < now() - (%s * interval '1 hour')
                          AND tx_from < now() - (%s * interval '1 day')
                    ) AS last_pre_dormant_write
                FROM belief
                WHERE tenant_id = %s
                GROUP BY author_agent_id
            )
            SELECT author_agent_id, burst_count, last_pre_dormant_write
            FROM agent_recent
            WHERE burst_count >= %s
              AND (
                last_pre_dormant_write IS NULL
                OR last_pre_dormant_write < now() - (%s * interval '1 day')
              )
        """
        rows = conn.execute(
            sql,
            (
                burst_hours,
                burst_hours,
                dormant_days,
                str(tenant_id),
                burst_threshold,
                dormant_days,
            ),
        ).fetchall()

        alerts: list[DriftAlert] = []
        now = datetime.now(tz=timezone.utc)
        for row in rows:
            agent_id = str(row["author_agent_id"])
            burst_count = int(row["burst_count"])
            last_write = row.get("last_pre_dormant_write")
            alerts.append(
                DriftAlert(
                    tenant_id=tenant_id,
                    detection_type="sleeper",
                    predicate=None,
                    agent_id=agent_id,
                    severity="high",
                    detail={
                        "author_agent_id": agent_id,
                        "burst_count": burst_count,
                        "burst_hours": burst_hours,
                        "dormant_days": dormant_days,
                        "last_pre_dormant_write": (
                            last_write.isoformat() if last_write else None
                        ),
                    },
                    detected_at=now,
                )
            )

        logger.debug(
            "drift_sleeper_check",
            tenant_id=str(tenant_id),
            alerts_found=len(alerts),
        )
        return alerts

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    def run_drift_scan(
        self,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
        audit_sink: AuditSink,
    ) -> DriftScanResult:
        """Run all four detectors. Emit audit record for each alert found."""
        logger.info("drift_scan_started", tenant_id=str(tenant_id))

        alerts: list[DriftAlert] = []
        alerts += self.detect_contradiction_bursts(tenant_id, conn)
        alerts += self.detect_write_shift(tenant_id, conn)
        alerts += self.detect_single_origin_clusters(tenant_id, conn)
        alerts += self.detect_sleepers(tenant_id, conn)

        for alert in alerts:
            audit_record = AuditRecord(
                event_type=AuditEventType.POSTURE_DRIFT_DETECTED,
                agent_id="a5-drift",
                tenant_id=alert.tenant_id,
                timestamp=alert.detected_at,
                before=None,
                after={
                    "detection_type": alert.detection_type,
                    "severity": alert.severity,
                    **{k: v for k, v in alert.detail.items() if isinstance(v, (str, int, float, bool)) or v is None},
                },
                reason=f"Drift detected: {alert.detection_type}",
            )
            emit_or_block(audit_sink, audit_record)

        result = DriftScanResult(
            tenant_id=tenant_id,
            alerts=alerts,
            scanned_at=datetime.now(tz=timezone.utc),
        )

        logger.info(
            "drift_scan_completed",
            tenant_id=str(tenant_id),
            total_alerts=len(alerts),
            high_severity=result.high_severity_count,
        )
        return result
