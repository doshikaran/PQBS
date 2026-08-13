"""Unit tests for ScreeningGate (A4) verdict composition and persistence.

All tests use mocked DB connections. No live DB required.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch
from uuid import UUID, uuid4

import pytest

from pqbs.contracts.cdc import BeliefSnapshot, ChangeEvent
from pqbs.contracts.enums import (
    BeliefStatus,
    CdcOperation,
    Sensitivity,
    SignalId,
    VerdictValue,
)
from pqbs.contracts.signals import SignalEvidence, SignalScore
from pqbs.integrity.gate import (
    QUARANTINE_THRESHOLD,
    SCREENER_VERSION,
    SIGNAL_WEIGHTS,
    TRUST_THRESHOLD,
    ScreeningGate,
)

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_TENANT = UUID("cccc0000-0000-0000-0000-000000000001")
_BELIEF = UUID("dddd0000-0000-0000-0000-000000000001")
_PROVENANCE = UUID("eeee0000-0000-0000-0000-000000000001")


def _make_snapshot(**kwargs: object) -> BeliefSnapshot:
    defaults: dict = dict(
        belief_id=_BELIEF,
        tenant_id=_TENANT,
        subject="Alice",
        predicate="works_at",
        object="Acme Corp",
        object_normalized="acme corp",
        confidence=0.9,
        valid_from=_NOW,
        valid_to=None,
        tx_from=_NOW,
        tx_to=None,
        status=BeliefStatus.PENDING,
        supersedes=None,
        superseded_by=None,
        author_agent_id="agent-test-001",
        provenance_id=_PROVENANCE,
        trust_score=None,
        screened_at=None,
        sensitivity=Sensitivity.NORMAL,
    )
    defaults.update(kwargs)
    return BeliefSnapshot(**defaults)  # type: ignore[arg-type]


def _make_event(snapshot: BeliefSnapshot) -> ChangeEvent:
    return ChangeEvent(
        belief_id=snapshot.belief_id,
        tenant_id=snapshot.tenant_id,
        operation=CdcOperation.INSERT,
        before=None,
        after=snapshot,
        commit_timestamp=snapshot.tx_from,
        screener_version=SCREENER_VERSION,
    )


def _make_signal_score(signal_id: SignalId, score: float, fired: bool = False) -> SignalScore:
    return SignalScore(
        signal_id=signal_id,
        score=score,
        evidence=SignalEvidence(description="test", raw_values={}),
        latency_ms=1,
        fired=fired,
    )


def _all_benign_scores() -> list[SignalScore]:
    """All 8 signals at near-zero score."""
    return [
        _make_signal_score(sid, 0.05, False)
        for sid in SignalId
    ]


def _mock_conn_no_existing_verdict() -> MagicMock:
    """Mock conn where idempotency check returns None (no existing verdict)."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.execute.return_value = cursor
    cursor.fetchone.return_value = None  # No existing verdict
    return conn


# ---------------------------------------------------------------------------
# Weighted composition tests
# ---------------------------------------------------------------------------

