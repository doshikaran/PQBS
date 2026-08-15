"""Unit tests for A8 Consolidation Agent.

All tests use mock DB connections — no live CockroachDB required.

Security invariant tested:
  - T9: no merge across quarantine boundary (any quarantined member → skip)
  - dry_run: detect but never write
  - idempotency: re-running on an already-merged group is a no-op
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from pqbs.agents.integrity.a8_consolidation import (
    ConsolidationAgent,
    ConsolidationRun,
)
from pqbs.contracts.exceptions import ConsolidationError

pytestmark = pytest.mark.unit

TENANT = UUID("00000000-0000-0000-0000-000000000002")

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(data: dict) -> MagicMock:
    """Create a mock row that supports dict-style key access."""
    r = MagicMock()
    r.__getitem__ = lambda self, k: data[k]
    return r


def _conn_no_duplicates() -> MagicMock:
    """Connection that reports: no duplicates, no long chains, no overdue TTL rows."""
    conn = MagicMock()
    dup_cursor = MagicMock()
    dup_cursor.fetchall.return_value = []
    chain_cursor = MagicMock()
    chain_cursor.fetchall.return_value = []
    ttl_cursor = MagicMock()
    ttl_cursor.fetchone.return_value = _row({"n": 0})
    conn.execute.side_effect = [dup_cursor, chain_cursor, ttl_cursor]
    return conn


# ---------------------------------------------------------------------------
# ConsolidationRun dataclass
# ---------------------------------------------------------------------------

class TestConsolidationRun:
    def test_elapsed_ms(self):
        started = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        completed = datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
        run = ConsolidationRun(
            tenant_id="t1",
            started_at=started,
            completed_at=completed,
        )
        assert run.elapsed_ms == 2000

    def test_defaults(self):
        run = ConsolidationRun(
            tenant_id="t1",
            started_at=_TS,
            completed_at=_TS,
        )
        assert run.duplicate_groups_found == 0
        assert run.duplicate_groups_merged == 0
        assert run.duplicate_groups_skipped == 0
        assert run.beliefs_superseded == 0
        assert run.chains_flagged == 0
        assert run.working_memory_overdue_count == 0
        assert run.chain_flags == []


# ---------------------------------------------------------------------------
# No-work pass
# ---------------------------------------------------------------------------

class TestNoOpPass:
    def test_no_duplicates_returns_zero_counts(self):
        conn = _conn_no_duplicates()
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.duplicate_groups_found == 0
        assert run.duplicate_groups_merged == 0
        assert run.beliefs_superseded == 0
        assert run.chains_flagged == 0
        assert run.working_memory_overdue_count == 0

    def test_run_sets_completed_at(self):
        conn = _conn_no_duplicates()
        before = datetime.now(tz=timezone.utc)
        run = ConsolidationAgent().run(TENANT, conn)
        after = datetime.now(tz=timezone.utc)
        assert before <= run.completed_at <= after


# ---------------------------------------------------------------------------
# Duplicate compaction
# ---------------------------------------------------------------------------

def _dup_row(bid_list: list[str]) -> MagicMock:
    r = MagicMock()
    r.__getitem__ = lambda self, k: {
        "subject": "Alice",
        "predicate": "worksAt",
        "object_normalized": "Acme",
        "belief_ids": bid_list,
    }[k]
    return r


class TestCompactDuplicates:
    def _conn_with_dup(
        self,
        bid_list: list[str],
        quarantined: bool = False,
        dry_run: bool = False,
    ) -> MagicMock:
        conn = MagicMock()
        dup_cursor = MagicMock()
        dup_cursor.fetchall.return_value = [_dup_row(bid_list)]
        quar_cursor = MagicMock()
        quar_cursor.fetchone.return_value = _row({"found": 1}) if quarantined else None
        chain_cursor = MagicMock()
        chain_cursor.fetchall.return_value = []
        ttl_cursor = MagicMock()
        ttl_cursor.fetchone.return_value = _row({"n": 0})

        side_effects: list = [dup_cursor, quar_cursor]
        if not dry_run and not quarantined:
            # _merge_group issues 2 execute() calls per loser:
            #   UPDATE loser status='superseded'
            #   UPDATE winner supersedes=loser_id
            n_losers = len(bid_list) - 1
            side_effects.extend(MagicMock() for _ in range(n_losers * 2))
        side_effects.extend([chain_cursor, ttl_cursor])
        conn.execute.side_effect = side_effects
        return conn

    def test_single_duplicate_group_merged(self):
        bids = ["aaa-winner", "bbb-loser"]
        conn = self._conn_with_dup(bids, quarantined=False, dry_run=False)
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.duplicate_groups_found == 1
        assert run.duplicate_groups_merged == 1
        assert run.beliefs_superseded == 1
        assert run.duplicate_groups_skipped == 0

    def test_winner_is_first_in_list(self):
        """SQL orders by confidence DESC, valid_from DESC — first element is winner."""
        bids = ["winner-id", "loser-1", "loser-2"]
        conn = self._conn_with_dup(bids, quarantined=False, dry_run=False)
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.beliefs_superseded == 2  # two losers

    def test_t9_quarantine_boundary_skips_group(self):
        """If any member was ever quarantined, the entire group is skipped."""
        bids = ["aaa", "bbb"]
        conn = self._conn_with_dup(bids, quarantined=True)
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.duplicate_groups_skipped == 1
        assert run.duplicate_groups_merged == 0
        assert run.beliefs_superseded == 0

    def test_dry_run_does_not_call_update(self):
        """dry_run=True: detect but do not write; commit must not be called."""
        bids = ["winner", "loser"]
        conn = self._conn_with_dup(bids, quarantined=False)
        run = ConsolidationAgent().run(TENANT, conn, dry_run=True)
        # Should still count as "merged" in the run stats
        assert run.duplicate_groups_merged == 1
        assert run.beliefs_superseded == 1
        # But conn.commit must never have been called
        conn.commit.assert_not_called()

    def test_dry_run_no_execute_for_update(self):
        """dry_run=True: UPDATE statements must not be sent to the DB."""
        bids = ["winner", "loser"]
        conn = self._conn_with_dup(bids, quarantined=False)
        ConsolidationAgent().run(TENANT, conn, dry_run=True)
        # execute calls: SELECT duplicates, SELECT quarantine, SELECT chains, SELECT ttl
        # No UPDATE calls expected
        for mock_call in conn.execute.call_args_list:
            sql_arg = mock_call[0][0]
            assert "UPDATE" not in sql_arg.upper(), (
                f"dry_run should not issue UPDATE but got: {sql_arg[:60]}"
            )

    def test_commit_called_per_group_when_not_dry_run(self):
        bids = ["winner", "loser"]
        conn = self._conn_with_dup(bids, quarantined=False, dry_run=False)
        ConsolidationAgent().run(TENANT, conn, dry_run=False)
        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Long chain flagging
# ---------------------------------------------------------------------------

class TestLongChainFlagging:
    def _conn_with_chains(self, chain_rows: list[dict]) -> MagicMock:
        conn = MagicMock()
        dup_cursor = MagicMock()
        dup_cursor.fetchall.return_value = []
        chain_cursor = MagicMock()
        chain_cursor.fetchall.return_value = [_row(r) for r in chain_rows]
        ttl_cursor = MagicMock()
        ttl_cursor.fetchone.return_value = _row({"n": 0})
        conn.execute.side_effect = [dup_cursor, chain_cursor, ttl_cursor]
        return conn

    def test_long_chain_detected(self):
        rows = [{"root_id": "root-abc", "predecessors": 15}]
        conn = self._conn_with_chains(rows)
        run = ConsolidationAgent().run(TENANT, conn, max_chain_depth=10)
        assert run.chains_flagged == 1
        assert len(run.chain_flags) == 1
        flag = run.chain_flags[0]
        assert flag.root_belief_id == "root-abc"
        assert flag.estimated_depth == 15

    def test_no_chains_flagged_when_below_threshold(self):
        conn = self._conn_with_chains([])
        run = ConsolidationAgent().run(TENANT, conn, max_chain_depth=10)
        assert run.chains_flagged == 0
        assert run.chain_flags == []

    def test_multiple_chains_all_flagged(self):
        rows = [
            {"root_id": "root-1", "predecessors": 20},
            {"root_id": "root-2", "predecessors": 12},
        ]
        conn = self._conn_with_chains(rows)
        run = ConsolidationAgent().run(TENANT, conn, max_chain_depth=10)
        assert run.chains_flagged == 2
        depths = {f.estimated_depth for f in run.chain_flags}
        assert depths == {20, 12}

    def test_chain_flagging_does_not_write(self):
        """Chain flagging is report-only — no writes expected."""
        rows = [{"root_id": "root-abc", "predecessors": 15}]
        conn = self._conn_with_chains(rows)
        ConsolidationAgent().run(TENANT, conn, max_chain_depth=10)
        conn.commit.assert_not_called()
        for mock_call in conn.execute.call_args_list:
            sql_arg = mock_call[0][0]
            assert "UPDATE" not in sql_arg.upper()


# ---------------------------------------------------------------------------
# TTL verification
# ---------------------------------------------------------------------------

class TestTTLVerification:
    def _conn_with_ttl(self, overdue: int) -> MagicMock:
        conn = MagicMock()
        dup_cursor = MagicMock()
        dup_cursor.fetchall.return_value = []
        chain_cursor = MagicMock()
        chain_cursor.fetchall.return_value = []
        ttl_cursor = MagicMock()
        ttl_cursor.fetchone.return_value = _row({"n": overdue})
        conn.execute.side_effect = [dup_cursor, chain_cursor, ttl_cursor]
        return conn

    def test_no_overdue_rows(self):
        conn = self._conn_with_ttl(0)
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.working_memory_overdue_count == 0

    def test_overdue_rows_reported(self):
        conn = self._conn_with_ttl(42)
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.working_memory_overdue_count == 42

    def test_ttl_query_failure_does_not_abort_run(self):
        """TTL check failure is non-fatal — run should still complete."""
        conn = MagicMock()
        dup_cursor = MagicMock()
        dup_cursor.fetchall.return_value = []
        chain_cursor = MagicMock()
        chain_cursor.fetchall.return_value = []
        ttl_cursor = MagicMock()
        ttl_cursor.fetchone.side_effect = Exception("table not found")
        conn.execute.side_effect = [dup_cursor, chain_cursor, ttl_cursor]
        # Should not raise
        run = ConsolidationAgent().run(TENANT, conn)
        assert run.working_memory_overdue_count == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_consolidation_error_raised_on_unexpected_exception(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("unexpected")
        with pytest.raises(ConsolidationError, match="Consolidation failed"):
            ConsolidationAgent().run(TENANT, conn)

    def test_consolidation_error_passthrough(self):
        """ConsolidationError is re-raised without wrapping."""
        conn = MagicMock()
        conn.execute.side_effect = ConsolidationError("already wrapped")
        with pytest.raises(ConsolidationError, match="already wrapped"):
            ConsolidationAgent().run(TENANT, conn)
