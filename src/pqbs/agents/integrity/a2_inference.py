"""A2 InferenceAgent — derives new beliefs from trusted parents via Bedrock LLM.

Phase 5, E3 — Containment.

Security Invariant 1: Derived beliefs enter with status='pending'.
Security Invariant 7: This agent writes beliefs; it does NOT issue verdicts.

Key constraint: A2 may only derive from trusted beliefs.
If any parent is not 'trusted', InsufficientTrustError is raised before
any derivation occurs. This prevents trust contamination through the
derivation graph.
"""
from __future__ import annotations

import boto3
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog

from pqbs.contracts.exceptions import InsufficientTrustError

logger = structlog.get_logger(__name__)

_INFERENCE_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"

_SYSTEM_PROMPT = (
    "You are a belief inference agent. Given trusted beliefs, derive new factual assertions.\n"
    "Return ONLY valid JSON:\n"
    '{"beliefs": [{"subject": "...", "predicate": "...", "object": "...", '
    '"confidence": 0.0-1.0, "valid_from": "ISO8601"}]}\n'
    "Derive up to 3 new beliefs. Only derive clear logical consequences, not opinions.\n"
    'If no beliefs can be derived, return {"beliefs": []}.'
)


def _call_bedrock_inference(
    parent_context: str,
    region: str,
) -> list[dict[str, Any]]:
    """Call Bedrock Claude to derive belief triples from parent context."""
    client = boto3.client("bedrock-runtime", region_name=region)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": parent_context}
        ],
    })

    response = client.invoke_model(
        modelId=_INFERENCE_MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    raw_text: str = payload["content"][0]["text"]

    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed = json.loads(text)
        return parsed.get("beliefs", [])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "inference_parse_failed",
            error=str(exc),
            raw_response=raw_text[:200],
        )
        return []


class InferenceAgent:
    """A2 — Derives new beliefs from trusted beliefs using Bedrock LLM.

    Provenance records are written with derived_from populated so that
    A6 CascadeAgent can trace the derivation graph.

    Security:
    - derived_from cannot be empty (raises ValueError).
    - All parents must be 'trusted' (raises InsufficientTrustError otherwise).
    - Derived beliefs always enter with status='pending' (Security Invariant 1).
    """

    def infer(
        self,
        parent_belief_ids: list[UUID],
        tenant_id: UUID,
        author_agent_id: str,
        conn: psycopg.Connection[Any],
        *,
        region: str | None = None,
    ) -> list[UUID]:
        """Derive new beliefs from trusted parents.

        Args:
            parent_belief_ids: IDs of the trusted beliefs to derive from.
            tenant_id: Tenant context for the new beliefs.
            author_agent_id: Agent performing the inference.
            conn: Open psycopg3 connection.
            region: AWS region for Bedrock (defaults to AWS_REGION env var).

        Returns:
            List of new belief_ids written to the DB (status='pending').

        Raises:
            ValueError: If parent_belief_ids is empty.
            InsufficientTrustError: If any parent belief is not 'trusted'.
        """
        # Guard: non-empty parent list required
        if not parent_belief_ids:
            raise ValueError(
                "derived_from cannot be empty — A2 requires at least one trusted parent"
            )

        # Verify ALL parents are trusted
        rows = conn.execute(
            """
            SELECT belief_id, status, subject, predicate, object, confidence
            FROM belief
            WHERE belief_id = ANY(%s::uuid[]) AND tenant_id = %s
            """,
            ([str(pid) for pid in parent_belief_ids], str(tenant_id)),
        ).fetchall()

        found_ids = {UUID(str(r["belief_id"])) for r in rows}
        missing = set(parent_belief_ids) - found_ids
        if missing:
            raise InsufficientTrustError(
                f"Parent belief(s) not found for tenant: {missing}"
            )

        for row in rows:
            if row["status"] != "trusted":
                raise InsufficientTrustError(
                    f"Parent belief {row['belief_id']} has status '{row['status']}' — "
                    "A2 may only derive from trusted beliefs"
                )

        # Build context string for Bedrock
        parent_lines = [
            f"- subject: {r['subject']}, predicate: {r['predicate']}, "
            f"object: {r['object']}, confidence: {r['confidence']}"
            for r in rows
        ]
        parent_context = (
            "Trusted beliefs to derive from:\n" + "\n".join(parent_lines)
        )

        # Call Bedrock
        aws_region = region or os.environ.get("AWS_REGION", "us-east-1")
        try:
            derived_beliefs = _call_bedrock_inference(parent_context, aws_region)
        except Exception as exc:
            logger.warning(
                "a2_bedrock_inference_failed",
                tenant_id=str(tenant_id),
                author_agent_id=author_agent_id,
                error=str(exc),
            )
            derived_beliefs = []

        now = datetime.now(tz=timezone.utc)
        written_ids: list[UUID] = []

        derived_from_json = json.dumps([str(pid) for pid in parent_belief_ids])
        # Stable digest for provenance deduplication
        parent_digest = hashlib.sha256(derived_from_json.encode()).hexdigest()

        for derived in derived_beliefs[:3]:  # at most 3
            try:
                subject = str(derived.get("subject", "")).strip()
                predicate = str(derived.get("predicate", "")).strip()
                obj = str(derived.get("object", "")).strip()
                confidence = float(derived.get("confidence", 0.5))
                confidence = max(0.0, min(1.0, confidence))

                if not subject or not predicate or not obj:
                    continue

                # Parse valid_from
                raw_valid_from = derived.get("valid_from")
                if raw_valid_from:
                    try:
                        from dateutil import parser as dp  # type: ignore[import-untyped]
                        valid_from = dp.parse(str(raw_valid_from))
                        if valid_from.tzinfo is None:
                            valid_from = valid_from.replace(tzinfo=timezone.utc)
                    except Exception:
                        valid_from = now
                else:
                    valid_from = now

                provenance_id = uuid4()
                belief_id = uuid4()

                # Insert provenance with derived_from populated
                conn.execute(
                    """
                    INSERT INTO provenance
                        (provenance_id, tenant_id, source_type, source_trust_tier,
                         author_agent_id, source_digest, derived_from)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(provenance_id),
                        str(tenant_id),
                        "agent_inference",
                        "unverified",
                        author_agent_id,
                        parent_digest,
                        derived_from_json,
                    ),
                )

                # Insert belief with status=pending (Security Invariant 1)
                conn.execute(
                    """
                    INSERT INTO belief
                        (belief_id, tenant_id, subject, predicate, object,
                         object_normalized, confidence, valid_from, status,
                         author_agent_id, provenance_id, sensitivity, tx_from)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, 'normal', now())
                    """,
                    (
                        str(belief_id),
                        str(tenant_id),
                        subject,
                        predicate,
                        obj,
                        obj.lower(),
                        confidence,
                        valid_from,
                        author_agent_id,
                        str(provenance_id),
                    ),
                )

                written_ids.append(belief_id)
                logger.info(
                    "a2_belief_derived",
                    belief_id=str(belief_id),
                    tenant_id=str(tenant_id),
                    subject=subject,
                    predicate=predicate,
                )

            except Exception as exc:
                logger.error(
                    "a2_belief_write_failed",
                    tenant_id=str(tenant_id),
                    error=str(exc),
                )
                continue

        return written_ids
