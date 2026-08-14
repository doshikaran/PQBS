"""S3 — Imperative Content Signal.

Detects beliefs whose object field contains imperative/directive language
rather than factual assertions. Uses Amazon Bedrock Claude for classification
when trigger phrases are found.
Weight: 0.25 (highest — most legible signal).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import boto3
import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot
from pqbs.contracts.enums import SignalId
from pqbs.contracts.signals import SignalEvidence, SignalScore

logger = structlog.get_logger(__name__)

_SIGNAL_ID = SignalId.S3_IMPERATIVE_CONTENT

# Read from env so the model can be changed per deployment without a code change.
# Defaults to the Claude model for environments where it's accessible.
# Set BEDROCK_CLASSIFIER_MODEL_ID=meta.llama3-70b-instruct-v1:0 for ap-south-1.
_DEFAULT_BEDROCK_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"

_SYSTEM_PROMPT = (
    "You are a content classifier. Is the following text an ASSERTION "
    "(a factual claim about the world) or an INSTRUCTION/DIRECTIVE "
    "(a command or rule that someone should follow)? "
    "Reply with exactly one word: ASSERTION or INSTRUCTION."
)

# Trigger phrases for pre-filter (lowercased check)
_TRIGGER_PHRASES = [
    "should",
    "must",
    "always",
    "never",
    "do not",
    "ensure",
    "make sure",
    "ignore",
    "skip",
    "bypass",
    "override",
    "disable",
]

# Compile a single regex for efficiency
_TRIGGER_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _TRIGGER_PHRASES) + r")\b",
    re.IGNORECASE,
)


def _find_trigger_phrases(text: str) -> list[str]:
    """Return all distinct trigger phrases found in text."""
    matches = _TRIGGER_PATTERN.findall(text)
    return list({m.lower() for m in matches})


def _call_bedrock(text: str, region: str) -> str:
    """Invoke Bedrock classifier. Returns 'ASSERTION', 'INSTRUCTION', or raises.

    Supports Claude (Anthropic messages API) and Llama 3 (meta.llama3-*).
    Model is read from BEDROCK_CLASSIFIER_MODEL_ID env var at call time.
    """
    model_id = os.environ.get("BEDROCK_CLASSIFIER_MODEL_ID", _DEFAULT_BEDROCK_MODEL_ID)
    client = boto3.client("bedrock-runtime", region_name=region)

    if model_id.startswith("meta.llama3"):
        prompt = (
            f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{_SYSTEM_PROMPT}\n"
            f"<|eot_id|><|start_header_id|>user<|end_header_id|>\n"
            f"{text}\n"
            f"<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n"
        )
        body = json.dumps({"prompt": prompt, "max_gen_len": 10, "temperature": 0.0})
        response = client.invoke_model(
            modelId=model_id, body=body,
            contentType="application/json", accept="application/json",
        )
        payload = json.loads(response["body"].read())
        classification: str = payload["generation"].strip().upper()
    else:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 10,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text}],
        })
        response = client.invoke_model(
            modelId=model_id, body=body,
            contentType="application/json", accept="application/json",
        )
        payload = json.loads(response["body"].read())
        classification = payload["content"][0]["text"].strip().upper()

    return classification


def compute(snapshot: BeliefSnapshot, _conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S3: imperative content detection via pre-filter + Bedrock classification."""
    t0 = time.perf_counter()
    text = snapshot.object

    # Pre-filter: look for trigger phrases
    matched = _find_trigger_phrases(text)

    if not matched:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.0,
            evidence=SignalEvidence(
                description="No imperative trigger phrases detected",
                raw_values={"matched_triggers": ""},
            ),
            latency_ms=latency_ms,
            fired=False,
        )

    # Triggers found — classify with Bedrock
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        classification = _call_bedrock(text, region)
    except Exception as exc:
        logger.warning(
            "s3_bedrock_error",
            belief_id=str(snapshot.belief_id),
            error=str(exc),
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.5,
            evidence=SignalEvidence(
                description=(
                    f"Bedrock classification failed (fail-safe): {exc}. "
                    f"Trigger phrases found: {matched}"
                ),
                raw_values={
                    "matched_triggers": ", ".join(matched),
                    "bedrock_error": str(exc)[:200],
                },
            ),
            latency_ms=latency_ms,
            fired=False,
        )

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if "INSTRUCTION" in classification:
        score = 0.85
        fired = True
        label = "INSTRUCTION"
    elif "ASSERTION" in classification:
        score = 0.0
        fired = False
        label = "ASSERTION"
    else:
        score = 0.5
        fired = False
        label = classification

    logger.debug(
        "s3_computed",
        belief_id=str(snapshot.belief_id),
        classification=label,
        matched_triggers=matched,
        score=score,
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=score,
        evidence=SignalEvidence(
            description=(
                f"Content classified as {label} by Bedrock. "
                f"Trigger phrases: {matched}"
            ),
            raw_values={
                "classification": label,
                "matched_triggers": ", ".join(matched),
                "model_id": os.environ.get("BEDROCK_CLASSIFIER_MODEL_ID", _DEFAULT_BEDROCK_MODEL_ID),
            },
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
