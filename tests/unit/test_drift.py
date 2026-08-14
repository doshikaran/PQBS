"""Unit tests for A5 DriftAgent.

All tests use mocked DB connections. No live DB required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

from pqbs.agents.integrity.a5_drift import DriftAgent, DriftAlert, DriftScanResult
from pqbs.agents.integrity.audit_sink import AuditSink

_TENANT = UUID("bbbb0000-0000-0000-0000-000000000002")
_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list[dict[str, Any]]) -> MagicMock:
    """Return a mock connection whose execute().fetchall() returns rows."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn.execute.return_value = cursor
    return conn


def _make_agent() -> DriftAgent:
    return DriftAgent()


# ---------------------------------------------------------------------------
# D1 — Contradiction burst
# ---------------------------------------------------------------------------

def test_contradiction_burst_fires_above_threshold() -> None:
    rows = [{"predicate": "works_at", "cnt": 6}]
    conn = _mock_conn(rows)
    agent = _make_agent()

    alerts = agent.detect_contradiction_bursts(_TENANT, conn, threshold=5)

    assert len(alerts) == 1
    assert alerts[0].detection_type == "contradiction_burst"
    assert alerts[0].predicate == "works_at"
    assert alerts[0].detail["contradiction_count"] == 6


def test_contradiction_burst_silent_below_threshold() -> None:
    # DB HAVING clause filters these out — mock returns empty (threshold not reached)
    conn = _mock_conn([])
    agent = _make_agent()

    alerts = agent.detect_contradiction_bursts(_TENANT, conn, threshold=5)

    assert len(alerts) == 0


def test_contradiction_burst_silent_at_threshold_minus_one() -> None:
    # Even if DB somehow returns 4 rows (shouldn't happen with HAVING)
    # the mock simulates the filtered result — empty because threshold=5 not met
    conn = _mock_conn([])
    agent = _make_agent()

    alerts = agent.detect_contradiction_bursts(_TENANT, conn, threshold=5)

    assert alerts == []


# ---------------------------------------------------------------------------
# D2 — Write-character shift
# ---------------------------------------------------------------------------

def test_write_shift_fires_when_recent_rate_3x_baseline() -> None:
    # recent=30 in 24h, baseline=5 in 168h → recent_rate=1.25/h, baseline_rate=0.03/h → ratio≈42x
    rows = [{"author_agent_id": "agent-x", "recent_count": 30, "baseline_count": 5}]
    conn = _mock_conn(rows)
    agent = _make_agent()

    alerts = agent.detect_write_shift(_TENANT, conn, window_hours=24, baseline_hours=168)

    assert len(alerts) == 1
    assert alerts[0].detection_type == "write_shift"
    assert alerts[0].agent_id == "agent-x"
    assert alerts[0].detail["recent_count"] == 30


def test_write_shift_silent_when_rate_normal() -> None:
    # recent=2 in 24h, baseline=10 in 168h → rate not elevated
    # The DB HAVING clause would filter these out, so mock returns empty
    conn = _mock_conn([])
    agent = _make_agent()

    alerts = agent.detect_write_shift(_TENANT, conn, window_hours=24, baseline_hours=168)

    assert len(alerts) == 0


def test_write_shift_silent_when_recent_count_below_3() -> None:
    # Even if ratio > 3x, recent_count < 3 → no alert
    rows = [{"author_agent_id": "agent-y", "recent_count": 2, "baseline_count": 1}]
    conn = _mock_conn(rows)
    agent = _make_agent()

    # recent_rate = 2/24 = 0.083, baseline_rate = 1/168 = 0.006, ratio ≈ 14x
    # But recent_count=2 < 3 → suppressed
    alerts = agent.detect_write_shift(_TENANT, conn, window_hours=24, baseline_hours=168)

    # recent_count=2 < 3 suppresses the alert
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# D3 — Single-origin cluster
# ---------------------------------------------------------------------------

def test_single_origin_cluster_fires() -> None:
    rows = [{"predicate": "is_premium", "source_digest": "abc123", "cnt": 6}]
    conn = _mock_conn(rows)
    agent = _make_agent()

    alerts = agent.detect_single_origin_clusters(_TENANT, conn, min_cluster_size=5)

    assert len(alerts) == 1
    assert alerts[0].detection_type == "single_origin_cluster"
    assert alerts[0].predicate == "is_premium"
    assert alerts[0].detail["cluster_size"] == 6
    assert alerts[0].detail["source_digest"] == "abc123"


