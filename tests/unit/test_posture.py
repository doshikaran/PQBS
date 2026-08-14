"""Unit tests for A18 PostureVerificationAgent.

All tests use mocked DB connections and mocked audit sinks.
No live DB or AWS required.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from pqbs.agents.integrity.a18_posture import (
    PostureDriftError,
    PostureState,
    PostureVerificationAgent,
    capture_live_posture,
    verify_posture,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
_TENANT = UUID("aaaa0000-0000-0000-0000-000000000001")

_FULL_BASELINE = PostureState(
    captured_at=_NOW,
    roles=["role_auditor", "role_consumer", "role_integrity", "role_producer", "role_semantics"],
    check_constraints={"belief": ["belief_status_check", "belief_sensitivity_check"]},
    views=["v_trusted_current"],
    vector_indexes=["belief_tenant_embedding_idx"],
    ttl_tables=["working_memory"],
)


def _make_conn_returning(
    roles: list[str],
    constraints: list[tuple[str, str]],
    views: list[str],
    indexes: list[str],
    ttl_tables: list[str],
) -> MagicMock:
    """Build a mock psycopg connection returning specific introspection rows."""

    def _execute_side_effect(sql: str, _p: Any = None) -> MagicMock:
        cursor = MagicMock()
        sql_lower = sql.lower()
        if "pg_roles" in sql_lower:
            cursor.fetchall.return_value = [{"rolname": r} for r in roles]
        elif "table_constraints" in sql_lower:
            cursor.fetchall.return_value = [
                {"table_name": t, "constraint_name": c} for t, c in constraints
            ]
        elif "information_schema.views" in sql_lower:
            cursor.fetchall.return_value = [{"table_name": v} for v in views]
        elif "pg_indexes" in sql_lower:
            cursor.fetchall.return_value = [{"indexname": i} for i in indexes]
        elif "crdb_internal" in sql_lower:
            cursor.fetchall.return_value = [{"name": t} for t in ttl_tables]
        else:
            cursor.fetchall.return_value = []
        return cursor

    conn = MagicMock()
    conn.execute.side_effect = _execute_side_effect
    return conn


# ---------------------------------------------------------------------------
# capture_live_posture
# ---------------------------------------------------------------------------

def test_capture_live_posture_returns_all_five_control_classes() -> None:
    conn = _make_conn_returning(
        roles=["role_consumer", "role_integrity", "role_producer", "role_semantics", "role_auditor"],
        constraints=[("belief", "belief_status_check"), ("belief", "belief_sensitivity_check")],
        views=["v_trusted_current"],
        indexes=["belief_tenant_embedding_idx"],
        ttl_tables=["working_memory"],
    )
    state = capture_live_posture(conn)

    assert sorted(state.roles) == sorted(_FULL_BASELINE.roles)
    assert "belief" in state.check_constraints
    assert "v_trusted_current" in state.views
    assert len(state.vector_indexes) == 1
    assert "working_memory" in state.ttl_tables


def test_capture_live_posture_tolerates_crdb_internal_failure() -> None:
    """A permission error on crdb_internal must not abort the entire capture."""

    def _execute(sql: str, _p: Any = None) -> MagicMock:
        cursor = MagicMock()
        if "crdb_internal" in sql.lower():
            raise Exception("permission denied for crdb_internal")
        cursor.fetchall.return_value = []
        return cursor

    conn = MagicMock()
    conn.execute.side_effect = _execute
    state = capture_live_posture(conn)
    assert state.ttl_tables == []


# ---------------------------------------------------------------------------
# verify_posture — clean match
# ---------------------------------------------------------------------------

def test_verify_posture_returns_no_findings_when_state_matches() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=list(_FULL_BASELINE.roles),
        check_constraints=dict(_FULL_BASELINE.check_constraints),
        views=list(_FULL_BASELINE.views),
        vector_indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )
    findings = verify_posture(live, _FULL_BASELINE)
    assert findings == []


# ---------------------------------------------------------------------------
# verify_posture — drift scenarios
# ---------------------------------------------------------------------------

def test_verify_posture_detects_missing_role() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=["role_auditor", "role_consumer", "role_integrity", "role_producer"],
        # role_semantics MISSING
        check_constraints=dict(_FULL_BASELINE.check_constraints),
        views=list(_FULL_BASELINE.views),
        vector_indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )
    findings = verify_posture(live, _FULL_BASELINE)
    role_findings = [f for f in findings if f.control_class == "role"]
    assert len(role_findings) == 1
    assert role_findings[0].control_name == "role_semantics"
    assert role_findings[0].observed == "missing"


def test_verify_posture_detects_unexpected_role() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=list(_FULL_BASELINE.roles) + ["role_backdoor"],
        check_constraints=dict(_FULL_BASELINE.check_constraints),
        views=list(_FULL_BASELINE.views),
        vector_indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )
    findings = verify_posture(live, _FULL_BASELINE)
    extra_role = [f for f in findings if f.control_name == "role_backdoor"]
    assert len(extra_role) == 1
    assert extra_role[0].observed == "exists"


def test_verify_posture_detects_missing_constraint() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=list(_FULL_BASELINE.roles),
        check_constraints={"belief": ["belief_status_check"]},  # missing belief_sensitivity_check
        views=list(_FULL_BASELINE.views),
        vector_indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )
    findings = verify_posture(live, _FULL_BASELINE)
    constraint_findings = [f for f in findings if f.control_class == "constraint"]
    assert any("belief_sensitivity_check" in f.control_name for f in constraint_findings)


def test_verify_posture_detects_missing_view() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=list(_FULL_BASELINE.roles),
        check_constraints=dict(_FULL_BASELINE.check_constraints),
        views=[],   # v_trusted_current MISSING
        vector_indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )
    findings = verify_posture(live, _FULL_BASELINE)
    view_findings = [f for f in findings if f.control_class == "view"]
    assert any("v_trusted_current" in f.control_name for f in view_findings)


def test_verify_posture_detects_missing_vector_index() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=list(_FULL_BASELINE.roles),
        check_constraints=dict(_FULL_BASELINE.check_constraints),
        views=list(_FULL_BASELINE.views),
        vector_indexes=[],   # index MISSING
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )
    findings = verify_posture(live, _FULL_BASELINE)
    idx_findings = [f for f in findings if f.control_class == "index"]
    assert len(idx_findings) == 1


def test_verify_posture_detects_missing_ttl() -> None:
    live = PostureState(
        captured_at=_NOW,
        roles=list(_FULL_BASELINE.roles),
        check_constraints=dict(_FULL_BASELINE.check_constraints),
        views=list(_FULL_BASELINE.views),
        vector_indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=[],   # working_memory TTL REMOVED
    )
    findings = verify_posture(live, _FULL_BASELINE)
    ttl_findings = [f for f in findings if f.control_class == "ttl"]
    assert any("working_memory" in f.control_name for f in ttl_findings)


# ---------------------------------------------------------------------------
# PostureVerificationAgent.run_verification_cycle
# ---------------------------------------------------------------------------

def _make_agent_with_baseline(baseline: PostureState, tmp_path: Path) -> tuple[PostureVerificationAgent, MagicMock]:
    baseline_path = tmp_path / "posture-baseline.json"
    baseline_path.write_text(
        json.dumps({"controls": baseline.to_dict()})
    )
    mock_sink = MagicMock()
    mock_sink.emit.return_value = "/tmp/pqbs_audit/fake"
    agent = PostureVerificationAgent(
        audit_sink=mock_sink,
        tenant_id=_TENANT,
        baseline_path=baseline_path,
    )
    return agent, mock_sink


def test_run_verification_cycle_writes_attestation_on_clean_match(tmp_path: Path) -> None:
    agent, mock_sink = _make_agent_with_baseline(_FULL_BASELINE, tmp_path)
    conn = _make_conn_returning(
        roles=list(_FULL_BASELINE.roles),
        constraints=[("belief", c) for c in _FULL_BASELINE.check_constraints.get("belief", [])],
        views=list(_FULL_BASELINE.views),
        indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )

    result = agent.run_verification_cycle(conn)

    assert not result.has_drift
    assert result.findings == []
    mock_sink.emit.assert_called_once()
    emitted: Any = mock_sink.emit.call_args[0][0]
    assert emitted.event_type.value == "posture_attested"


def test_run_verification_cycle_raises_and_writes_audit_on_drift(tmp_path: Path) -> None:
    agent, mock_sink = _make_agent_with_baseline(_FULL_BASELINE, tmp_path)
    # role_consumer was REVOKED — simulates deliberate attack or misconfiguration
    conn = _make_conn_returning(
        roles=["role_auditor", "role_integrity", "role_producer", "role_semantics"],
        constraints=[("belief", c) for c in _FULL_BASELINE.check_constraints.get("belief", [])],
        views=list(_FULL_BASELINE.views),
        indexes=list(_FULL_BASELINE.vector_indexes),
        ttl_tables=list(_FULL_BASELINE.ttl_tables),
    )

    with pytest.raises(PostureDriftError) as exc_info:
        agent.run_verification_cycle(conn)

    assert any(f.control_name == "role_consumer" for f in exc_info.value.drifts)
    mock_sink.emit.assert_called_once()
    emitted = mock_sink.emit.call_args[0][0]
    assert emitted.event_type.value == "posture_drift_detected"


def test_a18_cannot_remediate() -> None:
    """A18 has no REVOKE, GRANT, or ALTER method — negative test."""
    mock_sink = MagicMock()
    agent = PostureVerificationAgent(audit_sink=mock_sink, tenant_id=_TENANT)

    assert not hasattr(agent, "revoke_grant"), "A18 must not have a revoke_grant method"
    assert not hasattr(agent, "remediate"), "A18 must not have a remediate method"
    assert not hasattr(agent, "alter_role"), "A18 must not have an alter_role method"


# ---------------------------------------------------------------------------
# PostureState serialization
# ---------------------------------------------------------------------------

def test_posture_state_round_trips_through_dict() -> None:
    original = _FULL_BASELINE
    d = original.to_dict()
    restored = PostureState.from_dict(d)

    assert sorted(restored.roles) == sorted(original.roles)
    assert restored.views == original.views
    assert restored.ttl_tables == original.ttl_tables
