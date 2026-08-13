"""Unit tests for pqbs.agents.semantics.resolve (A7).

Tests cover:
- multi_valued predicate → BOTH_RETAINED, no contradiction_event
- No incumbent → BOTH_RETAINED/RECENCY (first write)
- EXPLICIT_INVALIDATION override beats all
- SOURCE_TIER: authoritative beats unverified
- RECENCY: newer valid_from wins
- CONFIDENCE: higher confidence wins
- DEFERRED when truly equal (same tier, same time, same confidence)
- contradiction_event written for all single_valued outcomes with an incumbent
- Incumbent retained: contradiction_event still written
- Deferred: contradiction_event still written

DB connection is mocked with MagicMock.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, call, patch
from uuid import uuid4, UUID

import pytest

from pqbs.contracts import (
    CandidateBelief,
    EmbeddedBelief,
    NormalizedBelief,
    ProvenanceRecord,
    Resolution,
    ResolutionBasis,
    ResolutionOutcome,
    Sensitivity,
)
from pqbs.contracts.enums import Cardinality, SourceType, TrustTier
from pqbs.contracts.provenance import ProvenanceStub
from pqbs.agents.semantics.resolve import resolve

pytestmark = pytest.mark.unit

TENANT_ID = uuid4()
EPISODE_ID = uuid4()
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(days=30)
LATER = NOW + timedelta(days=1)

FAKE_EMBEDDING: tuple[float, ...] = tuple([0.1] * 1024)


def _stub() -> ProvenanceStub:
    return ProvenanceStub(
        source_type=SourceType.SYSTEM_OF_RECORD,
        source_uri=None,
        source_digest="b" * 64,
        episode_id=EPISODE_ID,
        ingestion_agent_id="test-agent",
    )


def _provenance_record(
    trust_tier: TrustTier = TrustTier.UNVERIFIED,
) -> ProvenanceRecord:
    return ProvenanceRecord(
        provenance_id=uuid4(),
        tenant_id=TENANT_ID,
        source_type=SourceType.SYSTEM_OF_RECORD,
        source_uri=None,
        source_digest="b" * 64,
        episode_id=EPISODE_ID,
        derived_from=(),
        ingested_at=NOW,
        source_trust_tier=trust_tier,
        ingestion_agent_id="test-agent",
    )


def _candidate(
    subject: str = "Alice",
    predicate: str = "works_at",
    obj: str = "Acme Corp",
    confidence: float = 0.9,
    valid_from: datetime = NOW,
) -> CandidateBelief:
    return CandidateBelief(
        belief_id=uuid4(),
        tenant_id=TENANT_ID,
        subject=subject,
        predicate=predicate,
        object=obj,
        confidence=confidence,
        valid_from=valid_from,
        valid_to=None,
        provenance_stub=_stub(),
        author_agent_id="test-agent",
        sensitivity=Sensitivity.NORMAL,
    )


def _normalized(candidate: CandidateBelief) -> NormalizedBelief:
    return NormalizedBelief(
        candidate=candidate,
        object_normalized=candidate.object.lower(),
        sensitivity=Sensitivity.NORMAL,
    )


def _embedded(candidate: CandidateBelief) -> EmbeddedBelief:
    return EmbeddedBelief(
        normalized=_normalized(candidate),
        embedding=FAKE_EMBEDDING,
    )


# ---------------------------------------------------------------------------
# Mock connection builder
# ---------------------------------------------------------------------------

def _mock_conn_with_policy(
    cardinality: str = "single_valued",
    incumbent_row: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock conn that returns given policy and incumbent rows."""
    conn = MagicMock()

    # We'll track execute calls and return appropriate cursors
    call_results: list[MagicMock] = []

    # Call 1: policy query
    policy_cursor = MagicMock()
    if cardinality == "no_policy":
        policy_cursor.fetchone.return_value = None
    else:
        policy_cursor.fetchone.return_value = {"cardinality": cardinality}
    call_results.append(policy_cursor)

    # Call 2: provenance INSERT (no fetchone needed)
    prov_cursor = MagicMock()
    prov_cursor.fetchone.return_value = None
    call_results.append(prov_cursor)

    if cardinality == "multi_valued":
        # Call 3: belief INSERT
        belief_cursor = MagicMock()
        belief_cursor.fetchone.return_value = None
        call_results.append(belief_cursor)
    else:
        # Call 3: incumbent query
        incumbent_cursor = MagicMock()
        incumbent_cursor.fetchone.return_value = incumbent_row
        call_results.append(incumbent_cursor)

        if incumbent_row is not None:
            # Call 4: belief INSERT
            belief_cursor = MagicMock()
            belief_cursor.fetchone.return_value = None
            call_results.append(belief_cursor)

            # If challenger supersedes: call 5 is UPDATE, call 6 is contradiction INSERT
            # If incumbent retained or deferred: call 5 is contradiction INSERT
            # We handle this by making all subsequent cursors return None
            for _ in range(10):
                extra_cursor = MagicMock()
                extra_cursor.fetchone.return_value = None
                call_results.append(extra_cursor)
        else:
            # No incumbent: call 4 is belief INSERT
            belief_cursor = MagicMock()
            belief_cursor.fetchone.return_value = None
            call_results.append(belief_cursor)

    conn.execute.side_effect = iter(call_results + [MagicMock() for _ in range(20)])
    return conn