def test_single_origin_cluster_silent_below_threshold() -> None:
    # DB HAVING filters these out — mock returns empty
    conn = _mock_conn([])
    agent = _make_agent()

    alerts = agent.detect_single_origin_clusters(_TENANT, conn, min_cluster_size=5)

    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# D4 — Sleeper detection
# ---------------------------------------------------------------------------

def test_sleeper_detected() -> None:
    rows = [
        {
            "author_agent_id": "sleeper-agent",
            "burst_count": 5,
            "last_pre_dormant_write": None,
        }
    ]
    conn = _mock_conn(rows)
    agent = _make_agent()

    alerts = agent.detect_sleepers(
        _TENANT, conn, dormant_days=7, burst_hours=2, burst_threshold=3
    )

    assert len(alerts) == 1
    assert alerts[0].detection_type == "sleeper"
    assert alerts[0].agent_id == "sleeper-agent"
    assert alerts[0].severity == "high"
    assert alerts[0].detail["burst_count"] == 5


def test_sleeper_not_detected_when_no_burst() -> None:
    # DB query filters these out — mock returns empty
    conn = _mock_conn([])
    agent = _make_agent()

    alerts = agent.detect_sleepers(_TENANT, conn)

    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# run_drift_scan
# ---------------------------------------------------------------------------

def test_run_drift_scan_emits_audit_records() -> None:
    """Mock sink — verify emit called once per alert."""
    # Contradiction burst returns 1 alert; all others return empty
    def execute_side_effect(sql: str, params: tuple) -> MagicMock:  # type: ignore[type-arg]
        cursor = MagicMock()
        if "contradiction_event" in sql:
            cursor.fetchall.return_value = [{"predicate": "works_at", "cnt": 6}]
        else:
            cursor.fetchall.return_value = []
        return cursor

    conn = MagicMock()
    conn.execute.side_effect = execute_side_effect

    mock_sink = MagicMock(spec=AuditSink)
    mock_sink.emit.return_value = "/tmp/pqbs_audit/fake-path"

    agent = _make_agent()
    result = agent.run_drift_scan(_TENANT, conn, mock_sink)

    assert result.has_alerts
    assert mock_sink.emit.call_count == 1  # one alert → one emit


def test_run_drift_scan_no_alerts_no_emit() -> None:
    conn = _mock_conn([])  # all detectors return empty
    mock_sink = MagicMock(spec=AuditSink)

    agent = _make_agent()
    result = agent.run_drift_scan(_TENANT, conn, mock_sink)

    assert not result.has_alerts
    mock_sink.emit.assert_not_called()


# ---------------------------------------------------------------------------
# DriftAlert severity
# ---------------------------------------------------------------------------

def test_drift_alert_severity_high_for_burst() -> None:
    """Contradiction burst >= 2x threshold → high severity."""
    rows = [{"predicate": "account_balance", "cnt": 12}]  # threshold=5, 12 >= 10 → high
    conn = _mock_conn(rows)
    agent = _make_agent()

    alerts = agent.detect_contradiction_bursts(_TENANT, conn, threshold=5)

    assert len(alerts) == 1
    assert alerts[0].severity == "high"


def test_drift_alert_severity_medium_for_modest_burst() -> None:
    """5 <= count < 10 with threshold=5 → medium severity."""
    rows = [{"predicate": "account_balance", "cnt": 7}]
    conn = _mock_conn(rows)
    agent = _make_agent()

    alerts = agent.detect_contradiction_bursts(_TENANT, conn, threshold=5)

    assert len(alerts) == 1
    assert alerts[0].severity == "medium"


# ---------------------------------------------------------------------------
# DriftScanResult properties
# ---------------------------------------------------------------------------

def test_drift_scan_result_high_severity_count() -> None:
    alerts = [
        DriftAlert(
            tenant_id=_TENANT,
            detection_type="contradiction_burst",
            predicate="works_at",
            agent_id=None,
            severity="high",
            detail={},
            detected_at=_NOW,
        ),
        DriftAlert(
            tenant_id=_TENANT,
            detection_type="write_shift",
            predicate=None,
            agent_id="agent-z",
            severity="medium",
            detail={},
            detected_at=_NOW,
        ),
    ]
    result = DriftScanResult(
        tenant_id=_TENANT,
        alerts=alerts,
        scanned_at=_NOW,
    )
    assert result.high_severity_count == 1
    assert result.has_alerts is True
