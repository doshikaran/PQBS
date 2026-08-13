"""A12 Embedding agent.

Embeds text via Amazon Bedrock (amazon.titan-embed-text-v2:0 by default).
The embedding call MUST happen OUTSIDE the DB transaction — Bedrock latency
can be hundreds of milliseconds and holding a serializable transaction open
that long dramatically increases contention and retry rates.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog

from pqbs.contracts import EmbeddedBelief, NormalizedBelief
from pqbs.contracts.base import EMBEDDING_DIM
from pqbs.contracts.exceptions import EmbeddingError

logger = structlog.get_logger(__name__)


def embed_text(
    text: str,
    *,
    region: str | None = None,
    model_id: str | None = None,
) -> tuple[float, ...]:
    """Embed text via Amazon Bedrock. MUST be called BEFORE opening the DB transaction.

    Args:
        text: The text to embed. Passed as 'inputText' to the model.
        region: AWS region. Defaults to AWS_REGION environment variable.
        model_id: Bedrock model ID. Defaults to BEDROCK_EMBEDDING_MODEL_ID env
                  var, or 'amazon.titan-embed-text-v2:0'.

    Returns:
        A tuple of 1024 floats (the embedding vector).

    Raises:
        EmbeddingError: If the Bedrock call fails, the response is malformed,
                        or the embedding dimension is not exactly 1024.
    """
    resolved_region = region or os.environ.get("AWS_REGION")
    if not resolved_region:
        raise EmbeddingError(
            "AWS_REGION environment variable is not set and no region was provided"
        )

    resolved_model_id = (
        model_id
        or os.environ.get("BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    )

    body = json.dumps({
        "inputText": text,
        "dimensions": EMBEDDING_DIM,
        "normalize": True,
    })

    try:
        client = boto3.client("bedrock-runtime", region_name=resolved_region)
        response = client.invoke_model(
            modelId=resolved_model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        raw_body = response["body"].read()
        payload = json.loads(raw_body)
        embedding: list[float] = payload["embedding"]
    except EmbeddingError:
        raise
    except Exception as exc:
        raise EmbeddingError(
            f"Bedrock embedding call failed for model '{resolved_model_id}': {exc}"
        ) from exc

    if len(embedding) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"Expected {EMBEDDING_DIM}-dimensional embedding from Bedrock, "
            f"got {len(embedding)} dimensions (model: {resolved_model_id})"
        )

    logger.debug(
        "embedding_computed",
        model_id=resolved_model_id,
        region=resolved_region,
        dims=len(embedding),
    )

    return tuple(embedding)


def embed_normalized(
    normalized: NormalizedBelief,
    **kwargs: Any,
) -> EmbeddedBelief:
    """Embed the object_normalized field and return an EmbeddedBelief.

    Convenience wrapper that calls embed_text on the normalized object string
    and wraps the result in the EmbeddedBelief contract.

    Args:
        normalized: The NormalizedBelief whose object_normalized to embed.
        **kwargs: Forwarded to embed_text (e.g., region, model_id).

    Returns:
        EmbeddedBelief with the full 1024-dimensional embedding.

    Raises:
        EmbeddingError: Propagated from embed_text.
    """
    embedding = embed_text(normalized.object_normalized, **kwargs)
    return EmbeddedBelief(
        normalized=normalized,
        embedding=embedding,
    )
