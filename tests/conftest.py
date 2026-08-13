"""
Shared pytest fixtures for PQBS test suite.

All fixtures are session-scoped or function-scoped as appropriate.
Database fixtures are NOT here — they live in tests/integration/conftest.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import pytest

from pqbs.contracts import (
    BeliefStatus,
    BeliefSnapshot,
    CandidateBelief,
    ProvenanceStub,
    Sensitivity,
    SourceType,
)


# ---------------------------------------------------------------------------
# Tenant fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def tenant_id() -> UUID:
    """A fixed tenant UUID used throughout tests. Session-scoped for stability."""
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(scope="session")
def episode_id() -> UUID:
    """A fixed episode UUID for provenance stubs."""
    return UUID("00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def now() -> datetime:
    """A fixed UTC 'now' for deterministic tests."""
    return datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def future() -> datetime:
    """A fixed UTC datetime one year in the future."""
    return datetime(2027, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Provenance stub factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def provenance_stub(episode_id: UUID) -> ProvenanceStub:
    """A minimal, valid ProvenanceStub for use in CandidateBelief."""
    return ProvenanceStub(
        source_type=SourceType.USER_STATEMENT,
        source_uri=None,
        source_digest="a" * 64,  # 64-char hex placeholder (SHA-256 of empty string would be real)
        episode_id=episode_id,
        ingestion_agent_id="test-producer-agent-v1",
    )


# ---------------------------------------------------------------------------
# CandidateBelief factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def valid_candidate_belief(
    tenant_id: UUID,
    provenance_stub: ProvenanceStub,
    now: datetime,
    future: datetime,
) -> CandidateBelief:
    """
    A fully valid CandidateBelief for use in tests.
    Fields are set to legal boundary values so tests can derive invalid variants.
    """
    return CandidateBelief(
        belief_id=uuid.uuid4(),
        tenant_id=tenant_id,
        subject="Alice",
        predicate="works_at",
        object="Acme Corp",
        confidence=0.9,
        valid_from=now,
        valid_to=future,
        provenance_stub=provenance_stub,
        author_agent_id="test-producer-agent-v1",
        sensitivity=Sensitivity.NORMAL,
    )


# ---------------------------------------------------------------------------
# BeliefSnapshot factory (for CDC tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def valid_belief_snapshot(
    tenant_id: UUID,
    now: datetime,
    future: datetime,
) -> BeliefSnapshot:
    """A fully valid BeliefSnapshot for use in CDC tests."""
    return BeliefSnapshot(
        belief_id=uuid.uuid4(),
        tenant_id=tenant_id,
        subject="Alice",
        predicate="works_at",
        object="Acme Corp",
        object_normalized="acme corp",
        confidence=0.9,
        valid_from=now,
        valid_to=future,
        tx_from=now,
        tx_to=None,
        status=BeliefStatus.PENDING,
        supersedes=None,
        superseded_by=None,
        author_agent_id="test-producer-agent-v1",
        provenance_id=uuid.uuid4(),
        trust_score=None,
        screened_at=None,
        sensitivity=Sensitivity.NORMAL,
    )
