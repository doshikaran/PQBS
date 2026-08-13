"""S1 — Embedding Anomaly Signal.

Detects beliefs whose embedding vector is anomalous relative to the cluster
of existing trusted/screened beliefs for the same (tenant_id, predicate).
Weight: 0.20.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import psycopg
import structlog

from pqbs.contracts.cdc import BeliefSnapshot
from pqbs.contracts.enums import SignalId
from pqbs.contracts.signals import SignalEvidence, SignalScore

logger = structlog.get_logger(__name__)

_SIGNAL_ID = SignalId.S1_EMBEDDING_ANOMALY
_MIN_CORPUS = 5
_LOW_DISTANCE = 0.2
_HIGH_DISTANCE = 0.8


def compute(snapshot: BeliefSnapshot, conn: psycopg.Connection[Any]) -> SignalScore:
    """Compute S1: cosine distance of belief embedding from predicate cluster mean."""
    t0 = time.perf_counter()

    # Fetch the embedding for the belief being screened
    row = conn.execute(
        "SELECT embedding FROM belief WHERE belief_id = %s AND tenant_id = %s",
        (str(snapshot.belief_id), str(snapshot.tenant_id)),
    ).fetchone()

    if row is None or row["embedding"] is None:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.5,
            evidence=SignalEvidence(
                description="Embedding not found for belief",
                raw_values={"belief_id": str(snapshot.belief_id)},
            ),
            latency_ms=latency_ms,
            fired=False,
        )

    belief_vec = np.array(row["embedding"], dtype=np.float64)

    # Fetch corpus: embeddings of non-pending beliefs for same (tenant_id, predicate)
    corpus_rows = conn.execute(
        """
        SELECT embedding FROM belief
        WHERE tenant_id = %s
          AND predicate = %s
          AND status != 'pending'
          AND belief_id != %s
          AND embedding IS NOT NULL
        LIMIT 500
        """,
        (str(snapshot.tenant_id), snapshot.predicate, str(snapshot.belief_id)),
    ).fetchall()

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if len(corpus_rows) < _MIN_CORPUS:
        return SignalScore(
            signal_id=_SIGNAL_ID,
            score=0.5,
            evidence=SignalEvidence(
                description="Insufficient corpus for embedding anomaly detection",
                raw_values={
                    "corpus_size": len(corpus_rows),
                    "min_required": _MIN_CORPUS,
                    "predicate": snapshot.predicate,
                },
            ),
            latency_ms=latency_ms,
            fired=False,
        )

    corpus = np.array([r["embedding"] for r in corpus_rows], dtype=np.float64)
    cluster_mean = corpus.mean(axis=0)

    # Cosine distance = 1 - cosine_similarity
    norm_belief = np.linalg.norm(belief_vec)
    norm_mean = np.linalg.norm(cluster_mean)

    if norm_belief == 0.0 or norm_mean == 0.0:
        cosine_distance = 1.0
    else:
        cosine_similarity = float(np.dot(belief_vec, cluster_mean) / (norm_belief * norm_mean))
        # Clamp to [-1, 1] for numerical safety
        cosine_similarity = max(-1.0, min(1.0, cosine_similarity))
        cosine_distance = 1.0 - cosine_similarity

    # Map distance to [0, 1]: distance < 0.2 → 0.0, distance > 0.8 → 1.0, linear
    if cosine_distance <= _LOW_DISTANCE:
        score = 0.0
    elif cosine_distance >= _HIGH_DISTANCE:
        score = 1.0
    else:
        score = (cosine_distance - _LOW_DISTANCE) / (_HIGH_DISTANCE - _LOW_DISTANCE)

    fired = score > 0.5

    logger.debug(
        "s1_computed",
        belief_id=str(snapshot.belief_id),
        cosine_distance=round(cosine_distance, 4),
        score=round(score, 4),
        corpus_size=len(corpus_rows),
        fired=fired,
    )

    return SignalScore(
        signal_id=_SIGNAL_ID,
        score=round(score, 6),
        evidence=SignalEvidence(
            description=(
                f"Cosine distance from predicate cluster mean: {cosine_distance:.4f}"
                f" (corpus={len(corpus_rows)} beliefs)"
            ),
            raw_values={
                "cosine_distance": round(cosine_distance, 6),
                "corpus_size": len(corpus_rows),
                "predicate": snapshot.predicate,
            },
        ),
        latency_ms=latency_ms,
        fired=fired,
    )
