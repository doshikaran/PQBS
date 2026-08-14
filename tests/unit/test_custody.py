"""Unit tests for A19 SubstrateCustodyAgent.

All tests mock subprocess (ccloud CLI) and audit sink.
No live ccloud, CockroachDB Cloud, or AWS required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from pqbs.agents.integrity.a19_custody import (
    BackupEntry,
    SubstrateCustodyAgent,
    _parse_audit_event,
    _parse_backup_entry,
)

pytestmark = pytest.mark.unit

_CLUSTER_ID = "71b13406-ccdb-481e-b0dc-f4aa75718234"
_TENANT = UUID("bbbb0000-0000-0000-0000-000000000002")
_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

_RAW_AUDIT_EVENT = {
    "id": "evt-001",
    "type": "sql.user.create",
    "actor": "service-account-test@pqbs",
    "resource": _CLUSTER_ID,
    "timestamp": "2026-08-14T11:00:00Z",
    "target_user": "new_sql_user",
}

_RAW_BACKUP = {
    "id": "bk-001",
    "status": "completed",
    "started_at": "2026-08-14T00:00:00Z",
    "completed_at": "2026-08-14T00:30:00Z",
    "covers_from": "2026-08-13T00:00:00Z",
    "covers_to": "2026-08-14T00:30:00Z",
}


def _make_agent(mock_sink: Any | None = None) -> SubstrateCustodyAgent:
    if mock_sink is None:
        mock_sink = MagicMock()
        mock_sink.emit.return_value = "/tmp/pqbs_audit/fake"
    return SubstrateCustodyAgent(
        audit_sink=mock_sink,
        cluster_id=_CLUSTER_ID,
        tenant_id=_TENANT,
    )


# ---------------------------------------------------------------------------
# _parse_audit_event
# ---------------------------------------------------------------------------

def test_parse_audit_event_extracts_fields() -> None:
    event = _parse_audit_event(_RAW_AUDIT_EVENT)

    assert event.event_id == "evt-001"
    assert event.event_type == "sql.user.create"
    assert event.actor == "service-account-test@pqbs"
    assert event.timestamp.year == 2026
    assert "target_user" in event.details


def test_parse_audit_event_handles_missing_timestamp() -> None:
    raw = {"id": "evt-002", "type": "cluster.update"}
    event = _parse_audit_event(raw)
    assert event.timestamp is not None  # defaults to now


# ---------------------------------------------------------------------------
# _parse_backup_entry
# ---------------------------------------------------------------------------

def test_parse_backup_entry_extracts_fields() -> None:
    entry = _parse_backup_entry(_RAW_BACKUP, _CLUSTER_ID)

    assert entry.backup_id == "bk-001"
    assert entry.status == "completed"
    assert entry.covers_from is not None
    assert entry.covers_to is not None
    assert entry.covers_from < entry.covers_to


def test_parse_backup_entry_handles_missing_timestamps() -> None:
    entry = _parse_backup_entry({"id": "bk-002", "status": "running"}, _CLUSTER_ID)
    assert entry.covers_from is None
    assert entry.covers_to is None


# ---------------------------------------------------------------------------
# poll_control_plane_audit — ccloud mocked via subprocess
# ---------------------------------------------------------------------------

def test_poll_control_plane_audit_parses_events() -> None:
    with patch("pqbs.agents.integrity.a19_custody._run_ccloud") as mock_run:
        mock_run.return_value = [_RAW_AUDIT_EVENT]
        agent = _make_agent()
        events = agent.poll_control_plane_audit()

    assert len(events) == 1
    assert events[0].event_type == "sql.user.create"
    assert events[0].actor == "service-account-test@pqbs"


def test_poll_control_plane_audit_returns_empty_when_ccloud_missing() -> None:
    with patch(
        "pqbs.agents.integrity.a19_custody._run_ccloud",
        side_effect=FileNotFoundError("ccloud not found"),
    ):
        agent = _make_agent()
        events = agent.poll_control_plane_audit()

    assert events == []


def test_poll_control_plane_audit_returns_empty_on_runtime_error() -> None:
    with patch(
        "pqbs.agents.integrity.a19_custody._run_ccloud",
        side_effect=RuntimeError("exit code 1: unauthorized"),
    ):
        agent = _make_agent()
        events = agent.poll_control_plane_audit()

    assert events == []


# ---------------------------------------------------------------------------
# ingest_events_to_worm
# ---------------------------------------------------------------------------

def test_ingest_events_to_worm_emits_one_record_per_event() -> None:
    mock_sink = MagicMock()
    mock_sink.emit.return_value = "/tmp/fake"
    agent = _make_agent(mock_sink)

    events = [_parse_audit_event(_RAW_AUDIT_EVENT)]
    ingested = agent.ingest_events_to_worm(events)

    assert ingested == 1
    mock_sink.emit.assert_called_once()
    record = mock_sink.emit.call_args[0][0]
    assert record.event_type.value == "control_plane_audit_ingested"
    assert record.after["ccloud_event_type"] == "sql.user.create"
    assert record.after["actor"] == "service-account-test@pqbs"


def test_ingest_events_to_worm_returns_zero_for_empty_list() -> None:
    agent = _make_agent()
    assert agent.ingest_events_to_worm([]) == 0


def test_ingest_events_to_worm_continues_after_sink_failure() -> None:
    mock_sink = MagicMock()
    mock_sink.emit.side_effect = [Exception("sink down"), None]
    agent = _make_agent(mock_sink)

    raw2 = {**_RAW_AUDIT_EVENT, "id": "evt-003"}
    events = [_parse_audit_event(_RAW_AUDIT_EVENT), _parse_audit_event(raw2)]
    ingested = agent.ingest_events_to_worm(events)

    # first emit failed, second succeeded
    assert ingested == 1


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------

def test_list_backups_returns_parsed_entries() -> None:
    with patch("pqbs.agents.integrity.a19_custody._run_ccloud") as mock_run:
        mock_run.return_value = [_RAW_BACKUP]
        agent = _make_agent()
        backups = agent.list_backups(force_refresh=True)

    assert len(backups) == 1
    assert backups[0].backup_id == "bk-001"
    assert backups[0].status == "completed"


def test_list_backups_uses_cache_on_second_call() -> None:
    with patch("pqbs.agents.integrity.a19_custody._run_ccloud") as mock_run:
        mock_run.return_value = [_RAW_BACKUP]
        agent = _make_agent()
        agent.list_backups()
        agent.list_backups()   # should NOT call ccloud again

    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# mechanism_3_query
# ---------------------------------------------------------------------------

def test_mechanism_3_query_finds_covering_backup() -> None:
    backup_covers_to = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)
    backup_covers_from = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    backup = BackupEntry(
        backup_id="bk-001",
        cluster_id=_CLUSTER_ID,
        started_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
        completed_at=backup_covers_to,
        status="completed",
        covers_from=backup_covers_from,
        covers_to=backup_covers_to,
    )

    with patch("pqbs.agents.integrity.a19_custody._run_ccloud") as mock_run:
        mock_run.return_value = [_RAW_BACKUP]
        agent = _make_agent()
        agent._backup_cache = [backup]

    query_time = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    result = agent.mechanism_3_query(query_time)

    assert result.has_coverage is True
    assert result.covering_backup is not None
    assert result.covering_backup.backup_id == "bk-001"
    assert "cannot restore autonomously" in result.coverage_note


def test_mechanism_3_query_reports_gap_when_no_covering_backup() -> None:
    backup = BackupEntry(
        backup_id="bk-old",
        cluster_id=_CLUSTER_ID,
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        status="completed",
        covers_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        covers_to=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    agent = _make_agent()
    agent._backup_cache = [backup]

    # Query for June — no backup covers it
    query_time = datetime(2026, 6, 15, tzinfo=timezone.utc)
    result = agent.mechanism_3_query(query_time)

    assert result.has_coverage is False
    assert result.covering_backup is None
    assert "not possible" in result.coverage_note.lower() or "gap" in result.coverage_note.lower()


def test_mechanism_3_query_reports_no_backups_when_catalog_empty() -> None:
    agent = _make_agent()
    agent._backup_cache = []

    result = agent.mechanism_3_query(_NOW)

    assert result.has_coverage is False
    assert "no backups" in result.coverage_note.lower()


# ---------------------------------------------------------------------------
# A19 cannot restore — negative test
# ---------------------------------------------------------------------------

def test_a19_cannot_restore() -> None:
    """A19 must not have a restore method — only trigger_backup is allowed."""
    agent = _make_agent()

    assert not hasattr(agent, "restore"), "A19 must not have a restore method"
    assert not hasattr(agent, "restore_backup"), "A19 must not have a restore_backup method"
    assert not hasattr(agent, "rollback_to"), "A19 must not have a rollback_to method"
    # trigger_backup IS allowed
    assert hasattr(agent, "trigger_backup"), "A19 must expose trigger_backup"


def test_run_audit_ingestion_cycle_polls_and_ingests() -> None:
    mock_sink = MagicMock()
    mock_sink.emit.return_value = "/tmp/fake"
    agent = _make_agent(mock_sink)

    with patch("pqbs.agents.integrity.a19_custody._run_ccloud") as mock_run:
        mock_run.return_value = [_RAW_AUDIT_EVENT]
        ingested = agent.run_audit_ingestion_cycle()

    assert ingested == 1
    mock_sink.emit.assert_called_once()
