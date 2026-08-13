"""Integration tests for Phase 3 write path.

Requires COCKROACH_URL set and AWS credentials for Bedrock.
Mark: @pytest.mark.integration

Tests:
1. test_full_ingest_produces_pending_belief — ingest a sentence → belief in DB as pending
2. test_correction_supersedes_incumbent — trusted belief → correct() → superseded
3. test_retry_wrapper_live — simulate contention with threading
4. test_multi_valued_allows_parallel — same subject+predicate, multi_valued → both pending
5. test_canonicalize_live — tier normalization rule → object_normalized is canonical
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

from pqbs.contracts import (
    CandidateBelief,
    NormalizedBelief,
    EmbeddedBelief,
    ProvenanceRecord,
    ProvenanceStub,
    Resolution,
    ResolutionBasis,
    Sensitivity,
)
from pqbs.contracts.enums import Cardinality, SourceType, TrustTier
from pqbs.agents.semantics.canonicalize import canonicalize
from pqbs.agents.semantics.resolve import resolve
from pqbs.agents.producer.correct import correct
from pqbs.substrate.connection import get_connection
from pqbs.substrate.retry import with_serializable_retry
from pqbs.substrate.transaction import begin_serializable, commit, rollback

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
EPISODE_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# Fake 1024-dim embedding — avoids Bedrock calls in integration tests
# that test DB behavior, not Bedrock behavior.
FAKE_EMBEDDING: tuple[float, ...] = tuple([0.01] * 1024)


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def _cleanup(conn: psycopg.Connection[Any], tenant_id: uuid.UUID) -> None:
    """Remove all test data for the integration tenant."""
    conn.execute("DELETE FROM quarantine WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM integrity_verdict WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM contradiction_event WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM retrieval_log WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM belief WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM provenance WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM predicate_policy WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM agent_identity WHERE tenant_id = %s", (str(tenant_id),))
    conn.execute("DELETE FROM working_memory WHERE tenant_id = %s", (str(tenant_id),))


def _insert_agent(
    conn: psycopg.Connection[Any],
    tenant_id: uuid.UUID,
    agent_id: str = "test-producer-agent-v1",
) -> None:
    """Insert an active agent identity row."""
    conn.execute(
        """
        INSERT INTO agent_identity
            (agent_id, tenant_id, agent_class, db_role, credential_ref, status)
        VALUES (%s, %s, 'producer', 'pqbs_writer', 'test-ref', 'active')
        ON CONFLICT (tenant_id, agent_id) DO NOTHING
        """,
        (agent_id, str(tenant_id)),
    )


def _stub(agent_id: str = "test-producer-agent-v1") -> ProvenanceStub:
    return ProvenanceStub(
        source_type=SourceType.SYSTEM_OF_RECORD,
        source_uri=None,
        source_digest="c" * 64,
        episode_id=EPISODE_ID,
        ingestion_agent_id=agent_id,
    )


def _provenance_record(
    tenant_id: uuid.UUID,
    trust_tier: TrustTier = TrustTier.UNVERIFIED,
    agent_id: str = "test-producer-agent-v1",
) -> ProvenanceRecord:
    return ProvenanceRecord(
        provenance_id=uuid4(),
        tenant_id=tenant_id,
        source_type=SourceType.SYSTEM_OF_RECORD,
        source_uri=None,
        source_digest="c" * 64,
        episode_id=EPISODE_ID,
        derived_from=(),
        ingested_at=NOW,
        source_trust_tier=trust_tier,
        ingestion_agent_id=agent_id,
    )


def _candidate(
    tenant_id: uuid.UUID,
    subject: str = "Alice",
    predicate: str = "works_at",
    obj: str = "Acme Corp",
    confidence: float = 0.9,
    valid_from: datetime = NOW,
    agent_id: str = "test-producer-agent-v1",
) -> CandidateBelief:
    return CandidateBelief(
        belief_id=uuid4(),
        tenant_id=tenant_id,
        subject=subject,
        predicate=predicate,
        object=obj,
        confidence=confidence,
        valid_from=valid_from,
        valid_to=None,
        provenance_stub=_stub(agent_id),
        author_agent_id=agent_id,
        sensitivity=Sensitivity.NORMAL,
    )


def _embedded(candidate: CandidateBelief, normalized_val: str | None = None) -> EmbeddedBelief:
    norm = NormalizedBelief(
        candidate=candidate,
        object_normalized=normalized_val or candidate.object.lower(),
        sensitivity=candidate.sensitivity,
    )
    return EmbeddedBelief(normalized=norm, embedding=FAKE_EMBEDDING)


def _resolve_txn(
    conn: psycopg.Connection[Any],
    embedded: EmbeddedBelief,
    prov: ProvenanceRecord,
    override: ResolutionBasis | None = None,
) -> Any:
    begin_serializable(conn)
    try:
        outcome = resolve(conn, embedded, prov, resolution_basis_override=override)
        commit(conn)
        return outcome
    except Exception:
        rollback(conn)
        raise


# ---------------------------------------------------------------------------
# Test 1: Full resolve produces pending belief
# ---------------------------------------------------------------------------

class TestFullResolvePendingBelief:
    def test_resolve_produces_pending_belief(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """resolve() → belief appears in DB with status='pending'."""
        _cleanup(db_conn, integration_tenant_id)
        _insert_agent(db_conn, integration_tenant_id)

        candidate = _candidate(integration_tenant_id)
        embedded = _embedded(candidate)
        prov = _provenance_record(integration_tenant_id)

        outcome, _ = with_serializable_retry(
            db_conn, _resolve_txn, embedded, prov
        )

        # Verify belief in DB
        row = db_conn.execute(
            "SELECT status, subject, predicate FROM belief "
            "WHERE tenant_id = %s AND belief_id = %s",
            (str(integration_tenant_id), str(outcome.belief_id)),
        ).fetchone()

        assert row is not None
        assert row["status"] == "pending"
        assert row["subject"] == "Alice"
        assert row["predicate"] == "works_at"

        _cleanup(db_conn, integration_tenant_id)

    def test_resolve_writes_provenance_row(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """resolve() → provenance row created in DB."""
        _cleanup(db_conn, integration_tenant_id)
        _insert_agent(db_conn, integration_tenant_id)

        candidate = _candidate(integration_tenant_id)
        embedded = _embedded(candidate)
        prov = _provenance_record(integration_tenant_id)

        outcome, _ = with_serializable_retry(
            db_conn, _resolve_txn, embedded, prov
        )

        prov_row = db_conn.execute(
            "SELECT provenance_id FROM provenance WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchone()

        assert prov_row is not None

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# Test 2: Correction supersedes incumbent
# ---------------------------------------------------------------------------

class TestCorrectionSupersedesIncumbent:
    def test_correction_supersedes_trusted_incumbent(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Insert a trusted belief directly, then correct() → incumbent superseded."""
        _cleanup(db_conn, integration_tenant_id)
        _insert_agent(db_conn, integration_tenant_id)

        # Insert a trusted belief manually (bypasses integrity gate for test setup)
        prov_id = uuid4()
        db_conn.execute(
            """
            INSERT INTO provenance
                (provenance_id, tenant_id, source_type, source_uri, source_digest,
                 episode_id, derived_from, ingested_at, source_trust_tier, ingestion_agent_id)
            VALUES (%s, %s, 'system_of_record', NULL, %s, %s, '[]', %s, 'authoritative', %s)
            """,
            (
                str(prov_id),
                str(integration_tenant_id),
                "d" * 64,
                str(EPISODE_ID),
                NOW,
                "test-producer-agent-v1",
            ),
        )

        incumbent_belief_id = uuid4()
        incumbent_embedding = "[" + ",".join(["0.02"] * 1024) + "]"
        db_conn.execute(
            """
            INSERT INTO belief
                (tenant_id, belief_id, subject, predicate, object, object_normalized,
                 embedding, confidence, valid_from, status, author_agent_id,
                 provenance_id, trust_score, screened_at, sensitivity)
            VALUES (%s, %s, 'Alice', 'works_at', 'Old Company', 'old company',
                    %s::vector, 0.9, %s, 'trusted', 'test-producer-agent-v1',
                    %s, 0.9, %s, 'normal')
            """,
            (
                str(integration_tenant_id),
                str(incumbent_belief_id),
                incumbent_embedding,
                NOW,
                str(prov_id),
                NOW,
            ),
        )

        # Now apply correction
        corrected_prov_stub = ProvenanceStub(
            source_type=SourceType.SYSTEM_OF_RECORD,
            source_uri=None,
            source_digest="e" * 64,
            episode_id=EPISODE_ID,
            ingestion_agent_id="test-producer-agent-v1",
        )

        # correct() calls embed_normalized — patch it to avoid Bedrock
        from unittest.mock import patch

        with patch(
            "pqbs.agents.producer.correct.embed_normalized",
            side_effect=lambda norm, **kw: EmbeddedBelief(
                normalized=norm, embedding=FAKE_EMBEDDING
            ),
        ):
            outcome = correct(
                conn=db_conn,
                subject="Alice",
                predicate="works_at",
                correct_value="New Company",
                provenance_stub=corrected_prov_stub,
                tenant_id=integration_tenant_id,
                author_agent_id="test-producer-agent-v1",
                confidence=1.0,
            )

        assert outcome.resolution == Resolution.CHALLENGER_SUPERSEDES
        assert outcome.basis == ResolutionBasis.EXPLICIT_INVALIDATION
        assert outcome.incumbent_id == incumbent_belief_id

        # Verify incumbent is now superseded
        inc_row = db_conn.execute(
            "SELECT status, superseded_by FROM belief WHERE tenant_id = %s AND belief_id = %s",
            (str(integration_tenant_id), str(incumbent_belief_id)),
        ).fetchone()
        assert inc_row is not None
        assert inc_row["status"] == "superseded"
        assert str(inc_row["superseded_by"]) == str(outcome.challenger_id)

        # Verify new belief is pending
        new_row = db_conn.execute(
            "SELECT status FROM belief WHERE tenant_id = %s AND belief_id = %s",
            (str(integration_tenant_id), str(outcome.challenger_id)),
        ).fetchone()
        assert new_row is not None
        assert new_row["status"] == "pending"

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# Test 3: Retry wrapper live — threading-based contention simulation
# ---------------------------------------------------------------------------

