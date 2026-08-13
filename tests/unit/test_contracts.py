"""
Unit tests for all Phase 1 contracts.

These tests MUST fail if the contract is wrong. Each test:
  - Constructs a valid model and asserts it works, OR
  - Passes invalid data and asserts ValidationError is raised.

No mock database, no external services — pure model validation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from pqbs.contracts import (
    AuditEventType,
    AuditRecord,
    BeliefSnapshot,
    BeliefStatus,
    CandidateBelief,
    CascadeRequest,
    CdcOperation,
    ChangeEvent,
    Disposition,
    EmbeddedBelief,
    NormalizedBelief,
    ProvenanceRecord,
    ProvenanceStub,
    QuarantineRecord,
    ReasonCode,
    RecalledBelief,
    RecallRequest,
    RecallResult,
    Resolution,
    ResolutionBasis,
    ResolutionOutcome,
    Sensitivity,
    SignalEvidence,
    SignalId,
    SignalScore,
    SourceType,
    TemporalContext,
    TemporalMechanism,
    TemporalQuery,
    TrustTier,
    Verdict,
    VerdictValue,
    EMBEDDING_DIM,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT = UUID("00000000-0000-0000-0000-000000000001")
_EPISODE = UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_FUTURE = datetime(2027, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_PAST = datetime(2025, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_DIGEST = "a" * 64


def _stub() -> ProvenanceStub:
    return ProvenanceStub(
        source_type=SourceType.USER_STATEMENT,
        source_digest=_DIGEST,
        episode_id=_EPISODE,
        ingestion_agent_id="agent-v1",
    )


def _candidate(**overrides) -> CandidateBelief:
    defaults = dict(
        tenant_id=_TENANT,
        subject="Alice",
        predicate="works_at",
        object="Acme Corp",
        confidence=0.9,
        valid_from=_NOW,
        valid_to=_FUTURE,
        provenance_stub=_stub(),
        author_agent_id="agent-v1",
    )
    defaults.update(overrides)
    return CandidateBelief(**defaults)


def _snapshot(**overrides) -> BeliefSnapshot:
    defaults = dict(
        belief_id=uuid.uuid4(),
        tenant_id=_TENANT,
        subject="Alice",
        predicate="works_at",
        object="Acme Corp",
        object_normalized="acme corp",
        confidence=0.9,
        valid_from=_NOW,
        valid_to=_FUTURE,
        tx_from=_NOW,
        tx_to=None,
        status=BeliefStatus.PENDING,
        supersedes=None,
        superseded_by=None,
        author_agent_id="agent-v1",
        provenance_id=uuid.uuid4(),
        trust_score=None,
        screened_at=None,
        sensitivity=Sensitivity.NORMAL,
    )
    defaults.update(overrides)
    return BeliefSnapshot(**defaults)


def _all_signal_scores() -> tuple[SignalScore, ...]:
    """Build a complete set of 8 signal scores (one per SignalId)."""
    return tuple(
        SignalScore(
            signal_id=sid,
            score=0.1,
            evidence=SignalEvidence(description=f"evidence for {sid}"),
            latency_ms=5,
            fired=False,
        )
        for sid in SignalId
    )


# ===========================================================================
# ProvenanceStub tests
# ===========================================================================


class TestProvenanceStub:
    def test_valid_stub(self) -> None:
        stub = _stub()
        assert stub.source_type == SourceType.USER_STATEMENT
        assert stub.source_digest == _DIGEST
        assert stub.ingestion_agent_id == "agent-v1"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ProvenanceStub(
                source_type=SourceType.DOCUMENT,
                source_digest=_DIGEST,
                episode_id=_EPISODE,
                ingestion_agent_id="agent-v1",
                unknown_field="bad",  # type: ignore[call-arg]
            )

    def test_immutability(self) -> None:
        stub = _stub()
        with pytest.raises(ValidationError):
            stub.source_type = SourceType.DOCUMENT  # type: ignore[misc]


# ===========================================================================
# ProvenanceRecord tests
# ===========================================================================


class TestProvenanceRecord:
    def test_valid_record(self) -> None:
        rec = ProvenanceRecord(
            tenant_id=_TENANT,
            source_type=SourceType.DOCUMENT,
            source_digest=_DIGEST,
            episode_id=_EPISODE,
            ingested_at=_NOW,
            source_trust_tier=TrustTier.CORROBORATED,
            ingestion_agent_id="agent-v1",
        )
        assert rec.source_trust_tier == TrustTier.CORROBORATED

    def test_digest_too_short(self) -> None:
        with pytest.raises(ValidationError, match="at least 64"):
            ProvenanceRecord(
                tenant_id=_TENANT,
                source_type=SourceType.DOCUMENT,
                source_digest="abc",  # too short
                episode_id=_EPISODE,
                ingested_at=_NOW,
                source_trust_tier=TrustTier.UNVERIFIED,
                ingestion_agent_id="agent-v1",
            )

    def test_digest_too_long(self) -> None:
        with pytest.raises(ValidationError, match="at most 64"):
            ProvenanceRecord(
                tenant_id=_TENANT,
                source_type=SourceType.DOCUMENT,
                source_digest="a" * 65,  # too long
                episode_id=_EPISODE,
                ingested_at=_NOW,
                source_trust_tier=TrustTier.UNVERIFIED,
                ingestion_agent_id="agent-v1",
            )

    def test_empty_agent_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least 1"):
            ProvenanceRecord(
                tenant_id=_TENANT,
                source_type=SourceType.DOCUMENT,
                source_digest=_DIGEST,
                episode_id=_EPISODE,
                ingested_at=_NOW,
                source_trust_tier=TrustTier.UNVERIFIED,
                ingestion_agent_id="",  # empty
            )


# ===========================================================================
# CandidateBelief tests
# ===========================================================================


class TestCandidateBelief:
    def test_valid_belief(self) -> None:
        b = _candidate()
        assert b.subject == "Alice"
        assert b.confidence == 0.9

    def test_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(confidence=-0.1)

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(confidence=1.1)

    def test_confidence_at_boundaries_accepted(self) -> None:
        b0 = _candidate(confidence=0.0)
        b1 = _candidate(confidence=1.0)
        assert b0.confidence == 0.0
        assert b1.confidence == 1.0

    def test_empty_subject_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(subject="")

    def test_empty_predicate_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(predicate="")

    def test_empty_object_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(object="")

    def test_empty_author_agent_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(author_agent_id="")

    def test_valid_to_before_valid_from_rejected(self) -> None:
        with pytest.raises(ValidationError, match="valid_to must be strictly after valid_from"):
            _candidate(valid_from=_FUTURE, valid_to=_NOW)

    def test_valid_to_equal_to_valid_from_rejected(self) -> None:
        with pytest.raises(ValidationError, match="valid_to must be strictly after valid_from"):
            _candidate(valid_from=_NOW, valid_to=_NOW)

    def test_valid_to_none_accepted(self) -> None:
        b = _candidate(valid_to=None)
        assert b.valid_to is None

    def test_immutable(self) -> None:
        b = _candidate()
        with pytest.raises(ValidationError):
            b.subject = "Bob"  # type: ignore[misc]

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            CandidateBelief(
                tenant_id=_TENANT,
                subject="Alice",
                predicate="works_at",
                object="Acme Corp",
                confidence=0.9,
                valid_from=_NOW,
                provenance_stub=_stub(),
                author_agent_id="agent-v1",
                nonexistent_field="oops",  # type: ignore[call-arg]
            )


# ===========================================================================
# NormalizedBelief tests
# ===========================================================================


class TestNormalizedBelief:
    def test_valid_normalized_belief(self) -> None:
        nb = NormalizedBelief(
            candidate=_candidate(),
            object_normalized="acme corp",
            sensitivity=Sensitivity.NORMAL,
        )
        assert nb.object_normalized == "acme corp"

    def test_empty_object_normalized_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedBelief(
                candidate=_candidate(),
                object_normalized="",
                sensitivity=Sensitivity.NORMAL,
            )


# ===========================================================================
# EmbeddedBelief tests
# ===========================================================================


class TestEmbeddedBelief:
    def test_valid_embedding(self) -> None:
        nb = NormalizedBelief(
            candidate=_candidate(),
            object_normalized="acme corp",
            sensitivity=Sensitivity.NORMAL,
        )
        embedding = tuple(0.0 for _ in range(EMBEDDING_DIM))
        eb = EmbeddedBelief(normalized=nb, embedding=embedding)
        assert len(eb.embedding) == EMBEDDING_DIM

    def test_wrong_dimension_rejected(self) -> None:
        nb = NormalizedBelief(
            candidate=_candidate(),
            object_normalized="acme corp",
            sensitivity=Sensitivity.NORMAL,
        )
        with pytest.raises(ValidationError, match=f"{EMBEDDING_DIM}-dimensional"):
            EmbeddedBelief(
                normalized=nb,
                embedding=tuple(0.0 for _ in range(128)),  # wrong dim
            )

    def test_zero_length_embedding_rejected(self) -> None:
        nb = NormalizedBelief(
            candidate=_candidate(),
            object_normalized="acme corp",
            sensitivity=Sensitivity.NORMAL,
        )
        with pytest.raises(ValidationError):
            EmbeddedBelief(normalized=nb, embedding=())


# ===========================================================================
# BeliefSnapshot tests
# ===========================================================================


class TestBeliefSnapshot:
    def test_valid_snapshot(self) -> None:
        snap = _snapshot()
        assert snap.status == BeliefStatus.PENDING

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BeliefSnapshot(
                belief_id=uuid.uuid4(),
                tenant_id=_TENANT,
                subject="Alice",
                predicate="works_at",
                object="Acme Corp",
                object_normalized=None,
                confidence=0.9,
                valid_from=_NOW,
                valid_to=None,
                tx_from=_NOW,
                tx_to=None,
                status=BeliefStatus.PENDING,
                supersedes=None,
                superseded_by=None,
                author_agent_id="agent-v1",
                provenance_id=uuid.uuid4(),
                trust_score=None,
                screened_at=None,
                sensitivity=Sensitivity.NORMAL,
                mystery_field="oops",  # type: ignore[call-arg]
            )


# ===========================================================================
# ChangeEvent tests
# ===========================================================================


class TestChangeEvent:
    def test_insert_event_valid(self) -> None:
        event = ChangeEvent(
            belief_id=_snapshot().belief_id,
            tenant_id=_TENANT,
            operation=CdcOperation.INSERT,
            before=None,
            after=_snapshot(),
            commit_timestamp=_NOW,
            screener_version="1.0.0",
        )
        assert event.operation == CdcOperation.INSERT
        assert event.before is None

    def test_insert_with_before_rejected(self) -> None:
        snap = _snapshot()
        with pytest.raises(ValidationError, match="INSERT events must have before=None"):
            ChangeEvent(
                belief_id=snap.belief_id,
                tenant_id=_TENANT,
                operation=CdcOperation.INSERT,
                before=snap,   # invalid for INSERT
                after=snap,
                commit_timestamp=_NOW,
                screener_version="1.0.0",
            )

    def test_delete_event_valid(self) -> None:
        snap = _snapshot()
        event = ChangeEvent(
            belief_id=snap.belief_id,
            tenant_id=_TENANT,
            operation=CdcOperation.DELETE,
            before=snap,
            after=None,
            commit_timestamp=_NOW,
            screener_version="1.0.0",
        )
        assert event.operation == CdcOperation.DELETE
        assert event.after is None

    def test_delete_with_after_rejected(self) -> None:
        snap = _snapshot()
        with pytest.raises(ValidationError, match="DELETE events must have after=None"):
            ChangeEvent(
                belief_id=snap.belief_id,
                tenant_id=_TENANT,
                operation=CdcOperation.DELETE,
                before=snap,
                after=snap,    # invalid for DELETE
                commit_timestamp=_NOW,
                screener_version="1.0.0",
            )

    def test_update_event_valid(self) -> None:
        snap = _snapshot()
        event = ChangeEvent(
            belief_id=snap.belief_id,
            tenant_id=_TENANT,
            operation=CdcOperation.UPDATE,
            before=snap,
            after=snap,
            commit_timestamp=_NOW,
            screener_version="1.0.0",
        )
        assert event.before is not None
        assert event.after is not None

    def test_update_missing_before_rejected(self) -> None:
        snap = _snapshot()
        with pytest.raises(ValidationError, match="UPDATE events must have both before and after"):
            ChangeEvent(
                belief_id=snap.belief_id,
                tenant_id=_TENANT,
                operation=CdcOperation.UPDATE,
                before=None,   # invalid for UPDATE
                after=snap,
                commit_timestamp=_NOW,
                screener_version="1.0.0",
            )

    def test_update_missing_after_rejected(self) -> None:
        snap = _snapshot()
        with pytest.raises(ValidationError, match="UPDATE events must have both before and after"):
            ChangeEvent(
                belief_id=snap.belief_id,
                tenant_id=_TENANT,
                operation=CdcOperation.UPDATE,
                before=snap,
                after=None,    # invalid for UPDATE
                commit_timestamp=_NOW,
                screener_version="1.0.0",
            )

    def test_current_snapshot_insert(self) -> None:
        snap = _snapshot()
        event = ChangeEvent(
            belief_id=snap.belief_id,
            tenant_id=_TENANT,
            operation=CdcOperation.INSERT,
            before=None,
            after=snap,
            commit_timestamp=_NOW,
            screener_version="1.0.0",
        )
        assert event.current_snapshot == snap

    def test_current_snapshot_delete(self) -> None:
        snap = _snapshot()
        event = ChangeEvent(
            belief_id=snap.belief_id,
            tenant_id=_TENANT,
            operation=CdcOperation.DELETE,
            before=snap,
            after=None,
            commit_timestamp=_NOW,
            screener_version="1.0.0",
        )
        assert event.current_snapshot == snap


# ===========================================================================
# SignalScore tests
# ===========================================================================


class TestSignalScore:
    def test_valid_signal_score(self) -> None:
        ss = SignalScore(
            signal_id=SignalId.S1_EMBEDDING_ANOMALY,
            score=0.75,
            evidence=SignalEvidence(description="test evidence"),
            latency_ms=10,
            fired=True,
        )
        assert ss.signal_id == SignalId.S1_EMBEDDING_ANOMALY
        assert ss.fired is True

    def test_score_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalScore(
                signal_id=SignalId.S1_EMBEDDING_ANOMALY,
                score=-0.01,
                evidence=SignalEvidence(description="test"),
                latency_ms=0,
                fired=False,
            )

    def test_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalScore(
                signal_id=SignalId.S1_EMBEDDING_ANOMALY,
                score=1.01,
                evidence=SignalEvidence(description="test"),
                latency_ms=0,
                fired=False,
            )

    def test_negative_latency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalScore(
                signal_id=SignalId.S2_SOURCE_TRUST_TIER,
                score=0.5,
                evidence=SignalEvidence(description="test"),
                latency_ms=-1,
                fired=False,
            )

    def test_all_signal_ids_valid(self) -> None:
        """Ensure we can construct a SignalScore for every SignalId."""
        for sid in SignalId:
            ss = SignalScore(
                signal_id=sid,
                score=0.0,
                evidence=SignalEvidence(description=f"evidence {sid}"),
                latency_ms=0,
                fired=False,
            )
            assert ss.signal_id == sid


# ===========================================================================
# Verdict tests
# ===========================================================================


class TestVerdict:
    def test_valid_verdict_with_all_signals(self) -> None:
        v = Verdict(
            belief_id=uuid.uuid4(),
            tenant_id=_TENANT,
            verdict=VerdictValue.TRUSTED,
            trust_score=0.95,
            signal_scores=_all_signal_scores(),
            screened_at=_NOW,
            screener_version="1.0.0",
            latency_ms=42,
        )
        assert v.verdict == VerdictValue.TRUSTED
        assert len(v.signal_scores) == len(SignalId)

    def test_missing_signal_rejected(self) -> None:
        # Only 7 signals — missing one
        incomplete = _all_signal_scores()[:-1]
        with pytest.raises(ValidationError, match="signal scores"):
            Verdict(
                belief_id=uuid.uuid4(),
                tenant_id=_TENANT,
                verdict=VerdictValue.TRUSTED,
                trust_score=0.9,
                signal_scores=incomplete,
                screened_at=_NOW,
                screener_version="1.0.0",
                latency_ms=10,
            )

    def test_wrong_count_rejected(self) -> None:
        # 0 signals — clearly wrong
        with pytest.raises(ValidationError, match="signal scores"):
            Verdict(
                belief_id=uuid.uuid4(),
                tenant_id=_TENANT,
                verdict=VerdictValue.QUARANTINED,
                trust_score=0.1,
                signal_scores=(),
                screened_at=_NOW,
                screener_version="1.0.0",
                latency_ms=5,
            )

    def test_duplicate_signal_id_rejected(self) -> None:
        # 8 scores but all for S1 — missing S2–S8
        duped = tuple(
            SignalScore(
                signal_id=SignalId.S1_EMBEDDING_ANOMALY,
                score=0.5,
                evidence=SignalEvidence(description="dup"),
                latency_ms=1,
                fired=False,
            )
            for _ in range(8)
        )
        with pytest.raises(ValidationError, match="Missing signal scores"):
            Verdict(
                belief_id=uuid.uuid4(),
                tenant_id=_TENANT,
                verdict=VerdictValue.QUARANTINED,
                trust_score=0.2,
                signal_scores=duped,
                screened_at=_NOW,
                screener_version="1.0.0",
                latency_ms=5,
            )

    def test_trust_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Verdict(
                belief_id=uuid.uuid4(),
                tenant_id=_TENANT,
                verdict=VerdictValue.TRUSTED,
                trust_score=1.5,  # out of range
                signal_scores=_all_signal_scores(),
                screened_at=_NOW,
                screener_version="1.0.0",
                latency_ms=10,
            )

    def test_negative_latency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Verdict(
                belief_id=uuid.uuid4(),
                tenant_id=_TENANT,
                verdict=VerdictValue.TRUSTED,
                trust_score=0.9,
                signal_scores=_all_signal_scores(),
                screened_at=_NOW,
                screener_version="1.0.0",
                latency_ms=-1,  # invalid
            )


# ===========================================================================
# QuarantineRecord tests
# ===========================================================================


class TestQuarantineRecord:
    def test_valid_quarantine_record(self) -> None:
        qr = QuarantineRecord(
            belief_id=uuid.uuid4(),
            tenant_id=_TENANT,
            reason_code=ReasonCode.ANOMALOUS_EMBEDDING,
            quarantined_at=_NOW,
            triggering_verdict_id=uuid.uuid4(),
        )
        assert qr.disposition == Disposition.HELD

    def test_disposition_can_be_set(self) -> None:
        qr = QuarantineRecord(
            belief_id=uuid.uuid4(),
            tenant_id=_TENANT,
            reason_code=ReasonCode.UNTRUSTED_SOURCE,
            quarantined_at=_NOW,
            triggering_verdict_id=uuid.uuid4(),
            disposition=Disposition.RELEASED,
        )
        assert qr.disposition == Disposition.RELEASED


# ===========================================================================
# CascadeRequest tests
# ===========================================================================


class TestCascadeRequest:
    def test_valid_cascade_request(self) -> None:
        cr = CascadeRequest(
            root_quarantine_id=uuid.uuid4(),
            belief_ids=(uuid.uuid4(),),
            reason="parent belief quarantined",
            depth=0,
            max_depth=20,
            initiated_at=_NOW,
            tenant_id=_TENANT,
        )
        assert cr.depth == 0

    def test_empty_belief_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CascadeRequest(
                root_quarantine_id=uuid.uuid4(),
                belief_ids=(),  # must have at least 1
                reason="parent belief quarantined",
                depth=0,
                initiated_at=_NOW,
                tenant_id=_TENANT,
            )

    def test_depth_exceeds_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exceeds max_depth"):
            CascadeRequest(
                root_quarantine_id=uuid.uuid4(),
                belief_ids=(uuid.uuid4(),),
                reason="cascade overflow",
                depth=21,      # exceeds max_depth=20
                max_depth=20,
                initiated_at=_NOW,
                tenant_id=_TENANT,
            )

    def test_depth_at_max_accepted(self) -> None:
        cr = CascadeRequest(
            root_quarantine_id=uuid.uuid4(),
            belief_ids=(uuid.uuid4(),),
            reason="at limit",
            depth=20,
            max_depth=20,
            initiated_at=_NOW,
            tenant_id=_TENANT,
        )
        assert cr.depth == 20

    def test_negative_depth_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CascadeRequest(
                root_quarantine_id=uuid.uuid4(),
                belief_ids=(uuid.uuid4(),),
                reason="bad depth",
                depth=-1,
                initiated_at=_NOW,
                tenant_id=_TENANT,
            )

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CascadeRequest(
                root_quarantine_id=uuid.uuid4(),
                belief_ids=(uuid.uuid4(),),
                reason="",  # empty
                depth=0,
                initiated_at=_NOW,
                tenant_id=_TENANT,
            )


# ===========================================================================
# RecallRequest tests
# ===========================================================================


class TestRecallRequest:
    def test_valid_recall_request(self) -> None:
        rr = RecallRequest(
            query="What does Alice do?",
            tenant_id=_TENANT,
        )
        assert rr.limit == 10
        assert rr.min_trust_score == 0.0

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecallRequest(query="", tenant_id=_TENANT)

    def test_limit_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecallRequest(query="test", tenant_id=_TENANT, limit=0)

    def test_limit_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecallRequest(query="test", tenant_id=_TENANT, limit=101)

    def test_limit_at_max_accepted(self) -> None:
        rr = RecallRequest(query="test", tenant_id=_TENANT, limit=100)
        assert rr.limit == 100

    def test_min_trust_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecallRequest(query="test", tenant_id=_TENANT, min_trust_score=1.5)

    def test_temporal_context_defaults_to_none(self) -> None:
        rr = RecallRequest(query="test", tenant_id=_TENANT)
        assert rr.temporal_context.valid_at is None
        assert rr.temporal_context.known_at is None


# ===========================================================================
# RecallResult tests
# ===========================================================================


class TestRecallResult:
    def _recalled_belief(self) -> RecalledBelief:
        return RecalledBelief(
            belief_id=uuid.uuid4(),
            tenant_id=_TENANT,
            subject="Alice",
            predicate="works_at",
            object="Acme Corp",
            object_normalized="acme corp",
            confidence=0.9,
            trust_score=0.85,
            valid_from=_NOW,
            valid_to=None,
            tx_from=_NOW,
            author_agent_id="agent-v1",
            provenance_id=uuid.uuid4(),
        )

    def test_empty_result_valid(self) -> None:
        rr = RecallResult(
            request_id=uuid.uuid4(),
            beliefs=(),
            provenance_ids=(),
            trust_scores=(),
            retrieved_at=_NOW,
            query_latency_ms=12,
        )
        assert len(rr.beliefs) == 0

    def test_mismatched_provenance_ids_rejected(self) -> None:
        b = self._recalled_belief()
        with pytest.raises(ValidationError, match="provenance_ids length must match"):
            RecallResult(
                request_id=uuid.uuid4(),
                beliefs=(b,),
                provenance_ids=(),          # length mismatch
                trust_scores=(0.85,),
                retrieved_at=_NOW,
                query_latency_ms=10,
            )

    def test_mismatched_trust_scores_rejected(self) -> None:
        b = self._recalled_belief()
        with pytest.raises(ValidationError, match="trust_scores length must match"):
            RecallResult(
                request_id=uuid.uuid4(),
                beliefs=(b,),
                provenance_ids=(uuid.uuid4(),),
                trust_scores=(),            # length mismatch
                retrieved_at=_NOW,
                query_latency_ms=10,
            )

    def test_consistent_parallel_arrays_accepted(self) -> None:
        b = self._recalled_belief()
        prov_id = uuid.uuid4()
        rr = RecallResult(
            request_id=uuid.uuid4(),
            beliefs=(b,),
            provenance_ids=(prov_id,),
            trust_scores=(0.85,),
            retrieved_at=_NOW,
            query_latency_ms=7,
        )
        assert len(rr.beliefs) == 1
        assert len(rr.provenance_ids) == 1
        assert len(rr.trust_scores) == 1

    def test_negative_latency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecallResult(
                request_id=uuid.uuid4(),
                retrieved_at=_NOW,
                query_latency_ms=-1,
            )


# ===========================================================================
# ResolutionOutcome tests
# ===========================================================================


class TestResolutionOutcome:
    def test_valid_resolution_outcome(self) -> None:
        bid = uuid.uuid4()
        ro = ResolutionOutcome(
            belief_id=bid,
            tenant_id=_TENANT,
            resolution=Resolution.CHALLENGER_SUPERSEDES,
            basis=ResolutionBasis.RECENCY,
            incumbent_id=uuid.uuid4(),
            challenger_id=bid,
            retry_count=0,
            detected_at=_NOW,
        )
        assert ro.resolution == Resolution.CHALLENGER_SUPERSEDES

    def test_negative_retry_count_rejected(self) -> None:
        bid = uuid.uuid4()
        with pytest.raises(ValidationError):
            ResolutionOutcome(
                belief_id=bid,
                tenant_id=_TENANT,
                resolution=Resolution.INCUMBENT_RETAINED,
                basis=ResolutionBasis.CONFIDENCE,
                incumbent_id=uuid.uuid4(),
                challenger_id=bid,
                retry_count=-1,
                detected_at=_NOW,
            )


# ===========================================================================
# TemporalQuery tests
# ===========================================================================


class TestTemporalQuery:
    def test_valid_temporal_query(self) -> None:
        tq = TemporalQuery(
            tenant_id=_TENANT,
            as_of=_PAST,
            mechanism=TemporalMechanism.BITEMPORAL,
            requesting_agent_id="audit-agent-v1",
        )
        assert tq.mechanism == TemporalMechanism.BITEMPORAL

    def test_empty_agent_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TemporalQuery(
                tenant_id=_TENANT,
                as_of=_PAST,
                mechanism=TemporalMechanism.MVCC,
                requesting_agent_id="",  # empty
            )


# ===========================================================================
# AuditRecord tests
# ===========================================================================


class TestAuditRecord:
    def test_valid_audit_record(self) -> None:
        ar = AuditRecord(
            event_type=AuditEventType.BELIEF_CREATED,
            agent_id="producer-agent-v1",
            tenant_id=_TENANT,
            belief_id=uuid.uuid4(),
            timestamp=_NOW,
            reason="Belief ingested from user statement",
        )
        assert ar.event_type == AuditEventType.BELIEF_CREATED
        assert ar.before is None
        assert ar.after is None

    def test_empty_agent_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditRecord(
                event_type=AuditEventType.BELIEF_QUARANTINED,
                agent_id="",   # empty
                tenant_id=_TENANT,
                timestamp=_NOW,
                reason="Quarantined by screener",
            )

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditRecord(
                event_type=AuditEventType.BELIEF_RELEASED,
                agent_id="screener-v1",
                tenant_id=_TENANT,
                timestamp=_NOW,
                reason="",     # empty
            )

    def test_reason_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditRecord(
                event_type=AuditEventType.BELIEF_CREATED,
                agent_id="producer-v1",
                tenant_id=_TENANT,
                timestamp=_NOW,
                reason="x" * 2049,    # exceeds max_length=2048
            )

    def test_reason_at_max_length_accepted(self) -> None:
        ar = AuditRecord(
            event_type=AuditEventType.BELIEF_CREATED,
            agent_id="producer-v1",
            tenant_id=_TENANT,
            timestamp=_NOW,
            reason="x" * 2048,
        )
        assert len(ar.reason) == 2048

    def test_before_after_with_typed_values(self) -> None:
        ar = AuditRecord(
            event_type=AuditEventType.BELIEF_SUPERSEDED,
            agent_id="resolver-v1",
            tenant_id=_TENANT,
            belief_id=uuid.uuid4(),
            timestamp=_NOW,
            before={"status": "pending", "confidence": 0.8},
            after={"status": "superseded", "confidence": 0.8},
            reason="Challenger superseded incumbent",
        )
        assert ar.before is not None
        assert ar.after is not None
        assert ar.before["status"] == "pending"


# ===========================================================================
# Enum serialization tests
# ===========================================================================


class TestEnumSerialization:
    """Verify all enums serialize to their string values (not enum names)."""

    def test_belief_status_serializes_to_string(self) -> None:
        b = _candidate()
        # sensitivity is an enum — confirm it serializes as string
        data = b.model_dump()
        assert data["sensitivity"] == "normal"

    def test_change_event_operation_serializes_to_string(self) -> None:
        snap = _snapshot()
        event = ChangeEvent(
            belief_id=snap.belief_id,
            tenant_id=_TENANT,
            operation=CdcOperation.INSERT,
            before=None,
            after=snap,
            commit_timestamp=_NOW,
            screener_version="1.0.0",
        )
        data = event.model_dump()
        assert data["operation"] == "insert"

    def test_signal_id_values_are_s_codes(self) -> None:
        assert SignalId.S1_EMBEDDING_ANOMALY.value == "S1"
        assert SignalId.S8_TEMPORAL_PLAUSIBILITY.value == "S8"


# ===========================================================================
# Import smoke test
# ===========================================================================


class TestContractImports:
    """Verify all contracts are importable from the top-level package."""

    def test_all_contracts_importable(self) -> None:
        from pqbs.contracts import (
            AuditRecord,
            BeliefSnapshot,
            CandidateBelief,
            CascadeRequest,
            ChangeEvent,
            EmbeddedBelief,
            NormalizedBelief,
            ProvenanceRecord,
            ProvenanceStub,
            QuarantineRecord,
            RecallRequest,
            RecallResult,
            RecalledBelief,
            ResolutionOutcome,
            SignalEvidence,
            SignalScore,
            TemporalContext,
            TemporalQuery,
            Verdict,
        )
        # All names resolve without ImportError — the assertion is the import itself
        assert CandidateBelief is not None
        assert ChangeEvent is not None
        assert Verdict is not None
        assert AuditRecord is not None

    def test_exceptions_importable(self) -> None:
        from pqbs.contracts import (
            AuditSinkError,
            BeliefValidationError,
            ContractionError,
            EmbeddingError,
            InsufficientTrustError,
            PQBSError,
            QuarantineError,
            RetryExhaustedError,
            ScreeningError,
            TenantIsolationError,
        )
        assert issubclass(RetryExhaustedError, PQBSError)
        assert issubclass(TenantIsolationError, PQBSError)