def _make_incumbent_row(
    trust_tier: str = "unverified",
    confidence: float = 0.8,
    valid_from: datetime = EARLIER,
) -> dict[str, Any]:
    return {
        "belief_id": str(uuid4()),
        "confidence": confidence,
        "valid_from": valid_from,
        "valid_to": None,
        "source_trust_tier": trust_tier,
    }


# ---------------------------------------------------------------------------
# Multi-valued predicate
# ---------------------------------------------------------------------------

class TestMultiValuedPredicate:
    def test_multi_valued_returns_both_retained(self) -> None:
        """multi_valued predicate → BOTH_RETAINED/POLICY, no contradiction_event."""
        conn = _mock_conn_with_policy(cardinality="multi_valued")
        candidate = _candidate()
        embedded = _embedded(candidate)
        prov = _provenance_record()

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.BOTH_RETAINED
        assert outcome.basis == ResolutionBasis.POLICY
        assert outcome.incumbent_id is None
        assert outcome.contradiction_event_id is None
        assert outcome.tenant_id == TENANT_ID

    def test_multi_valued_no_contradiction_event_written(self) -> None:
        """Verify no contradiction_event INSERT is called for multi_valued."""
        conn = _mock_conn_with_policy(cardinality="multi_valued")
        candidate = _candidate()
        embedded = _embedded(candidate)
        prov = _provenance_record()

        resolve(conn, embedded, prov)

        # Check that no SQL containing 'contradiction_event' was executed
        for call_args in conn.execute.call_args_list:
            sql = call_args[0][0] if call_args[0] else ""
            assert "contradiction_event" not in sql.lower(), (
                "contradiction_event should not be written for multi_valued predicate"
            )


# ---------------------------------------------------------------------------
# No incumbent (first write)
# ---------------------------------------------------------------------------

class TestNoIncumbent:
    def test_first_write_returns_both_retained_recency(self) -> None:
        """No incumbent → BOTH_RETAINED/RECENCY."""
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=None)
        candidate = _candidate()
        embedded = _embedded(candidate)
        prov = _provenance_record()

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.BOTH_RETAINED
        assert outcome.basis == ResolutionBasis.RECENCY
        assert outcome.incumbent_id is None
        assert outcome.contradiction_event_id is None

    def test_first_write_no_contradiction_event(self) -> None:
        """No contradiction_event for first write."""
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=None)
        embedded = _embedded(_candidate())
        prov = _provenance_record()
        resolve(conn, embedded, prov)

        for call_args in conn.execute.call_args_list:
            sql = call_args[0][0] if call_args[0] else ""
            assert "contradiction_event" not in sql.lower()


# ---------------------------------------------------------------------------
# Resolution precedence: EXPLICIT_INVALIDATION
# ---------------------------------------------------------------------------

class TestExplicitInvalidation:
    def test_explicit_invalidation_beats_all(self) -> None:
        """EXPLICIT_INVALIDATION override → CHALLENGER_SUPERSEDES regardless of tiers."""
        incumbent = _make_incumbent_row(
            trust_tier="authoritative",  # highest tier
            confidence=1.0,
            valid_from=LATER,  # newer than challenger
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.1, valid_from=EARLIER)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNTRUSTED)  # lowest tier

        outcome = resolve(
            conn, embedded, prov,
            resolution_basis_override=ResolutionBasis.EXPLICIT_INVALIDATION,
        )

        assert outcome.resolution == Resolution.CHALLENGER_SUPERSEDES
        assert outcome.basis == ResolutionBasis.EXPLICIT_INVALIDATION
        assert outcome.contradiction_event_id is not None


# ---------------------------------------------------------------------------
# Resolution precedence: SOURCE_TIER
# ---------------------------------------------------------------------------

