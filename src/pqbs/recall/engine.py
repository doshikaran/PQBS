"""A9 — Recall engine.

Structurally restricted to trusted-current beliefs via role_consumer's view
access. Application code adds defence-in-depth but is NOT the primary control.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
import structlog

from pqbs.agents.semantics.embed import embed_text
from pqbs.contracts import RecalledBelief, RecallRequest, RecallResult
from pqbs.telemetry import get_metrics

logger = structlog.get_logger(__name__)


class RecallEngine:
    """
    A9 — Recall engine. Structurally restricted to trusted-current beliefs
    via role_consumer's view access. Application code adds defence-in-depth
    but is NOT the primary control.
    """

    def recall(
        self,
        request: RecallRequest,
        conn: psycopg.Connection[Any],
        *,
        region: str | None = None,
    ) -> RecallResult:
        """Execute a recall query against the trusted-current view.

        Steps:
        1. Embed the query text (same model as write path).
        2. Vector search v_trusted_current with mandatory tenant + trust filters.
        3. Apply temporal context if present.
        4. Fetch provenance for each returned belief.
        5. Log the retrieval.
        6. Return RecallResult with parallel arrays.
        """
        start = time.monotonic()
        retrieval_id = uuid.uuid4()
        retrieved_at = datetime.now(tz=timezone.utc)

        log = logger.bind(
            retrieval_id=str(retrieval_id),
            tenant_id=str(request.tenant_id),
            query_len=len(request.query),
            limit=request.limit,
        )
        log.info("recall_start")

        # Step 2 — embed query (outside DB transaction — Bedrock latency can be high)
        embedding = embed_text(request.query, region=region)
        vector_str = "[" + ",".join(str(x) for x in embedding) + "]"

        # Step 3 — vector search with mandatory filters
        min_score: float | None = (
            request.min_trust_score if request.min_trust_score > 0.0 else None
        )

        temporal_ctx = request.temporal_context
        has_valid_at = temporal_ctx.valid_at is not None
        has_known_at = temporal_ctx.known_at is not None

        sql_parts = [
            """
SELECT
    b.belief_id,
    b.tenant_id,
    b.subject,
    b.predicate,
    b.object,
    b.object_normalized,
    b.confidence,
    b.valid_from,
    b.valid_to,
    b.tx_from,
    b.author_agent_id,
    b.provenance_id,
    b.trust_score,
    b.screened_at
FROM v_trusted_current b
WHERE b.tenant_id = %s
  AND (%s::float IS NULL OR b.trust_score >= %s)
""",
        ]
        params: list[Any] = [
            str(request.tenant_id),
            min_score,
            min_score,
        ]

        if has_valid_at:
            sql_parts.append("  AND b.valid_from <= %s AND (b.valid_to IS NULL OR b.valid_to >= %s)\n")
            params.extend([temporal_ctx.valid_at, temporal_ctx.valid_at])

        if has_known_at:
            sql_parts.append("  AND b.tx_from <= %s\n")
            params.append(temporal_ctx.known_at)

        sql_parts.append("ORDER BY b.embedding <-> %s::vector\nLIMIT %s")
        params.extend([vector_str, request.limit])

        sql = "".join(sql_parts)

        rows = conn.execute(sql, params).fetchall()  # type: ignore[arg-type]

        log.info("recall_rows_fetched", count=len(rows))

        # Step 4 — assemble RecalledBelief objects + fetch provenance
        beliefs: list[RecalledBelief] = []
        provenance_ids: list[UUID] = []
        trust_scores: list[float] = []

        for row in rows:
            prov_id = UUID(str(row["provenance_id"])) if isinstance(row["provenance_id"], str) else row["provenance_id"]
            b = RecalledBelief(
                belief_id=row["belief_id"],
                tenant_id=row["tenant_id"],
                subject=row["subject"],
                predicate=row["predicate"],
                object=row["object"],
                object_normalized=row["object_normalized"],
                confidence=float(row["confidence"]),
                trust_score=float(row["trust_score"]) if row["trust_score"] is not None else 0.0,
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                tx_from=row["tx_from"],
                author_agent_id=row["author_agent_id"],
                provenance_id=prov_id,
            )
            beliefs.append(b)
            provenance_ids.append(prov_id)
            trust_scores.append(b.trust_score)

        # Fetch provenance records (best-effort for attribution enrichment)
        if beliefs:
            prov_uuid_strs = [str(b.provenance_id) for b in beliefs]
            try:
                conn.execute(
                    """
SELECT provenance_id, source_type, source_trust_tier, source_uri, author_agent_id, source_digest
FROM provenance
WHERE provenance_id = ANY(%s::uuid[]) AND tenant_id = %s
""",
                    (prov_uuid_strs, str(request.tenant_id)),
                )
                # result is available but we don't need it for RecallResult;
                # provenance_ids parallel array is already built above.
            except Exception as exc:
                log.warning("recall_provenance_fetch_failed", error=str(exc))

        # Step 5 — log the retrieval
        query_digest = hashlib.sha256(request.query.encode()).hexdigest()
        returned_ids = json.dumps([str(b.belief_id) for b in beliefs])

        try:
            conn.execute(
                """
INSERT INTO retrieval_log (retrieval_id, tenant_id, requesting_agent_id, query_digest, returned_belief_ids, retrieved_at)
VALUES (%s, %s, %s, %s, %s, %s)
""",
                (
                    str(retrieval_id),
                    str(request.tenant_id),
                    "a9-recall",
                    query_digest,
                    returned_ids,
                    retrieved_at,
                ),
            )
            conn.commit()
        except Exception as exc:
            log.error("recall_log_failed", error=str(exc))
            # Best-effort — don't fail the recall just because logging failed.

        elapsed_ms = int((time.monotonic() - start) * 1000)

        result = RecallResult(
            retrieval_id=retrieval_id,
            request_id=request.request_id,
            beliefs=tuple(beliefs),
            provenance_ids=tuple(provenance_ids),
            trust_scores=tuple(trust_scores),
            retrieved_at=retrieved_at,
            query_latency_ms=elapsed_ms,
        )

        log.info(
            "recall_complete",
            returned=len(beliefs),
            latency_ms=elapsed_ms,
        )

        try:
            get_metrics().record_recall(latency_ms=float(elapsed_ms))
        except Exception:
            pass

        return result
