"""A10 — Audit engine.

Two temporal reconstruction mechanisms and attribution queries.
Has elevated read authority (role_auditor) — can read quarantined and historical beliefs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.contracts import TemporalMechanism, TemporalQuery

logger = structlog.get_logger(__name__)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a psycopg dict-row to a plain dict with serializable values."""
    if row is None:
        return {}
    result: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


class AuditEngine:
    """
    A10 — Audit engine with two temporal reconstruction mechanisms and attribution queries.
    Has elevated read authority (role_auditor) — can read quarantined and historical beliefs.
    """

    def query_bitemporal(
        self,
        query: TemporalQuery,
        conn: psycopg.Connection[Any],
    ) -> list[dict[str, Any]]:
        """Mechanism 1 — unbounded bitemporal query. Works arbitrarily far back.

        Uses the tx_from / tx_to columns written at commit time. Any point in
        history is queryable regardless of MVCC GC retention.
        """
        log = logger.bind(
            mechanism="bitemporal",
            tenant_id=str(query.tenant_id),
            as_of=query.as_of.isoformat(),
        )
        log.info("audit_bitemporal_start")

        as_of = query.as_of

        sql = """
SELECT b.*, p.source_type, p.source_trust_tier, p.source_uri, p.author_agent_id
FROM belief b
LEFT JOIN provenance p ON p.provenance_id = b.provenance_id AND p.tenant_id = b.tenant_id
WHERE b.tenant_id = %s
  AND b.tx_from <= %s
  AND (b.tx_to IS NULL OR b.tx_to > %s)
"""
        params: list[Any] = [str(query.tenant_id), as_of, as_of]

        if query.subject_filter is not None:
            sql += "  AND b.subject = %s\n"
            params.append(query.subject_filter)

        if query.predicate_filter is not None:
            sql += "  AND b.predicate = %s\n"
            params.append(query.predicate_filter)

        sql += "ORDER BY b.tx_from"

        rows = conn.execute(sql, params).fetchall()
        result = [_row_to_dict(r) for r in rows]
        log.info("audit_bitemporal_complete", count=len(result))
        return result

    def query_mvcc(
        self,
        query: TemporalQuery,
        conn: psycopg.Connection[Any],
    ) -> dict[str, Any]:
        """Mechanism 2 — MVCC AS OF SYSTEM TIME. Bounded by GC retention window.

        On CockroachDB Serverless, the GC retention window is typically ~1 hour.
        Timestamps beyond that window will receive a graceful error dict rather
        than raising an exception.
        """
        log = logger.bind(
            mechanism="mvcc",
            tenant_id=str(query.tenant_id),
            as_of=query.as_of.isoformat(),
        )
        log.info("audit_mvcc_start")

        as_of = query.as_of
        as_of_iso = as_of.isoformat()

        # CockroachDB AS OF SYSTEM TIME accepts ISO8601 string literals.
        # We embed it directly in the SQL (not as a param) because psycopg
        # does not support parameterised AS OF SYSTEM TIME clauses.
        # The value is an isoformat datetime string — no user input reaches here raw.
        sql = f"""
SELECT b.*, p.source_type, p.source_trust_tier
FROM belief AS OF SYSTEM TIME '{as_of_iso}'  b
LEFT JOIN provenance AS OF SYSTEM TIME '{as_of_iso}' p
       ON p.provenance_id = b.provenance_id AND p.tenant_id = b.tenant_id
WHERE b.tenant_id = %s
"""
        params: list[Any] = [str(query.tenant_id)]

        if query.subject_filter is not None:
            sql += "  AND b.subject = %s\n"
            params.append(query.subject_filter)

        if query.predicate_filter is not None:
            sql += "  AND b.predicate = %s\n"
            params.append(query.predicate_filter)

        try:
            rows = conn.execute(sql, params).fetchall()  # type: ignore[arg-type]
        except Exception as exc:
            error_msg = str(exc).lower()
            if (
                "gc threshold" in error_msg
                or "as of system time" in error_msg
                or "cannot read" in error_msg
                or "timestamp" in error_msg
                or "history" in error_msg
            ):
                log.warning("audit_mvcc_gc_exceeded", error=str(exc))
                return {
                    "error": "MVCC window exceeded",
                    "mechanism": "mvcc",
                    "as_of": query.as_of.isoformat(),
                    "detail": str(exc),
                    "suggestion": (
                        "Use mechanism='bitemporal' for timestamps beyond the "
                        "MVCC retention window (typically ~1 hour on CockroachDB Serverless)"
                    ),
                }
            raise

        result_rows = [_row_to_dict(r) for r in rows]
        log.info("audit_mvcc_complete", count=len(result_rows))
        return {
            "mechanism": "mvcc",
            "as_of": query.as_of.isoformat(),
            "beliefs": result_rows,
            "count": len(result_rows),
        }

    def query_auto(
        self,
        query: TemporalQuery,
        conn: psycopg.Connection[Any],
    ) -> dict[str, Any]:
        """Select best mechanism based on query timestamp vs MVCC window.

        Heuristic:
        - as_of within last 24 hours → MVCC (faster, uses CockroachDB MVCC)
        - older → bitemporal (uses tx_from/tx_to columns, unbounded)
        """
        now = datetime.now(tz=timezone.utc)
        # Normalise as_of to tz-aware
        as_of = query.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        age = now - as_of
        use_mvcc = age < timedelta(hours=24)

        log = logger.bind(
            mechanism_chosen="mvcc" if use_mvcc else "bitemporal",
            age_seconds=int(age.total_seconds()),
        )
        log.info("audit_auto_mechanism_selected")

        if use_mvcc:
            result = self.query_mvcc(query, conn)
            # If MVCC returned an error dict, fall back to bitemporal
            if isinstance(result, dict) and "error" in result:
                log.info("audit_auto_mvcc_fallback_to_bitemporal")
                rows = self.query_bitemporal(query, conn)
                return {
                    "mechanism_used": "bitemporal",
                    "fallback_reason": "mvcc_gc_exceeded",
                    "beliefs": rows,
                    "count": len(rows),
                }
            result["mechanism_used"] = "mvcc"
            return result
        else:
            rows = self.query_bitemporal(query, conn)
            return {
                "mechanism_used": "bitemporal",
                "as_of": query.as_of.isoformat(),
                "beliefs": rows,
                "count": len(rows),
            }

    def get_attribution(
        self,
        belief_id: UUID,
        tenant_id: UUID,
        conn: psycopg.Connection[Any],
    ) -> dict[str, Any]:
        """Full attribution chain: who wrote it, why quarantined, what it influenced."""
        log = logger.bind(belief_id=str(belief_id), tenant_id=str(tenant_id))
        log.info("audit_attribution_start")

        # Who wrote it
        belief_rows = conn.execute(
            """
SELECT b.belief_id, b.subject, b.predicate, b.object, b.status,
       b.author_agent_id, b.tx_from, b.screened_at, b.confidence, b.trust_score,
       p.source_type, p.source_trust_tier, p.source_uri, p.source_digest,
       p.derived_from
FROM belief b
LEFT JOIN provenance p ON p.provenance_id = b.provenance_id AND p.tenant_id = b.tenant_id
WHERE b.belief_id = %s AND b.tenant_id = %s
""",
            (str(belief_id), str(tenant_id)),
        ).fetchall()

        belief = _row_to_dict(belief_rows[0]) if belief_rows else {}

        # Why quarantined (if applicable)
        quarantine_rows = conn.execute(
            """
SELECT q.reason_code, q.quarantined_at, q.disposition, q.reviewed_by,
       iv.verdict, iv.trust_score, iv.signal_scores, iv.screened_at
FROM quarantine q
LEFT JOIN integrity_verdict iv ON iv.belief_id = q.belief_id AND iv.tenant_id = q.tenant_id
WHERE q.belief_id = %s AND q.tenant_id = %s
""",
            (str(belief_id), str(tenant_id)),
        ).fetchall()

        quarantine = _row_to_dict(quarantine_rows[0]) if quarantine_rows else {}

        # What queries returned it
        influenced_rows = conn.execute(
            """
SELECT rl.retrieval_id, rl.requesting_agent_id, rl.query_digest, rl.retrieved_at
FROM retrieval_log rl
WHERE rl.tenant_id = %s
  AND rl.returned_belief_ids @> %s::jsonb
ORDER BY rl.retrieved_at DESC
LIMIT 20
""",
            (str(tenant_id), json.dumps([str(belief_id)])),
        ).fetchall()

        influenced_queries = [_row_to_dict(r) for r in influenced_rows]

        log.info(
            "audit_attribution_complete",
            has_belief=bool(belief),
            has_quarantine=bool(quarantine),
            influenced_count=len(influenced_queries),
        )

        return {
            "belief": belief,
            "quarantine": quarantine,
            "influenced_queries": influenced_queries,
        }

    def diff_beliefs(
        self,
        tenant_id: UUID,
        t1: datetime,
        t2: datetime,
        conn: psycopg.Connection[Any],
        subject_filter: str | None = None,
    ) -> dict[str, Any]:
        """What changed between T1 and T2? Bitemporal diff."""
        log = logger.bind(
            tenant_id=str(tenant_id),
            t1=t1.isoformat(),
            t2=t2.isoformat(),
        )
        log.info("audit_diff_start")

        q1 = TemporalQuery(
            tenant_id=tenant_id,
            as_of=t1,
            mechanism=TemporalMechanism.BITEMPORAL,
            requesting_agent_id="a10-diff",
            subject_filter=subject_filter,
        )
        q2 = TemporalQuery(
            tenant_id=tenant_id,
            as_of=t2,
            mechanism=TemporalMechanism.BITEMPORAL,
            requesting_agent_id="a10-diff",
            subject_filter=subject_filter,
        )

        at_t1 = self.query_bitemporal(q1, conn)
        at_t2 = self.query_bitemporal(q2, conn)

        t1_ids = {r["belief_id"] for r in at_t1}
        t2_ids = {r["belief_id"] for r in at_t2}

        added = [r for r in at_t2 if r["belief_id"] not in t1_ids]
        removed = [r for r in at_t1 if r["belief_id"] not in t2_ids]

        log.info(
            "audit_diff_complete",
            count_t1=len(at_t1),
            count_t2=len(at_t2),
            added=len(added),
            removed=len(removed),
        )

        return {
            "t1": t1.isoformat(),
            "t2": t2.isoformat(),
            "added": added,
            "removed": removed,
            "count_t1": len(at_t1),
            "count_t2": len(at_t2),
        }
