"""A4 ScreeningGate — verdict composition and persistence.

Security Invariants enforced:
  1. No belief enters status='trusted' without passing TRUST_THRESHOLD check.
  7. This agent issues verdicts; it never writes beliefs independently
     (it only updates the status column of an existing pending belief).
  Fail-closed: per-signal errors assign score=0.5, fired=False.
              The belief stays pending if screen() raises.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog

from pqbs.contracts.cdc import ChangeEvent, BeliefSnapshot
from pqbs.contracts.enums import (
    BeliefStatus,
    Disposition,
    ReasonCode,
    SignalId,
    VerdictValue,
)
from pqbs.contracts.signals import SignalEvidence, SignalScore
from pqbs.contracts.verdicts import QuarantineRecord, Verdict

from pqbs.integrity.signals import (
    s1_embedding_anomaly,
    s2_source_trust_tier,
    s3_imperative_content,
    s4_author_behavior,
    s5_contradiction_burst,
    s6_corroboration_diversity,
    s7_derivation_integrity,
    s8_temporal_plausibility,
)

logger = structlog.get_logger(__name__)

SIGNAL_WEIGHTS: dict[SignalId, float] = {
    SignalId.S3_IMPERATIVE_CONTENT: 0.25,
    SignalId.S1_EMBEDDING_ANOMALY: 0.20,
    SignalId.S2_SOURCE_TRUST_TIER: 0.20,
    SignalId.S4_AUTHOR_BEHAVIOR: 0.10,
    SignalId.S5_CONTRADICTION_BURST: 0.10,
    SignalId.S6_CORROBORATION_DIVERSITY: 0.05,
    SignalId.S7_DERIVATION_INTEGRITY: 0.05,
    SignalId.S8_TEMPORAL_PLAUSIBILITY: 0.05,
}

TRUST_THRESHOLD = float(os.getenv("PQBS_TRUST_THRESHOLD", "0.4"))
QUARANTINE_THRESHOLD = float(os.getenv("PQBS_QUARANTINE_THRESHOLD", "0.7"))
SCREENER_VERSION = "1.0.0"

# Ordered list of (signal_id, module) — compute is looked up at call time so
# patches applied via unittest.mock take effect correctly.
_SIGNAL_MODULES = [
    (SignalId.S1_EMBEDDING_ANOMALY, s1_embedding_anomaly),
    (SignalId.S2_SOURCE_TRUST_TIER, s2_source_trust_tier),
    (SignalId.S3_IMPERATIVE_CONTENT, s3_imperative_content),
    (SignalId.S4_AUTHOR_BEHAVIOR, s4_author_behavior),
    (SignalId.S5_CONTRADICTION_BURST, s5_contradiction_burst),
    (SignalId.S6_CORROBORATION_DIVERSITY, s6_corroboration_diversity),
    (SignalId.S7_DERIVATION_INTEGRITY, s7_derivation_integrity),
    (SignalId.S8_TEMPORAL_PLAUSIBILITY, s8_temporal_plausibility),
]

_FALLBACK_SCORE = 0.5  # used when a signal compute function raises


def _fallback_signal(signal_id: SignalId, error: Exception, elapsed_ms: int) -> SignalScore:
    """Produce a neutral score when a signal raises unexpectedly."""
    return SignalScore(
        signal_id=signal_id,
        score=_FALLBACK_SCORE,
        evidence=SignalEvidence(
            description=f"Signal computation error (fail-safe): {error}",
            raw_values={"error": str(error)[:256]},
        ),
        latency_ms=elapsed_ms,
        fired=False,
    )


def _compose_trust_score(signal_scores: list[SignalScore]) -> float:
    """Weighted average of all signal scores. Normalised by sum of weights."""
    total_weight = sum(SIGNAL_WEIGHTS[s.signal_id] for s in signal_scores)
    if total_weight == 0.0:
        return 0.5
    weighted_sum = sum(s.score * SIGNAL_WEIGHTS[s.signal_id] for s in signal_scores)
    return weighted_sum / total_weight


def _classify(trust_score: float) -> VerdictValue:
    if trust_score <= TRUST_THRESHOLD:
        return VerdictValue.TRUSTED
    if trust_score >= QUARANTINE_THRESHOLD:
        return VerdictValue.QUARANTINED
    return VerdictValue.INCONCLUSIVE


def _dominant_reason(signal_scores: list[SignalScore], verdict: VerdictValue) -> str | None:
    """Return the signal_id of the highest-scoring fired signal, or None."""
    if verdict == VerdictValue.TRUSTED:
        return None
    fired = [s for s in signal_scores if s.fired]
    if not fired:
        return None
    dominant = max(fired, key=lambda s: s.score * SIGNAL_WEIGHTS[s.signal_id])
    return dominant.signal_id.value


def _serialize_signal_scores(signal_scores: list[SignalScore]) -> str:
    """Serialize signal scores to JSONB-compatible string."""
    out = []
    for s in signal_scores:
        out.append({
            "signal_id": s.signal_id.value,
            "score": s.score,
            "fired": s.fired,
            "latency_ms": s.latency_ms,
            "evidence_description": s.evidence.description,
            "evidence_raw_values": s.evidence.raw_values,
        })
    return json.dumps(out)


def _reconstruct_verdict_from_row(row: dict[str, Any]) -> Verdict:
    """Rebuild a Verdict from an integrity_verdict DB row (for idempotency returns)."""
    raw_signals = json.loads(row["signal_scores"]) if isinstance(row["signal_scores"], str) else row["signal_scores"]
    signal_scores = tuple(
        SignalScore(
            signal_id=SignalId(s["signal_id"]),
            score=float(s["score"]),
            evidence=SignalEvidence(
                description=s.get("evidence_description", ""),
                raw_values=s.get("evidence_raw_values", {}),
            ),
            latency_ms=int(s.get("latency_ms", 0)),
            fired=bool(s.get("fired", False)),
        )
        for s in raw_signals
    )
    screened_at = row["screened_at"]
    if isinstance(screened_at, str):
        screened_at = datetime.fromisoformat(screened_at)

    return Verdict(
        verdict_id=UUID(str(row["verdict_id"])),
        belief_id=UUID(str(row["belief_id"])),
        tenant_id=UUID(str(row["tenant_id"])),
        verdict=VerdictValue(row["verdict"]),
        trust_score=float(row["trust_score"]),
        signal_scores=signal_scores,
        triggering_rule=row.get("triggering_rule"),
        screened_at=screened_at,
        screener_version=str(row["screener_version"]),
        re_screen_reason=row.get("re_screen_reason"),
        latency_ms=int(row.get("latency_ms", 0)),
    )


class ScreeningGate:
    """
    A4 ScreeningGate: computes all 8 signals, composes a weighted trust score,
    persists verdict and quarantine records, and updates belief status.

    Security:
    - Per-signal exceptions are caught; failing signals get score=0.5 (fail-safe).
    - If screen() itself raises, the belief is NOT updated (stays pending).
    - Idempotent: same (belief_id, screener_version, tenant_id) returns stored verdict.
    """

    def screen(self, event: ChangeEvent, conn: psycopg.Connection[Any]) -> Verdict:
        """Screen a belief. Returns Verdict. Raises on unrecoverable errors."""
        t_start = time.perf_counter()
        snapshot = event.current_snapshot

        # --- Idempotency check ---
        existing = conn.execute(
            """
            SELECT verdict_id, belief_id, tenant_id, verdict, trust_score,
                   signal_scores, triggering_rule, screened_at, screener_version,
                   re_screen_reason, latency_ms
            FROM integrity_verdict
            WHERE belief_id = %s
              AND screener_version = %s
              AND tenant_id = %s
            LIMIT 1
            """,
            (str(snapshot.belief_id), SCREENER_VERSION, str(snapshot.tenant_id)),
        ).fetchone()

        if existing is not None:
            logger.info(
                "gate_idempotent_hit",
                belief_id=str(snapshot.belief_id),
                verdict_id=str(existing["verdict_id"]),
            )
            return _reconstruct_verdict_from_row(dict(existing))

        # --- Compute all 8 signals ---
        signal_scores: list[SignalScore] = []
        for signal_id, module in _SIGNAL_MODULES:
            t_sig = time.perf_counter()
            try:
                # Look up compute at call time so unittest.mock patches work.
                score = module.compute(snapshot, conn)
                signal_scores.append(score)
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - t_sig) * 1000)
                logger.warning(
                    "signal_compute_error",
                    signal_id=signal_id.value,
                    belief_id=str(snapshot.belief_id),
                    error=str(exc),
                )
                signal_scores.append(_fallback_signal(signal_id, exc, elapsed_ms))

        # --- Compose trust score ---
        trust_score = _compose_trust_score(signal_scores)
        verdict_value = _classify(trust_score)
        triggering_rule = _dominant_reason(signal_scores, verdict_value)

        screened_at = datetime.now(tz=timezone.utc)
        total_latency_ms = int((time.perf_counter() - t_start) * 1000)
        verdict_id = uuid4()

        verdict = Verdict(
            verdict_id=verdict_id,
            belief_id=snapshot.belief_id,
            tenant_id=snapshot.tenant_id,
            verdict=verdict_value,
            trust_score=trust_score,
            signal_scores=tuple(signal_scores),
            triggering_rule=triggering_rule,
            screened_at=screened_at,
            screener_version=SCREENER_VERSION,
            re_screen_reason=None,
            latency_ms=total_latency_ms,
        )

        # --- Persist integrity_verdict ---
        conn.execute(
            """
            INSERT INTO integrity_verdict
                (verdict_id, belief_id, tenant_id, verdict, trust_score,
                 signal_scores, triggering_rule, screened_at, screener_version,
                 re_screen_reason, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(verdict_id),
                str(snapshot.belief_id),
                str(snapshot.tenant_id),
                verdict_value.value,
                trust_score,
                _serialize_signal_scores(signal_scores),
                triggering_rule,
                screened_at,
                SCREENER_VERSION,
                None,
                total_latency_ms,
            ),
        )

        # --- Update belief status (only from pending) ---
        # Security Invariant 1: Never set trusted if score is wrong.
        # We assert here before writing.
        if verdict_value == VerdictValue.TRUSTED:
            assert trust_score <= TRUST_THRESHOLD, (
                f"BUG: trust_score {trust_score} > TRUST_THRESHOLD {TRUST_THRESHOLD} "
                f"but verdict is TRUSTED"
            )

        new_status: str
        if verdict_value == VerdictValue.TRUSTED:
            new_status = BeliefStatus.TRUSTED.value
        elif verdict_value == VerdictValue.QUARANTINED:
            new_status = BeliefStatus.QUARANTINED.value
        else:
            # INCONCLUSIVE: belief stays pending; log for A14
            new_status = BeliefStatus.PENDING.value
            logger.warning(
                "gate_inconclusive",
                belief_id=str(snapshot.belief_id),
                trust_score=trust_score,
                tenant_id=str(snapshot.tenant_id),
            )

        conn.execute(
            """
            UPDATE belief
            SET status = %s, trust_score = %s, screened_at = %s
            WHERE belief_id = %s
              AND tenant_id = %s
              AND status = 'pending'
            """,
            (
                new_status,
                trust_score,
                screened_at,
                str(snapshot.belief_id),
                str(snapshot.tenant_id),
            ),
        )

        # --- Write quarantine record if QUARANTINED ---
        if verdict_value == VerdictValue.QUARANTINED:
            quarantine_id = uuid4()
            # Determine dominant reason code for quarantine record
            fired_signals = [s for s in signal_scores if s.fired]
            if fired_signals:
                dominant = max(fired_signals, key=lambda s: s.score * SIGNAL_WEIGHTS[s.signal_id])
                reason_code = _signal_to_reason_code(dominant.signal_id)
            else:
                reason_code = ReasonCode.ANOMALOUS_EMBEDDING

            conn.execute(
                """
                INSERT INTO quarantine
                    (quarantine_id, belief_id, tenant_id, reason_code,
                     quarantined_at, disposition)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(quarantine_id),
                    str(snapshot.belief_id),
                    str(snapshot.tenant_id),
                    reason_code.value,
                    screened_at,
                    Disposition.HELD.value,
                ),
            )

            logger.warning(
                "belief_quarantined",
                belief_id=str(snapshot.belief_id),
                tenant_id=str(snapshot.tenant_id),
                trust_score=trust_score,
                reason_code=reason_code.value,
                triggering_rule=triggering_rule,
            )
        else:
            logger.info(
                "belief_screened",
                belief_id=str(snapshot.belief_id),
                tenant_id=str(snapshot.tenant_id),
                verdict=verdict_value.value,
                trust_score=round(trust_score, 4),
                latency_ms=total_latency_ms,
            )

        return verdict


def _signal_to_reason_code(signal_id: SignalId) -> ReasonCode:
    """Map a signal ID to the most appropriate ReasonCode for quarantine records."""
    mapping: dict[SignalId, ReasonCode] = {
        SignalId.S1_EMBEDDING_ANOMALY: ReasonCode.ANOMALOUS_EMBEDDING,
        SignalId.S2_SOURCE_TRUST_TIER: ReasonCode.UNTRUSTED_SOURCE,
        SignalId.S3_IMPERATIVE_CONTENT: ReasonCode.IMPERATIVE_CONTENT,
        SignalId.S4_AUTHOR_BEHAVIOR: ReasonCode.IDENTITY_ANOMALY,
        SignalId.S5_CONTRADICTION_BURST: ReasonCode.CONTRADICTION_BURST,
        SignalId.S6_CORROBORATION_DIVERSITY: ReasonCode.UNTRUSTED_SOURCE,
        SignalId.S7_DERIVATION_INTEGRITY: ReasonCode.DERIVED_FROM_QUARANTINED,
        SignalId.S8_TEMPORAL_PLAUSIBILITY: ReasonCode.TEMPORAL_IMPLAUSIBLE,
    }
    return mapping.get(signal_id, ReasonCode.ANOMALOUS_EMBEDDING)
