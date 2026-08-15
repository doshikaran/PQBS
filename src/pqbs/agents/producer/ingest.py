"""A1 Ingestion agent.

Validates the author agent, calls Bedrock to extract beliefs from source
content, then for each belief: canonicalize → embed → (serializable retry)
resolve.

Security Invariant 1: All beliefs enter the store with status='pending'.
Security Invariant 7: This agent writes beliefs, it never issues verdicts.
"""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import boto3
import structlog

import psycopg

from pqbs.contracts import (
    CandidateBelief,
    ProvenanceRecord,
    ProvenanceStub,
    ResolutionOutcome,
    Sensitivity,
)
from pqbs.contracts.enums import SourceType, TrustTier
from pqbs.contracts.exceptions import (
    AdmissionRejectedError,
    AdmissionThrottledError,
    BeliefValidationError,
)
from pqbs.agents.producer.a13_admission import AdmissionAgent
from pqbs.agents.semantics.canonicalize import canonicalize
from pqbs.agents.semantics.embed import embed_normalized
from pqbs.agents.semantics.resolve import resolve
from pqbs.substrate.retry import with_serializable_retry
from pqbs.substrate.transaction import begin_serializable, commit, rollback
from pqbs.telemetry import get_metrics

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Bedrock belief extraction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = (
    "You are a belief extraction agent. Extract factual assertions from the "
    "given text as structured triples.\n"
    "Return ONLY valid JSON with this schema:\n"
    '{"beliefs": [{"subject": "string", "predicate": "string", "object": "string", '
    '"confidence": 0.0-1.0, "valid_from": "ISO8601"}]}\n'
    "Extract up to {max_beliefs} beliefs. Only extract clear factual assertions, "
    "not opinions or commands.\n"
    'If no beliefs can be extracted, return {"beliefs": []}.'
)


def _call_bedrock_extraction(
    source_content: str,
    max_beliefs: int,
    region: str,
    model_id: str,
) -> list[dict[str, Any]]:
    """Call Bedrock to extract belief triples. Returns raw list of dicts."""
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(max_beliefs=max_beliefs)
    client = boto3.client("bedrock-runtime", region_name=region)

    if model_id.startswith("anthropic.claude"):
        # Anthropic Messages API format
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": source_content}
            ],
        })
        response = client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        # Claude returns {"content": [{"type": "text", "text": "..."}]}
        raw_text: str = payload["content"][0]["text"]

    elif model_id.startswith("meta.llama"):
        # Llama prompt format
        prompt = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}\n"
            "<|eot_id|><|start_header_id|>user<|end_header_id|>\n"
            f"{source_content}\n"
            "<|eot_id|><|start_header_id|>assistant<|end_header_id|>"
        )
        body = json.dumps({
            "prompt": prompt,
            "max_gen_len": 4096,
            "temperature": 0.0,
        })
        response = client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        raw_text = payload.get("generation", "")
    else:
        # Generic: try Messages API format
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": source_content}
            ],
        })
        response = client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        content = payload.get("content", [])
        if content and isinstance(content, list):
            raw_text = content[0].get("text", "")
        else:
            raw_text = payload.get("completion", "")

    # Parse JSON out of the response text
    try:
        # Strip markdown code fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines if they're fences
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        parsed = json.loads(text)
        return parsed.get("beliefs", [])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "belief_extraction_parse_failed",
            model_id=model_id,
            error=str(exc),
            raw_response=raw_text[:200],
        )
        return []


