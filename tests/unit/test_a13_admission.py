"""Unit tests for A13 Admission Agent (rate limiting & queue depth throttle).

All tests use mock DB connections — no live CockroachDB required.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from pqbs.agents.producer.a13_admission import AdmissionAgent, AdmissionDecision
from pqbs.contracts.exceptions import AdmissionRejectedError, AdmissionThrottledError

pytestmark = pytest.mark.unit

TENANT = UUID("00000000-0000-0000-0000-000000000001")
AGENT_ID = "agent-abc"

_QUOTA = 100
_THROTTLE = 500


def _make_conn(writes_in_window: int, pending_depth: int) -> MagicMock:
    """Return a mock psycopg connection that answers quota and depth queries."""
    conn = MagicMock()
    writes_cursor = MagicMock()
    writes_cursor.fetchone.return_value = {"n": writes_in_window}
    pending_cursor = MagicMock()
    pending_cursor.fetchone.return_value = {"n": pending_depth}
    conn.execute.side_effect = [writes_cursor, pending_cursor]
    return conn


def _patched_env(quota: int = _QUOTA, throttle: int = _THROTTLE):
    return patch.dict(
        os.environ,
        {
            "PQBS_RATE_LIMIT_PER_AGENT_PER_HOUR": str(quota),
            "PQBS_QUEUE_DEPTH_THROTTLE": str(throttle),
        },
    )


class TestAdmissionDecision:
    def test_admitted_property(self):
        d = AdmissionDecision(
            action="admit",
            reason="ok",
            writes_in_window=0,
            pending_depth=0,
            quota=100,
            throttle_limit=500,
        )
        assert d.admitted is True

    def test_reject_not_admitted(self):
        d = AdmissionDecision(
            action="reject",
            reason="quota",
            writes_in_window=100,
            pending_depth=0,
            quota=100,
            throttle_limit=500,
        )
        assert d.admitted is False

    def test_throttle_not_admitted(self):
        d = AdmissionDecision(
            action="throttle",
            reason="queue",
            writes_in_window=0,
            pending_depth=500,
            quota=100,
            throttle_limit=500,
        )
        assert d.admitted is False

    def test_str_repr(self):
        d = AdmissionDecision(
            action="admit",
            reason="within limits",
            writes_in_window=5,
            pending_depth=10,
            quota=100,
            throttle_limit=500,
        )
        s = str(d)
        assert "admit" in s
        assert "5/100" in s


class TestAdmissionAgentCheck:
    def test_admit_within_limits(self):
        conn = _make_conn(writes_in_window=50, pending_depth=100)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "admit"
        assert decision.writes_in_window == 50
        assert decision.pending_depth == 100

    def test_reject_at_quota_boundary(self):
        """Exactly at quota → reject."""
        conn = _make_conn(writes_in_window=_QUOTA, pending_depth=0)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "reject"
        assert decision.writes_in_window == _QUOTA

    def test_reject_over_quota(self):
        conn = _make_conn(writes_in_window=_QUOTA + 50, pending_depth=0)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "reject"

    def test_throttle_at_queue_depth_boundary(self):
        """Exactly at throttle limit → throttle."""
        conn = _make_conn(writes_in_window=0, pending_depth=_THROTTLE)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "throttle"

    def test_throttle_over_queue_depth(self):
        conn = _make_conn(writes_in_window=0, pending_depth=_THROTTLE + 1000)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "throttle"

    def test_reject_takes_priority_over_throttle(self):
        """When both quota and depth are exceeded, reject takes priority."""
        conn = _make_conn(writes_in_window=_QUOTA + 1, pending_depth=_THROTTLE + 1)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "reject"

    def test_quota_check_db_failure_failsafe_throttle(self):
        """If quota query fails, fail-safe: throttle (not reject, not admit)."""
        conn = MagicMock()
        conn.execute.side_effect = Exception("connection timeout")
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.action == "throttle"
        assert "fail-safe" in decision.reason

    def test_pending_depth_db_failure_conservative_no_throttle(self):
        """If only depth query fails, we count pending=0 (conservative: don't throttle)."""
        conn = MagicMock()
        writes_cursor = MagicMock()
        writes_cursor.fetchone.return_value = {"n": 0}
        conn.execute.side_effect = [writes_cursor, Exception("timeout")]
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        # With writes=0 and pending=0 (default on failure), should admit
        assert decision.action == "admit"

    def test_correct_quota_and_throttle_from_env(self):
        conn = _make_conn(writes_in_window=0, pending_depth=0)
        with _patched_env(quota=42, throttle=99):
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.quota == 42
        assert decision.throttle_limit == 99

    def test_decision_carries_correct_counts(self):
        conn = _make_conn(writes_in_window=7, pending_depth=200)
        with _patched_env():
            decision = AdmissionAgent().check(AGENT_ID, TENANT, conn)
        assert decision.writes_in_window == 7
        assert decision.pending_depth == 200


class TestAdmissionAgentEnforce:
    def test_enforce_admit_returns_decision(self):
        conn = _make_conn(writes_in_window=0, pending_depth=0)
        with _patched_env():
            decision = AdmissionAgent().enforce(AGENT_ID, TENANT, conn)
        assert decision.admitted is True

    def test_enforce_reject_raises_admission_rejected(self):
        conn = _make_conn(writes_in_window=_QUOTA, pending_depth=0)
        with _patched_env():
            with pytest.raises(AdmissionRejectedError):
                AdmissionAgent().enforce(AGENT_ID, TENANT, conn)

    def test_enforce_throttle_raises_admission_throttled(self):
        conn = _make_conn(writes_in_window=0, pending_depth=_THROTTLE)
        with _patched_env():
            with pytest.raises(AdmissionThrottledError):
                AdmissionAgent().enforce(AGENT_ID, TENANT, conn)

    def test_enforce_error_messages_contain_reason(self):
        conn = _make_conn(writes_in_window=_QUOTA, pending_depth=0)
        with _patched_env():
            with pytest.raises(AdmissionRejectedError, match="quota"):
                AdmissionAgent().enforce(AGENT_ID, TENANT, conn)

    def test_enforce_throttle_error_messages_contain_reason(self):
        conn = _make_conn(writes_in_window=0, pending_depth=_THROTTLE)
        with _patched_env():
            with pytest.raises(AdmissionThrottledError, match="queue depth"):
                AdmissionAgent().enforce(AGENT_ID, TENANT, conn)
