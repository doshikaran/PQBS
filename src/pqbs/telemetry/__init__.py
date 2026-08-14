"""pqbs.telemetry — process-level metrics singleton and supporting utilities."""
from __future__ import annotations

from pqbs.telemetry.metrics import MetricsCollector

_METRICS: MetricsCollector | None = None
_METRICS_LOCK_IMPORT_GUARD = True


def get_metrics() -> MetricsCollector:
    """Return the process-level MetricsCollector singleton.

    Thread-safe: MetricsCollector itself uses a lock on all mutations.
    Initialised on first call; never reset in production.
    """
    global _METRICS
    if _METRICS is None:
        _METRICS = MetricsCollector()
    return _METRICS


def reset_metrics_for_testing() -> None:
    """Replace the singleton with a fresh instance — ONLY for test isolation."""
    global _METRICS
    _METRICS = MetricsCollector()


__all__ = ["get_metrics", "reset_metrics_for_testing", "MetricsCollector"]