def _extract_beliefs(
    source_content: str,
    tenant_id: UUID,
    author_agent_id: str,
    provenance_stub: ProvenanceStub,
    max_beliefs: int,
) -> list[CandidateBelief]:
    """Extract CandidateBelief objects from source_content via Bedrock."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    model_id = os.environ.get(
        "BEDROCK_EXTRACTION_MODEL_ID",
        os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
    )

    try:
        raw_beliefs = _call_bedrock_extraction(
            source_content=source_content,
            max_beliefs=max_beliefs,
            region=region,
            model_id=model_id,
        )
    except Exception as exc:
        logger.warning(
            "bedrock_extraction_failed",
            error=str(exc),
            model_id=model_id,
        )
        return []

    now = datetime.now(tz=timezone.utc)
    candidates: list[CandidateBelief] = []

    for raw in raw_beliefs[:max_beliefs]:
        try:
            subject = str(raw["subject"]).strip()
            predicate = str(raw["predicate"]).strip()
            obj = str(raw["object"]).strip()
            confidence = float(raw.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            # Parse valid_from — default to now if missing or unparseable
            raw_valid_from = raw.get("valid_from")
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

            if not subject or not predicate or not obj:
                continue

            candidate = CandidateBelief(
                belief_id=uuid4(),
                tenant_id=tenant_id,
                subject=subject,
                predicate=predicate,
                object=obj,
                confidence=confidence,
                valid_from=valid_from,
                valid_to=None,
                provenance_stub=provenance_stub,
                author_agent_id=author_agent_id,
                sensitivity=Sensitivity.NORMAL,
            )
            candidates.append(candidate)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "belief_construction_failed",
                raw=raw,
                error=str(exc),
            )
            continue

    return candidates


# ---------------------------------------------------------------------------
# Provenance record construction
# ---------------------------------------------------------------------------

def _make_provenance_record(
    provenance_stub: ProvenanceStub,
    tenant_id: UUID,
    source_trust_tier: TrustTier,
) -> ProvenanceRecord:
    """Build a full ProvenanceRecord from a stub."""
    return ProvenanceRecord(
        provenance_id=uuid4(),
        tenant_id=tenant_id,
        source_type=provenance_stub.source_type,
        source_uri=provenance_stub.source_uri,
        source_digest=provenance_stub.source_digest,
        episode_id=provenance_stub.episode_id,
        derived_from=(),
        ingested_at=datetime.now(tz=timezone.utc),
        source_trust_tier=source_trust_tier,
        ingestion_agent_id=provenance_stub.ingestion_agent_id,
    )


# ---------------------------------------------------------------------------
# Transaction function for resolve step
# ---------------------------------------------------------------------------

def _resolve_txn(
    conn: psycopg.Connection[Any],
    embedded_belief: Any,
    provenance_record: ProvenanceRecord,
) -> ResolutionOutcome:
    """Transaction function: begin serializable, resolve, commit."""
    begin_serializable(conn)
    try:
        outcome = resolve(conn, embedded_belief, provenance_record)
        commit(conn)
        return outcome
    except Exception:
        rollback(conn)
        raise


# ---------------------------------------------------------------------------
# Main ingest entrypoint
# ---------------------------------------------------------------------------

def ingest(
    conn: psycopg.Connection[Any],
    source_content: str,
    provenance_stub: ProvenanceStub,
    tenant_id: UUID,
    author_agent_id: str,
    *,
    max_beliefs: int = 20,
) -> list[ResolutionOutcome]:
    """Ingest source_content: extract beliefs, canonicalize, embed, resolve.

    Args:
        conn: Open psycopg3 connection in transactional mode.
        source_content: Raw text to extract beliefs from.
        provenance_stub: Provenance metadata for the source.
        tenant_id: Tenant this belief belongs to.
        author_agent_id: Agent performing the ingestion.
        max_beliefs: Maximum beliefs to extract from source_content.

    Returns:
        List of ResolutionOutcome, one per successfully processed belief.

    Raises:
        BeliefValidationError: If author_agent_id is not active.
        AdmissionRejectedError: If per-agent hourly quota is exhausted.
        AdmissionThrottledError: If screening queue depth is too high (retry later).
    """
    # Step 0: Rate limiting / admission control (A13)
    # Must run before any Bedrock calls to avoid paying extraction cost on rejected writes.
    AdmissionAgent().enforce(author_agent_id, tenant_id, conn)

    # Step 1: Validate author_agent_id is active
    # Use autocommit-safe single read (not inside a txn)
    agent_row = conn.execute(
        """
        SELECT status FROM agent_identity
        WHERE tenant_id = %s AND agent_id = %s
        """,
        (str(tenant_id), author_agent_id),
    ).fetchone()

    if agent_row is None:
        raise BeliefValidationError(
            f"Agent '{author_agent_id}' not found for tenant '{tenant_id}'"
        )
    if agent_row["status"] != "active":
        raise BeliefValidationError(
            f"Agent '{author_agent_id}' has status '{agent_row['status']}', "
            f"must be 'active' to ingest beliefs"
        )

    # Determine source trust tier from agent_identity if available, else default
    # We fetch the agent's class to map to a trust tier default
    agent_class_row = conn.execute(
        """
        SELECT agent_class, trust_multiplier FROM agent_identity
        WHERE tenant_id = %s AND agent_id = %s
        """,
        (str(tenant_id), author_agent_id),
    ).fetchone()

    # Conservative default: unverified for all ingested content
    source_trust_tier = TrustTier.UNVERIFIED

    # Step 2: Extract beliefs from source content via Bedrock
    candidates = _extract_beliefs(
        source_content=source_content,
        tenant_id=tenant_id,
        author_agent_id=author_agent_id,
        provenance_stub=provenance_stub,
        max_beliefs=max_beliefs,
    )

    if not candidates:
        logger.info(
            "ingest_no_beliefs_extracted",
            tenant_id=str(tenant_id),
            author_agent_id=author_agent_id,
            content_length=len(source_content),
        )
        return []

    outcomes: list[ResolutionOutcome] = []

    # Step 3: For each belief: canonicalize → embed → (retry) resolve
    for candidate in candidates:
        try:
            # A11: Canonicalize (reads predicate_policy — cheap, outside main txn)
            normalized = canonicalize(conn, candidate)

            # A12: Embed OUTSIDE the transaction (Bedrock latency)
            embedded = embed_normalized(normalized)

            # Build provenance record (unique per belief)
            provenance_record = _make_provenance_record(
                provenance_stub=provenance_stub,
                tenant_id=tenant_id,
                source_trust_tier=source_trust_tier,
            )

            # A7: Resolve inside a serializable transaction with retry
            write_start = _time.monotonic()
            outcome, retry_count = with_serializable_retry(
                conn,
                _resolve_txn,
                embedded,
                provenance_record,
            )
            write_latency_ms = (_time.monotonic() - write_start) * 1000
            # Rebuild outcome with correct retry_count
            outcome = ResolutionOutcome(
                belief_id=outcome.belief_id,
                tenant_id=outcome.tenant_id,
                resolution=outcome.resolution,
                basis=outcome.basis,
                incumbent_id=outcome.incumbent_id,
                challenger_id=outcome.challenger_id,
                retry_count=retry_count,
                detected_at=outcome.detected_at,
                contradiction_event_id=outcome.contradiction_event_id,
            )
            outcomes.append(outcome)

            try:
                get_metrics().record_write(
                    latency_ms=write_latency_ms,
                    retry_count=retry_count,
                )
            except Exception:
                pass

            logger.info(
                "belief_ingested",
                tenant_id=str(tenant_id),
                belief_id=str(outcome.belief_id),
                subject=candidate.subject,
                predicate=candidate.predicate,
                resolution=outcome.resolution.value,
                retry_count=retry_count,
            )
        except Exception as exc:
            logger.error(
                "belief_ingest_failed",
                tenant_id=str(tenant_id),
                subject=candidate.subject,
                predicate=candidate.predicate,
                error=str(exc),
            )
            # Continue processing remaining beliefs
            continue

    return outcomes