class TestSourceTier:
    def test_authoritative_beats_unverified(self) -> None:
        """Challenger with AUTHORITATIVE tier beats UNVERIFIED incumbent."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.95,
            valid_from=LATER,  # incumbent is newer
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.5, valid_from=EARLIER)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.AUTHORITATIVE)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.CHALLENGER_SUPERSEDES
        assert outcome.basis == ResolutionBasis.SOURCE_TIER

    def test_unverified_loses_to_authoritative_incumbent(self) -> None:
        """UNVERIFIED challenger loses to AUTHORITATIVE incumbent."""
        incumbent = _make_incumbent_row(
            trust_tier="authoritative",
            confidence=0.5,
            valid_from=EARLIER,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.99, valid_from=LATER)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.INCUMBENT_RETAINED
        assert outcome.basis == ResolutionBasis.SOURCE_TIER
        assert outcome.contradiction_event_id is not None

    def test_contradiction_event_written_for_incumbent_retained(self) -> None:
        """contradiction_event is written even when incumbent is retained."""
        incumbent = _make_incumbent_row(trust_tier="authoritative")
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        embedded = _embedded(_candidate())
        prov = _provenance_record(trust_tier=TrustTier.UNTRUSTED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.INCUMBENT_RETAINED
        assert outcome.contradiction_event_id is not None


# ---------------------------------------------------------------------------
# Resolution precedence: RECENCY
# ---------------------------------------------------------------------------

class TestRecency:
    def test_newer_challenger_wins_on_recency(self) -> None:
        """Same tier: newer challenger valid_from → CHALLENGER_SUPERSEDES/RECENCY."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.9,
            valid_from=EARLIER,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.9, valid_from=LATER)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.CHALLENGER_SUPERSEDES
        assert outcome.basis == ResolutionBasis.RECENCY

    def test_older_challenger_loses_on_recency(self) -> None:
        """Same tier: older challenger valid_from → INCUMBENT_RETAINED/RECENCY."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.9,
            valid_from=LATER,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.9, valid_from=EARLIER)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.INCUMBENT_RETAINED
        assert outcome.basis == ResolutionBasis.RECENCY


# ---------------------------------------------------------------------------
# Resolution precedence: CONFIDENCE
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_higher_challenger_confidence_wins(self) -> None:
        """Same tier, same time: higher challenger confidence → CHALLENGER_SUPERSEDES."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.5,
            valid_from=NOW,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.9, valid_from=NOW)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.CHALLENGER_SUPERSEDES
        assert outcome.basis == ResolutionBasis.CONFIDENCE

    def test_lower_challenger_confidence_loses(self) -> None:
        """Same tier, same time: lower challenger confidence → INCUMBENT_RETAINED."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.9,
            valid_from=NOW,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.5, valid_from=NOW)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.INCUMBENT_RETAINED
        assert outcome.basis == ResolutionBasis.CONFIDENCE


# ---------------------------------------------------------------------------
# Resolution: DEFERRED
# ---------------------------------------------------------------------------

class TestDeferred:
    def test_deferred_when_truly_equal(self) -> None:
        """Same tier, same valid_from, same confidence → DEFERRED/POLICY."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.9,
            valid_from=NOW,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(confidence=0.9, valid_from=NOW)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.resolution == Resolution.DEFERRED
        assert outcome.basis == ResolutionBasis.POLICY
        assert outcome.contradiction_event_id is not None

    def test_deferred_contradiction_event_written(self) -> None:
        """DEFERRED still writes contradiction_event."""
        incumbent = _make_incumbent_row(
            trust_tier="unverified",
            confidence=0.9,
            valid_from=NOW,
        )
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        embedded = _embedded(_candidate(confidence=0.9, valid_from=NOW))
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.contradiction_event_id is not None


# ---------------------------------------------------------------------------
# ResolutionOutcome fields
# ---------------------------------------------------------------------------

class TestResolutionOutcomeFields:
    def test_outcome_tenant_id_matches(self) -> None:
        conn = _mock_conn_with_policy(cardinality="multi_valued")
        candidate = _candidate()
        embedded = _embedded(candidate)
        prov = _provenance_record()

        outcome = resolve(conn, embedded, prov)

        assert outcome.tenant_id == TENANT_ID

    def test_outcome_challenger_id_matches_belief_id(self) -> None:
        conn = _mock_conn_with_policy(cardinality="multi_valued")
        candidate = _candidate()
        embedded = _embedded(candidate)
        prov = _provenance_record()

        outcome = resolve(conn, embedded, prov)

        assert outcome.challenger_id == candidate.belief_id
        assert outcome.belief_id == candidate.belief_id

    def test_incumbent_id_set_when_incumbent_exists(self) -> None:
        incumbent = _make_incumbent_row(trust_tier="unverified", valid_from=EARLIER)
        conn = _mock_conn_with_policy(cardinality="single_valued", incumbent_row=incumbent)
        candidate = _candidate(valid_from=LATER)
        embedded = _embedded(candidate)
        prov = _provenance_record(trust_tier=TrustTier.UNVERIFIED)

        outcome = resolve(conn, embedded, prov)

        assert outcome.incumbent_id is not None
        assert isinstance(outcome.incumbent_id, UUID)

    def test_retry_count_forwarded(self) -> None:
        conn = _mock_conn_with_policy(cardinality="multi_valued")
        embedded = _embedded(_candidate())
        prov = _provenance_record()

        outcome = resolve(conn, embedded, prov, retry_count=3)

        assert outcome.retry_count == 3


# ---------------------------------------------------------------------------
# No policy row → defaults to single_valued
# ---------------------------------------------------------------------------

class TestNoPolicyDefaults:
    def test_no_policy_defaults_single_valued(self) -> None:
        """When predicate_policy has no row, cardinality defaults to single_valued."""
        conn = _mock_conn_with_policy(cardinality="no_policy", incumbent_row=None)
        embedded = _embedded(_candidate())
        prov = _provenance_record()

        outcome = resolve(conn, embedded, prov)

        # Should be single_valued first-write → BOTH_RETAINED/RECENCY
        assert outcome.resolution == Resolution.BOTH_RETAINED
        assert outcome.basis == ResolutionBasis.RECENCY