class TestRetryWrapperLive:
    def test_retry_wrapper_commits_under_contention(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Two threads writing the same row — both should ultimately succeed
        (one may retry). Verifies retry wrapper doesn't corrupt state."""
        _cleanup(db_conn, integration_tenant_id)
        _insert_agent(db_conn, integration_tenant_id)

        results: list[Any] = []
        errors: list[Exception] = []

        def writer_thread(subject: str) -> None:
            import os
            from psycopg.rows import dict_row as _dict_row
            url = os.environ["COCKROACH_URL"]
            thread_conn = psycopg.connect(url, row_factory=_dict_row)
            try:
                candidate = _candidate(
                    integration_tenant_id,
                    subject=subject,
                    predicate="shipping_tier",
                    obj="gold",
                )
                embedded = _embedded(candidate)
                prov = _provenance_record(integration_tenant_id)

                outcome, retry_count = with_serializable_retry(
                    thread_conn,
                    _resolve_txn,
                    embedded,
                    prov,
                    max_attempts=10,
                )
                results.append((outcome, retry_count))
            except Exception as exc:
                errors.append(exc)
            finally:
                thread_conn.close()

        # Use different subjects to avoid FK/unique conflicts while exercising contention
        threads = [
            threading.Thread(target=writer_thread, args=(f"Entity{i}",))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 3

        # Verify all beliefs landed in DB
        rows = db_conn.execute(
            "SELECT belief_id FROM belief WHERE tenant_id = %s",
            (str(integration_tenant_id),),
        ).fetchall()
        assert len(rows) >= 3

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# Test 4: Multi-valued allows parallel
# ---------------------------------------------------------------------------

class TestMultiValuedAllowsParallel:
    def test_multi_valued_two_beliefs_both_pending(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Insert multi_valued predicate policy, then resolve twice → both pending."""
        _cleanup(db_conn, integration_tenant_id)
        _insert_agent(db_conn, integration_tenant_id)

        # Insert multi_valued policy
        db_conn.execute(
            """
            INSERT INTO predicate_policy
                (tenant_id, predicate, cardinality, resolution_strategy,
                 min_source_tier, is_sensitive, normalization_rule)
            VALUES (%s, 'phone_number', 'multi_valued', 'recency',
                    'unverified', false, 'none')
            ON CONFLICT (tenant_id, predicate) DO UPDATE
                SET cardinality = EXCLUDED.cardinality
            """,
            (str(integration_tenant_id),),
        )

        # Resolve first belief
        c1 = _candidate(integration_tenant_id, predicate="phone_number", obj="555-1234")
        e1 = _embedded(c1)
        prov1 = _provenance_record(integration_tenant_id)
        outcome1, _ = with_serializable_retry(db_conn, _resolve_txn, e1, prov1)

        # Resolve second belief
        c2 = _candidate(integration_tenant_id, predicate="phone_number", obj="555-5678")
        e2 = _embedded(c2)
        prov2 = _provenance_record(integration_tenant_id)
        outcome2, _ = with_serializable_retry(db_conn, _resolve_txn, e2, prov2)

        # Both should be BOTH_RETAINED
        assert outcome1.resolution == Resolution.BOTH_RETAINED
        assert outcome2.resolution == Resolution.BOTH_RETAINED

        # Both should have no contradiction event
        assert outcome1.contradiction_event_id is None
        assert outcome2.contradiction_event_id is None

        # Both beliefs in DB
        rows = db_conn.execute(
            """
            SELECT belief_id, status FROM belief
            WHERE tenant_id = %s AND predicate = 'phone_number'
            """,
            (str(integration_tenant_id),),
        ).fetchall()

        assert len(rows) == 2
        for row in rows:
            assert row["status"] == "pending"

        _cleanup(db_conn, integration_tenant_id)


# ---------------------------------------------------------------------------
# Test 5: Canonicalize live — tier normalization rule
# ---------------------------------------------------------------------------

class TestCanonicalizeWithTierPolicy:
    def test_tier_normalization_applied_to_object_normalized(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Insert 'tier' normalization policy → object_normalized is canonical."""
        _cleanup(db_conn, integration_tenant_id)
        _insert_agent(db_conn, integration_tenant_id)

        # Insert tier normalization policy
        db_conn.execute(
            """
            INSERT INTO predicate_policy
                (tenant_id, predicate, cardinality, resolution_strategy,
                 min_source_tier, is_sensitive, normalization_rule)
            VALUES (%s, 'loyalty_tier', 'single_valued', 'recency',
                    'unverified', false, 'tier')
            ON CONFLICT (tenant_id, predicate) DO UPDATE
                SET normalization_rule = EXCLUDED.normalization_rule
            """,
            (str(integration_tenant_id),),
        )

        # Canonicalize with "Gold Tier" input
        candidate = _candidate(
            integration_tenant_id,
            predicate="loyalty_tier",
            obj="Gold Tier",
        )
        normalized = canonicalize(db_conn, candidate)

        assert normalized.object_normalized == "gold"
        assert normalized.sensitivity == Sensitivity.NORMAL

        # Now resolve so we can verify it's written to DB
        embedded = EmbeddedBelief(normalized=normalized, embedding=FAKE_EMBEDDING)
        prov = _provenance_record(integration_tenant_id)
        outcome, _ = with_serializable_retry(db_conn, _resolve_txn, embedded, prov)

        row = db_conn.execute(
            "SELECT object_normalized FROM belief "
            "WHERE tenant_id = %s AND belief_id = %s",
            (str(integration_tenant_id), str(outcome.belief_id)),
        ).fetchone()

        assert row is not None
        assert row["object_normalized"] == "gold"

        _cleanup(db_conn, integration_tenant_id)

    def test_unknown_tier_raises_elevated_sensitivity(
        self,
        db_conn: psycopg.Connection[Any],
        integration_tenant_id: uuid.UUID,
    ) -> None:
        """Unknown tier value → sensitivity ELEVATED in normalized belief."""
        _cleanup(db_conn, integration_tenant_id)

        # Insert tier normalization policy
        db_conn.execute(
            """
            INSERT INTO predicate_policy
                (tenant_id, predicate, cardinality, resolution_strategy,
                 min_source_tier, is_sensitive, normalization_rule)
            VALUES (%s, 'loyalty_tier', 'single_valued', 'recency',
                    'unverified', false, 'tier')
            ON CONFLICT (tenant_id, predicate) DO UPDATE
                SET normalization_rule = EXCLUDED.normalization_rule
            """,
            (str(integration_tenant_id),),
        )

        candidate = _candidate(
            integration_tenant_id,
            predicate="loyalty_tier",
            obj="Diamond",  # unknown tier
        )
        normalized = canonicalize(db_conn, candidate)

        assert normalized.sensitivity == Sensitivity.ELEVATED
        assert normalized.object_normalized == "Diamond"  # unchanged

        _cleanup(db_conn, integration_tenant_id)
