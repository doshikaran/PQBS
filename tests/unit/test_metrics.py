"""Unit tests for MetricsCollector (A17 Telemetry).

No external dependencies — all in-process.
"""
from __future__ import annotations

import json
import threading
from uuid import uuid4

import pytest

from pqbs.telemetry.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# record_write
# ---------------------------------------------------------------------------

def test_record_write_updates_health() -> None:
    c = MetricsCollector()
    c.record_write(latency_ms=42.0, retry_count=0)

    assert c.health.write_latency_ms == [42.0]
    assert c.health.retry_total_attempts == 1
    assert c.health.retry_count == 0


def test_record_write_with_retries() -> None:
    c = MetricsCollector()
    c.record_write(latency_ms=100.0, retry_count=2)

    assert len(c.health.write_latency_ms) == 1
    assert c.health.retry_count == 2
    assert c.health.retry_total_attempts == 3  # 1 + 2 retries


# ---------------------------------------------------------------------------
# retry_rate
# ---------------------------------------------------------------------------

def test_retry_rate_calculation() -> None:
    c = MetricsCollector()
    # 2 retries across 10 total attempts
    for _ in range(8):
        c.record_write(latency_ms=10.0, retry_count=0)
    c.record_write(latency_ms=50.0, retry_count=1)
    c.record_write(latency_ms=80.0, retry_count=1)

    # retry_count=2, retry_total_attempts=10
    assert c.health.retry_total_attempts == 12  # 8*1 + 1*(1+1) + 1*(1+1)
    # 8 writes with 0 retries → 8 attempts; 2 writes with 1 retry each → 2*2=4 attempts; total=12
    # retry_count = 2
    assert c.health.retry_count == 2
    assert abs(c.health.retry_rate - 2 / 12) < 1e-9


def test_retry_rate_zero_when_no_attempts() -> None:
    c = MetricsCollector()
    assert c.health.retry_rate == 0.0


# ---------------------------------------------------------------------------
# p50 / p99
# ---------------------------------------------------------------------------

def test_p50_p99_calculation() -> None:
    c = MetricsCollector()
    values = list(range(1, 101))  # 1..100
    p50 = c.health.p50(values)
    p99 = c.health.p99(values)

    # p50 index = int(100 * 0.50) = 50 → values[50] = 51
    assert p50 == 51
    # p99 index = int(100 * 0.99) = 99 → values[99] = 100
    assert p99 == 100


def test_p50_empty_list() -> None:
    c = MetricsCollector()
    assert c.health.p50([]) == 0.0
    assert c.health.p99([]) == 0.0


def test_p50_single_element() -> None:
    c = MetricsCollector()
    assert c.health.p50([42.0]) == 42.0
    assert c.health.p99([42.0]) == 42.0


# ---------------------------------------------------------------------------
# record_screening
# ---------------------------------------------------------------------------

def test_record_screening_updates_quarantine_by_reason() -> None:
    c = MetricsCollector()
    c.record_screening(lag_ms=200.0, verdict="quarantined", trust_score=0.3, reason_code="anomalous_embedding")
    c.record_screening(lag_ms=150.0, verdict="quarantined", trust_score=0.2, reason_code="anomalous_embedding")
    c.record_screening(lag_ms=100.0, verdict="trusted", trust_score=0.9, reason_code=None)

    assert c.integrity.quarantine_by_reason["anomalous_embedding"] == 2
    assert "trusted" not in c.integrity.quarantine_by_reason


def test_record_screening_updates_screening_lag() -> None:
    c = MetricsCollector()
    c.record_screening(lag_ms=300.0, verdict="trusted", trust_score=0.85)

    assert 300.0 in c.health.screening_lag_ms


def test_record_screening_inconclusive_increments_count() -> None:
    c = MetricsCollector()
    c.record_screening(lag_ms=100.0, verdict="inconclusive", trust_score=0.5)

    assert c.integrity.inconclusive_count == 1


def test_record_screening_updates_trust_score_samples() -> None:
    c = MetricsCollector()
    c.record_screening(lag_ms=50.0, verdict="trusted", trust_score=0.88)
    c.record_screening(lag_ms=60.0, verdict="quarantined", trust_score=0.15, reason_code="imperative_content")

    assert 0.88 in c.integrity.trust_score_samples
    assert 0.15 in c.integrity.trust_score_samples


# ---------------------------------------------------------------------------
# record_cascade
# ---------------------------------------------------------------------------

def test_record_cascade_appends_depth() -> None:
    c = MetricsCollector()
    c.record_cascade(depth=3)
    c.record_cascade(depth=7)

    assert c.integrity.cascade_depths == [3, 7]


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def test_snapshot_is_json_serializable() -> None:
    c = MetricsCollector()
    c.record_write(latency_ms=55.5, retry_count=1)
    c.record_screening(lag_ms=200.0, verdict="quarantined", trust_score=0.25, reason_code="untrusted_source")
    c.record_recall(latency_ms=120.0)
    c.record_cascade(depth=2)

    snap = c.snapshot()
    # Must not raise
    serialized = json.dumps(snap)
    assert "health" in serialized
    assert "integrity" in serialized
    assert "security" in serialized
    assert "belief_counts" in serialized


def test_snapshot_health_fields_present() -> None:
    c = MetricsCollector()
    snap = c.snapshot()
    h = snap["health"]
    assert "write_latency_p50_ms" in h
    assert "write_latency_p99_ms" in h
    assert "screening_lag_p50_ms" in h
    assert "screening_lag_p99_ms" in h
    assert "retry_rate" in h


def test_snapshot_integrity_fields_present() -> None:
    c = MetricsCollector()
    snap = c.snapshot()
    i = snap["integrity"]
    assert "quarantine_by_reason" in i
    assert "review_queue_depth" in i
    assert "trust_score_p50" in i


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_record_writes_thread_safe() -> None:
    c = MetricsCollector()
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(50):
                c.record_write(latency_ms=10.0, retry_count=0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(c.health.write_latency_ms) == 200  # 4 threads * 50 writes
