"""A10 — Audit agent wrapper.

Elevated read access (role_auditor). Supports both temporal reconstruction
mechanisms (bitemporal and MVCC) plus attribution and diff queries.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.audit.engine import AuditEngine
from pqbs.contracts import TemporalMechanism, TemporalQuery

logger = structlog.get_logger(__name__)


class AuditAgent:
    """A10 — Audit agent. Elevated read (role_auditor). Both temporal mechanisms."""

    def __init__(self, engine: AuditEngine | None = None) -> None:
        self.engine = engine or AuditEngine()

    def query(
        self,
        temporal_query: TemporalQuery,
        conn: psycopg.Connection[Any],
    ) -> dict[str, Any]:
        """Dispatch a temporal query to the appropriate mechanism.

        BITEMPORAL → unbounded tx_from/tx_to query on the belief table.
        MVCC       → AS OF SYSTEM TIME bounded by GC window (~1h Serverless).
        BACKUP_ANCHORED → not yet implemented (Phase 6.5 / A19).
        AUTO (default) → selects mechanism based on query age.
        """
        if temporal_query.mechanism == TemporalMechanism.BITEMPORAL:
            rows = self.engine.query_bitemporal(temporal_query, conn)
            return {"mechanism": "bitemporal", "beliefs": rows, "count": len(rows)}
        elif temporal_query.mechanism == TemporalMechanism.MVCC:
            return self.engine.query_mvcc(temporal_query, conn)
        elif temporal_query.mechanism == TemporalMechanism.BACKUP_ANCHORED:
            return {
                "error": "backup_anchored not yet implemented",
                "mechanism": "backup_anchored",
                "suggestion": "Phase 6.5 (A19) implements this mechanism",
            }
        else:
            return self.engine.query_auto(temporal_query, conn)

    def get_attribution(
        self,
        belief_id: UUID,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> dict[str, Any]:
        """Return full attribution chain for a belief.

        Returns a dict with keys:
        - belief: core belief + provenance fields
        - quarantine: quarantine record + verdict (empty dict if not quarantined)
        - influenced_queries: up to 20 retrieval_log entries that returned this belief
        """
        return self.engine.get_attribution(belief_id, tenant_id, conn)

    def diff(
        self,
        tenant_id: UUID,
        t1: datetime,
        t2: datetime,
        conn: psycopg.Connection[Any],
        subject_filter: str | None = None,
    ) -> dict[str, Any]:
        """Return bitemporal diff of beliefs between T1 and T2.

        Returns a dict with keys:
        - t1, t2: ISO timestamps
        - added: beliefs present at T2 but not T1
        - removed: beliefs present at T1 but not T2
        - count_t1, count_t2: total counts at each timestamp
        """
        return self.engine.diff_beliefs(tenant_id, t1, t2, conn, subject_filter)
