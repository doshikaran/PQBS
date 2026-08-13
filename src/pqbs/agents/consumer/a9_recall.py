"""A9 — Recall agent wrapper.

Thin agent wrapper that wires RecallEngine to the connection.
Read-only; uses role_consumer connection.
"""
from __future__ import annotations

from typing import Any

import psycopg
import structlog

from pqbs.contracts import RecallRequest, RecallResult
from pqbs.recall.engine import RecallEngine

logger = structlog.get_logger(__name__)


class RecallAgent:
    """A9 — Recall agent. Read-only; uses role_consumer connection."""

    def __init__(self, engine: RecallEngine | None = None) -> None:
        self.engine = engine or RecallEngine()

    def recall(
        self,
        request: RecallRequest,
        conn: psycopg.Connection[Any],
        *,
        region: str | None = None,
    ) -> RecallResult:
        """Execute a recall query and return trusted-current beliefs.

        Args:
            request: RecallRequest with query text, tenant_id, limit, filters.
            conn: A psycopg connection. Should be connected as role_consumer
                  for structural enforcement of Security Invariant #2.
            region: AWS region for Bedrock embedding call.

        Returns:
            RecallResult with parallel belief/provenance_id/trust_score arrays.
        """
        return self.engine.recall(request, conn, region=region)
