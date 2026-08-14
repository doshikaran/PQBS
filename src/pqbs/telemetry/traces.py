"""
Belief lifecycle traces — span from ingestion to first retrieval.
Uses structlog for structured trace emission (no external tracing backend needed for demo).
Each trace is a dict with a trace_id that can be correlated across log lines.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generator

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# TraceSpan
# ---------------------------------------------------------------------------

@dataclass
class TraceSpan:
    span_name: str
    trace_id: str
    belief_id: str | None
    started_at: float  # perf_counter
    ended_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at) * 1000


# ---------------------------------------------------------------------------
# BeliefLifecycleTracer
# ---------------------------------------------------------------------------

class BeliefLifecycleTracer:
    """
    Traces the full belief lifecycle: ingest → canonicalize → embed → commit → screen → recall.
    Emits structured log lines at each span boundary.
    """

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or str(uuid.uuid4())
        self.spans: list[TraceSpan] = []
        self._started_at = datetime.now(tz=timezone.utc)

    @contextmanager
    def span(
        self,
        name: str,
        belief_id: str | None = None,
        **metadata: Any,
    ) -> Generator[TraceSpan, None, None]:
        """Context manager that records a named span.

        Usage:
            with tracer.span("ingest", belief_id=str(bid)) as s:
                do_work()
        """
        s = TraceSpan(
            span_name=name,
            trace_id=self.trace_id,
            belief_id=belief_id,
            started_at=time.perf_counter(),
            metadata=metadata,
        )
        try:
            yield s
        finally:
            s.ended_at = time.perf_counter()
            self.spans.append(s)
            logger.info(
                "trace_span",
                trace_id=self.trace_id,
                span=name,
                belief_id=belief_id,
                duration_ms=round(s.duration_ms, 2),
                **metadata,
            )

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of all spans in this trace."""
        return {
            "trace_id": self.trace_id,
            "started_at": self._started_at.isoformat(),
            "spans": [
                {
                    "name": s.span_name,
                    "duration_ms": round(s.duration_ms, 2),
                    "belief_id": s.belief_id,
                    "metadata": s.metadata,
                }
                for s in self.spans
            ],
            "total_ms": round(sum(s.duration_ms for s in self.spans), 2),
            "span_count": len(self.spans),
        }
