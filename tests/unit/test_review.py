"""Unit tests for ReviewAgent (A14).

All tests use mocked DB connections and a mocked AuditSink.
No live DB or AWS credentials required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from pqbs.contracts import AuditEventType
from pqbs.contracts.exceptions import QuarantineError
from pqbs.agents.integrity.a14_review import ReviewAgent
from pqbs.agents.integrity.audit_sink import AuditSink

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_TENANT = UUID("bbbb0000-0000-0000-0000-000000000001")


def _make_quarantine_row(
    quarantine_id: UUID,
    belief_id: UUID,
    *,
    disposition: str = "held",
) -> dict[str, Any]:
    return {
        "quarantine_id": str(quarantine_id),
        "belief_id": str(belief_id),
        "disposition": disposition,
    }


def _make_conn_with_quarantine(
    quarantine_id: UUID,
    belief_id: UUID,
    *,
    disposition: str = "held",
    list_rows: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Mock conn that returns a quarantine row for SELECT and allows UPDATE."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.execute.return_value = cursor

    quarantine_row = _make_quarantine_row(quarantine_id, belief_id, disposition=disposition)

    def fetchone_side_effect() -> dict[str, Any] | None:
        return quarantine_row

    def fetchall_side_effect() -> list[dict[str, Any]]:
        return list_rows or []

    cursor.fetchone.side_effect = fetchone_side_effect
    cursor.fetchall.side_effect = fetchall_side_effect
    return conn


def _make_sink(tmp_path: Path) -> AuditSink:
    return AuditSink(_local_dir=str(tmp_path))


class TestRelease:
    def test_release_requires_reviewed_by(self, tmp_path: Path) -> None:
        """release() with empty reviewed_by raises ValueError."""
        agent = ReviewAgent()
        conn = MagicMock()
        sink = _make_sink(tmp_path)

        with pytest.raises(ValueError, match="reviewed_by is required"):
            agent.release(
                quarantine_id=uuid4(),
                tenant_id=_TENANT,
                reviewed_by="",
                review_notes="notes",
                conn=conn,
                audit_sink=sink,
            )

    def test_release_requires_reviewed_by_not_whitespace(self, tmp_path: Path) -> None:
        """reviewed_by = whitespace-only string raises ValueError."""
        agent = ReviewAgent()
        conn = MagicMock()
        sink = _make_sink(tmp_path)

        with pytest.raises(ValueError, match="reviewed_by is required"):
            agent.release(
                quarantine_id=uuid4(),
                tenant_id=_TENANT,
                reviewed_by="   ",
                review_notes="notes",
                conn=conn,
                audit_sink=sink,
            )

    def test_release_updates_belief_status_to_trusted(self, tmp_path: Path) -> None:
        """release() issues UPDATE setting status='trusted'."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(quarantine_id, belief_id)
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        agent.release(
            quarantine_id=quarantine_id,
            tenant_id=_TENANT,
            reviewed_by="reviewer-alice",
            review_notes="Verified safe",
            conn=conn,
            audit_sink=sink,
        )

        # Verify there was an UPDATE to belief with status='trusted'
        all_calls = [str(c) for c in conn.execute.call_args_list]
        belief_update_calls = [
            c for c in all_calls
            if "UPDATE belief" in c and "trusted" in c
        ]
        assert len(belief_update_calls) >= 1

    def test_release_updates_quarantine_disposition(self, tmp_path: Path) -> None:
        """release() issues UPDATE setting disposition='released'."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(quarantine_id, belief_id)
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        agent.release(
            quarantine_id=quarantine_id,
            tenant_id=_TENANT,
            reviewed_by="reviewer-alice",
            review_notes="Verified safe",
            conn=conn,
            audit_sink=sink,
        )

        all_calls = [str(c) for c in conn.execute.call_args_list]
        quarantine_update_calls = [
            c for c in all_calls
            if "UPDATE quarantine" in c and "released" in c
        ]
        assert len(quarantine_update_calls) >= 1

    def test_release_not_held_raises_quarantine_error(self, tmp_path: Path) -> None:
        """release() on a non-held quarantine record raises QuarantineError."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(
            quarantine_id, belief_id, disposition="released"
        )
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        with pytest.raises(QuarantineError, match="disposition is already"):
            agent.release(
                quarantine_id=quarantine_id,
                tenant_id=_TENANT,
                reviewed_by="reviewer",
                review_notes="notes",
                conn=conn,
                audit_sink=sink,
            )

    def test_release_not_found_raises_quarantine_error(self, tmp_path: Path) -> None:
        """release() when no quarantine record exists raises QuarantineError."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.execute.return_value = cursor
        cursor.fetchone.return_value = None  # record not found
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        with pytest.raises(QuarantineError, match="not found"):
            agent.release(
                quarantine_id=uuid4(),
                tenant_id=_TENANT,
                reviewed_by="reviewer",
                review_notes="notes",
                conn=conn,
                audit_sink=sink,
            )

    def test_release_emits_belief_released_audit_record(self, tmp_path: Path) -> None:
        """release() writes a BELIEF_RELEASED audit record."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(quarantine_id, belief_id)
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        agent.release(
            quarantine_id=quarantine_id,
            tenant_id=_TENANT,
            reviewed_by="reviewer-alice",
            review_notes="Verified safe",
            conn=conn,
            audit_sink=sink,
        )

        tenant_dir = Path(tmp_path) / str(_TENANT)
        released_files = list(
            (tenant_dir / AuditEventType.BELIEF_RELEASED.value).glob("*.json")
        )
        assert len(released_files) == 1