class TestWeightedComposition:
    def test_all_signals_present_in_weights(self) -> None:
        assert set(SIGNAL_WEIGHTS.keys()) == set(SignalId)

    def test_weights_sum_to_one(self) -> None:
        total = sum(SIGNAL_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-10

    def test_all_benign_signals_trusted(self) -> None:
        """All signals at 0.05 → trust_score well below TRUST_THRESHOLD → TRUSTED."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)
        conn = _mock_conn_no_existing_verdict()

        with patch("pqbs.integrity.signals.s1_embedding_anomaly.compute", return_value=_make_signal_score(SignalId.S1_EMBEDDING_ANOMALY, 0.05)), \
             patch("pqbs.integrity.signals.s2_source_trust_tier.compute", return_value=_make_signal_score(SignalId.S2_SOURCE_TRUST_TIER, 0.05)), \
             patch("pqbs.integrity.signals.s3_imperative_content.compute", return_value=_make_signal_score(SignalId.S3_IMPERATIVE_CONTENT, 0.05)), \
             patch("pqbs.integrity.signals.s4_author_behavior.compute", return_value=_make_signal_score(SignalId.S4_AUTHOR_BEHAVIOR, 0.05)), \
             patch("pqbs.integrity.signals.s5_contradiction_burst.compute", return_value=_make_signal_score(SignalId.S5_CONTRADICTION_BURST, 0.05)), \
             patch("pqbs.integrity.signals.s6_corroboration_diversity.compute", return_value=_make_signal_score(SignalId.S6_CORROBORATION_DIVERSITY, 0.05)), \
             patch("pqbs.integrity.signals.s7_derivation_integrity.compute", return_value=_make_signal_score(SignalId.S7_DERIVATION_INTEGRITY, 0.05)), \
             patch("pqbs.integrity.signals.s8_temporal_plausibility.compute", return_value=_make_signal_score(SignalId.S8_TEMPORAL_PLAUSIBILITY, 0.05)):

            verdict = gate.screen(event, conn)

        assert verdict.verdict == VerdictValue.TRUSTED
        assert verdict.trust_score <= TRUST_THRESHOLD

    def test_s3_high_score_triggers_quarantine(self) -> None:
        """All signals at 0.85 → above QUARANTINE_THRESHOLD → QUARANTINED."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)
        conn = _mock_conn_no_existing_verdict()

        with patch("pqbs.integrity.signals.s1_embedding_anomaly.compute", return_value=_make_signal_score(SignalId.S1_EMBEDDING_ANOMALY, 0.85, True)), \
             patch("pqbs.integrity.signals.s2_source_trust_tier.compute", return_value=_make_signal_score(SignalId.S2_SOURCE_TRUST_TIER, 0.85, True)), \
             patch("pqbs.integrity.signals.s3_imperative_content.compute", return_value=_make_signal_score(SignalId.S3_IMPERATIVE_CONTENT, 0.85, True)), \
             patch("pqbs.integrity.signals.s4_author_behavior.compute", return_value=_make_signal_score(SignalId.S4_AUTHOR_BEHAVIOR, 0.85, True)), \
             patch("pqbs.integrity.signals.s5_contradiction_burst.compute", return_value=_make_signal_score(SignalId.S5_CONTRADICTION_BURST, 0.85, True)), \
             patch("pqbs.integrity.signals.s6_corroboration_diversity.compute", return_value=_make_signal_score(SignalId.S6_CORROBORATION_DIVERSITY, 0.85, True)), \
             patch("pqbs.integrity.signals.s7_derivation_integrity.compute", return_value=_make_signal_score(SignalId.S7_DERIVATION_INTEGRITY, 0.85, True)), \
             patch("pqbs.integrity.signals.s8_temporal_plausibility.compute", return_value=_make_signal_score(SignalId.S8_TEMPORAL_PLAUSIBILITY, 0.85, True)):

            verdict = gate.screen(event, conn)

        assert verdict.verdict == VerdictValue.QUARANTINED
        assert verdict.trust_score >= QUARANTINE_THRESHOLD

    def test_all_8_signals_present_in_verdict(self) -> None:
        """Verdict must always contain all 8 signal scores."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)
        conn = _mock_conn_no_existing_verdict()

        with patch("pqbs.integrity.signals.s1_embedding_anomaly.compute", return_value=_make_signal_score(SignalId.S1_EMBEDDING_ANOMALY, 0.1)), \
             patch("pqbs.integrity.signals.s2_source_trust_tier.compute", return_value=_make_signal_score(SignalId.S2_SOURCE_TRUST_TIER, 0.1)), \
             patch("pqbs.integrity.signals.s3_imperative_content.compute", return_value=_make_signal_score(SignalId.S3_IMPERATIVE_CONTENT, 0.1)), \
             patch("pqbs.integrity.signals.s4_author_behavior.compute", return_value=_make_signal_score(SignalId.S4_AUTHOR_BEHAVIOR, 0.1)), \
             patch("pqbs.integrity.signals.s5_contradiction_burst.compute", return_value=_make_signal_score(SignalId.S5_CONTRADICTION_BURST, 0.1)), \
             patch("pqbs.integrity.signals.s6_corroboration_diversity.compute", return_value=_make_signal_score(SignalId.S6_CORROBORATION_DIVERSITY, 0.1)), \
             patch("pqbs.integrity.signals.s7_derivation_integrity.compute", return_value=_make_signal_score(SignalId.S7_DERIVATION_INTEGRITY, 0.1)), \
             patch("pqbs.integrity.signals.s8_temporal_plausibility.compute", return_value=_make_signal_score(SignalId.S8_TEMPORAL_PLAUSIBILITY, 0.1)):

            verdict = gate.screen(event, conn)

        assert len(verdict.signal_scores) == 8
        present_ids = {s.signal_id for s in verdict.signal_scores}
        assert present_ids == set(SignalId)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_same_belief_returns_same_verdict_id(self) -> None:
        """Calling screen() twice for the same belief returns the stored verdict_id."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)

        stored_verdict_id = str(uuid4())
        stored_row = {
            "verdict_id": stored_verdict_id,
            "belief_id": str(snapshot.belief_id),
            "tenant_id": str(snapshot.tenant_id),
            "verdict": "trusted",
            "trust_score": 0.1,
            "signal_scores": json.dumps([
                {
                    "signal_id": sid.value,
                    "score": 0.1,
                    "fired": False,
                    "latency_ms": 1,
                    "evidence_description": "test",
                    "evidence_raw_values": {},
                }
                for sid in SignalId
            ]),
            "triggering_rule": None,
            "screened_at": datetime.now(tz=timezone.utc).isoformat(),
            "screener_version": SCREENER_VERSION,
            "re_screen_reason": None,
            "latency_ms": 10,
        }

        conn = MagicMock()
        cursor = MagicMock()
        conn.execute.return_value = cursor
        # First call returns existing row
        cursor.fetchone.return_value = stored_row

        verdict = gate.screen(event, conn)
        assert str(verdict.verdict_id) == stored_verdict_id

        # conn.execute was called only once (the idempotency SELECT)
        # No INSERT should have been called
        assert conn.execute.call_count == 1

    def test_idempotency_check_uses_screener_version(self) -> None:
        """Idempotency SELECT must include screener_version."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)

        conn = _mock_conn_no_existing_verdict()

        with patch("pqbs.integrity.signals.s1_embedding_anomaly.compute", return_value=_make_signal_score(SignalId.S1_EMBEDDING_ANOMALY, 0.1)), \
             patch("pqbs.integrity.signals.s2_source_trust_tier.compute", return_value=_make_signal_score(SignalId.S2_SOURCE_TRUST_TIER, 0.1)), \
             patch("pqbs.integrity.signals.s3_imperative_content.compute", return_value=_make_signal_score(SignalId.S3_IMPERATIVE_CONTENT, 0.1)), \
             patch("pqbs.integrity.signals.s4_author_behavior.compute", return_value=_make_signal_score(SignalId.S4_AUTHOR_BEHAVIOR, 0.1)), \
             patch("pqbs.integrity.signals.s5_contradiction_burst.compute", return_value=_make_signal_score(SignalId.S5_CONTRADICTION_BURST, 0.1)), \
             patch("pqbs.integrity.signals.s6_corroboration_diversity.compute", return_value=_make_signal_score(SignalId.S6_CORROBORATION_DIVERSITY, 0.1)), \
             patch("pqbs.integrity.signals.s7_derivation_integrity.compute", return_value=_make_signal_score(SignalId.S7_DERIVATION_INTEGRITY, 0.1)), \
             patch("pqbs.integrity.signals.s8_temporal_plausibility.compute", return_value=_make_signal_score(SignalId.S8_TEMPORAL_PLAUSIBILITY, 0.1)):

            gate.screen(event, conn)

        # First execute call is the idempotency check
        first_call_sql: str = conn.execute.call_args_list[0][0][0]
        assert SCREENER_VERSION in str(conn.execute.call_args_list[0][0][1])


# ---------------------------------------------------------------------------
# Fail-safe per signal
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_signal_exception_does_not_abort_screening(self) -> None:
        """If one signal raises, the others are still computed and score=0.5 is used."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)
        conn = _mock_conn_no_existing_verdict()

        with patch("pqbs.integrity.signals.s1_embedding_anomaly.compute", side_effect=Exception("S1 DB error")), \
             patch("pqbs.integrity.signals.s2_source_trust_tier.compute", return_value=_make_signal_score(SignalId.S2_SOURCE_TRUST_TIER, 0.1)), \
             patch("pqbs.integrity.signals.s3_imperative_content.compute", return_value=_make_signal_score(SignalId.S3_IMPERATIVE_CONTENT, 0.1)), \
             patch("pqbs.integrity.signals.s4_author_behavior.compute", return_value=_make_signal_score(SignalId.S4_AUTHOR_BEHAVIOR, 0.1)), \
             patch("pqbs.integrity.signals.s5_contradiction_burst.compute", return_value=_make_signal_score(SignalId.S5_CONTRADICTION_BURST, 0.1)), \
             patch("pqbs.integrity.signals.s6_corroboration_diversity.compute", return_value=_make_signal_score(SignalId.S6_CORROBORATION_DIVERSITY, 0.1)), \
             patch("pqbs.integrity.signals.s7_derivation_integrity.compute", return_value=_make_signal_score(SignalId.S7_DERIVATION_INTEGRITY, 0.1)), \
             patch("pqbs.integrity.signals.s8_temporal_plausibility.compute", return_value=_make_signal_score(SignalId.S8_TEMPORAL_PLAUSIBILITY, 0.1)):

            verdict = gate.screen(event, conn)

        # Should still have 8 signal scores
        assert len(verdict.signal_scores) == 8
        # S1 should have score=0.5 (fallback)
        s1_score = next(s for s in verdict.signal_scores if s.signal_id == SignalId.S1_EMBEDDING_ANOMALY)
        assert s1_score.score == pytest.approx(0.5)
        assert s1_score.fired is False


# ---------------------------------------------------------------------------
# Security invariant: never set trusted when score too high
# ---------------------------------------------------------------------------

class TestSecurityInvariants:
    def test_inconclusive_belief_stays_pending(self) -> None:
        """INCONCLUSIVE verdict → belief status remains pending."""
        gate = ScreeningGate()
        snapshot = _make_snapshot()
        event = _make_event(snapshot)
        conn = _mock_conn_no_existing_verdict()

        # Score just between thresholds (> 0.4, < 0.7)
        mid_score = 0.55
        with patch("pqbs.integrity.signals.s1_embedding_anomaly.compute", return_value=_make_signal_score(SignalId.S1_EMBEDDING_ANOMALY, mid_score)), \
             patch("pqbs.integrity.signals.s2_source_trust_tier.compute", return_value=_make_signal_score(SignalId.S2_SOURCE_TRUST_TIER, mid_score)), \
             patch("pqbs.integrity.signals.s3_imperative_content.compute", return_value=_make_signal_score(SignalId.S3_IMPERATIVE_CONTENT, mid_score)), \
             patch("pqbs.integrity.signals.s4_author_behavior.compute", return_value=_make_signal_score(SignalId.S4_AUTHOR_BEHAVIOR, mid_score)), \
             patch("pqbs.integrity.signals.s5_contradiction_burst.compute", return_value=_make_signal_score(SignalId.S5_CONTRADICTION_BURST, mid_score)), \
             patch("pqbs.integrity.signals.s6_corroboration_diversity.compute", return_value=_make_signal_score(SignalId.S6_CORROBORATION_DIVERSITY, mid_score)), \
             patch("pqbs.integrity.signals.s7_derivation_integrity.compute", return_value=_make_signal_score(SignalId.S7_DERIVATION_INTEGRITY, mid_score)), \
             patch("pqbs.integrity.signals.s8_temporal_plausibility.compute", return_value=_make_signal_score(SignalId.S8_TEMPORAL_PLAUSIBILITY, mid_score)):

            verdict = gate.screen(event, conn)

        assert verdict.verdict == VerdictValue.INCONCLUSIVE

        # The UPDATE SQL should set status='pending' (no change from pending)
        update_calls = [c for c in conn.execute.call_args_list if "UPDATE belief" in str(c)]
        assert len(update_calls) == 1
        update_args = update_calls[0][0][1]
        assert "pending" in update_args