class TestReject:
    def test_reject_requires_reviewed_by(self, tmp_path: Path) -> None:
        """reject() with empty reviewed_by raises ValueError."""
        agent = ReviewAgent()
        conn = MagicMock()
        sink = _make_sink(tmp_path)

        with pytest.raises(ValueError, match="reviewed_by is required"):
            agent.reject(
                quarantine_id=uuid4(),
                tenant_id=_TENANT,
                reviewed_by="",
                review_notes="notes",
                conn=conn,
                audit_sink=sink,
            )

    def test_reject_updates_belief_status_to_rejected(self, tmp_path: Path) -> None:
        """reject() issues UPDATE setting status='rejected'."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(quarantine_id, belief_id)
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        agent.reject(
            quarantine_id=quarantine_id,
            tenant_id=_TENANT,
            reviewed_by="reviewer-bob",
            review_notes="Confirmed malicious",
            conn=conn,
            audit_sink=sink,
        )

        all_calls = [str(c) for c in conn.execute.call_args_list]
        belief_update_calls = [
            c for c in all_calls
            if "UPDATE belief" in c and "rejected" in c
        ]
        assert len(belief_update_calls) >= 1

    def test_reject_updates_quarantine_disposition(self, tmp_path: Path) -> None:
        """reject() issues UPDATE setting disposition='rejected'."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(quarantine_id, belief_id)
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        agent.reject(
            quarantine_id=quarantine_id,
            tenant_id=_TENANT,
            reviewed_by="reviewer-bob",
            review_notes="Confirmed malicious",
            conn=conn,
            audit_sink=sink,
        )

        all_calls = [str(c) for c in conn.execute.call_args_list]
        quarantine_update_calls = [
            c for c in all_calls
            if "UPDATE quarantine" in c and "rejected" in c
        ]
        assert len(quarantine_update_calls) >= 1

    def test_reject_emits_belief_rejected_audit_record(self, tmp_path: Path) -> None:
        """reject() writes a BELIEF_REJECTED audit record."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(quarantine_id, belief_id)
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        agent.reject(
            quarantine_id=quarantine_id,
            tenant_id=_TENANT,
            reviewed_by="reviewer-bob",
            review_notes="Confirmed malicious",
            conn=conn,
            audit_sink=sink,
        )

        tenant_dir = Path(tmp_path) / str(_TENANT)
        rejected_files = list(
            (tenant_dir / AuditEventType.BELIEF_REJECTED.value).glob("*.json")
        )
        assert len(rejected_files) == 1

    def test_reject_not_held_raises_quarantine_error(self, tmp_path: Path) -> None:
        """reject() on a non-held record raises QuarantineError."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        conn = _make_conn_with_quarantine(
            quarantine_id, belief_id, disposition="rejected"
        )
        sink = _make_sink(tmp_path)

        agent = ReviewAgent()
        with pytest.raises(QuarantineError, match="disposition is already"):
            agent.reject(
                quarantine_id=quarantine_id,
                tenant_id=_TENANT,
                reviewed_by="reviewer",
                review_notes="notes",
                conn=conn,
                audit_sink=sink,
            )


class TestListPendingReview:
    def test_list_pending_review_queries_held_only(self) -> None:
        """list_pending_review() SQL filters disposition='held'."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.execute.return_value = cursor
        cursor.fetchall.return_value = []

        agent = ReviewAgent()
        result = agent.list_pending_review(_TENANT, conn)

        # Verify the SQL was called with 'held' filter
        assert conn.execute.called
        call_args = conn.execute.call_args
        sql = call_args[0][0]
        assert "disposition = 'held'" in sql or "disposition='held'" in sql

    def test_list_pending_review_returns_dicts(self) -> None:
        """list_pending_review() returns a list of dicts."""
        quarantine_id = uuid4()
        belief_id = uuid4()
        fake_row = {
            "quarantine_id": str(quarantine_id),
            "belief_id": str(belief_id),
            "reason_code": "anomalous_embedding",
            "quarantined_at": _NOW,
            "subject": "Alice",
            "predicate": "works_at",
            "object": "Acme Corp",
            "author_agent_id": "test-agent",
            "verdict": "quarantined",
            "trust_score": 0.9,
            "signal_scores": "{}",
            "screened_at": _NOW,
        }

        conn = MagicMock()
        cursor = MagicMock()
        conn.execute.return_value = cursor
        cursor.fetchall.return_value = [fake_row]

        agent = ReviewAgent()
        result = agent.list_pending_review(_TENANT, conn)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["quarantine_id"] == str(quarantine_id)
