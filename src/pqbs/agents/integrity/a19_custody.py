"""A19 Substrate Custody Agent — Phase 6.5, E3.

Two functions:

(a) Control-plane audit ingestion.
    Polls the CockroachDB Cloud cluster's audit log via the ccloud CLI,
    converts each event to an AuditRecord, and forwards it to the same
    WORM sink used by belief-layer events. This is design §17's second
    audit layer — the one that catches administrative actions at the
    cluster level, not the SQL level.

(b) Backup catalog and Mechanism 3.
    Polls available backups via ccloud, tracks backup coverage, and
    answers "which backup covers timestamp T?" queries — enabling
    Mechanism 3 (backup-anchored temporal reconstruction) beyond the
    MVCC GC horizon.

Authority boundary:
    A19 may READ the audit log and TRIGGER backups. It CANNOT restore.
    Restoration is human-authorized. An agent that can restore can also
    roll back an inconvenient audit trail.

ccloud CLI integration:
    Shells out to `ccloud` with PQBS_CCLOUD_API_KEY for authentication.
    The service account must have:
      - cluster:read (read audit logs, list backups)
      - cluster:backup-create (optional; only if trigger_backup is used)
      - NO restore authority and NO SQL-layer access.

    Command syntax is marked [VERIFY] per build plan conventions —
    confirm against current ccloud version before production deployment.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

from pqbs.agents.integrity.audit_sink import AuditSink, emit_or_block
from pqbs.contracts import AuditRecord
from pqbs.contracts.enums import AuditEventType

logger = structlog.get_logger(__name__)

_DEFAULT_CLUSTER_ID = os.environ.get(
    "PQBS_CCLOUD_CLUSTER_ID",
    "71b13406-ccdb-481e-b0dc-f4aa75718234",
)
_CCLOUD_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlPlaneEvent:
    """Single event from the ccloud control-plane audit log."""
    event_id: str
    event_type: str
    actor: str
    resource: str
    timestamp: datetime
    details: dict[str, Any]


@dataclass(frozen=True)
class BackupEntry:
    """Single backup from the ccloud backup catalog."""
    backup_id: str
    cluster_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str         # "completed", "running", "failed"
    covers_from: datetime | None
    covers_to: datetime | None


@dataclass
class Mechanism3Result:
    """Result of a backup-anchored temporal query (Mechanism 3)."""
    as_of: datetime
    covering_backup: BackupEntry | None
    has_coverage: bool
    coverage_note: str


# ---------------------------------------------------------------------------
# ccloud CLI wrapper (subprocess)
# ---------------------------------------------------------------------------

def _run_ccloud(args: list[str], timeout: int = _CCLOUD_TIMEOUT_S) -> dict[str, Any] | list[Any]:
    """Execute a ccloud command and parse its JSON output.

    [VERIFY] Command surface: confirm against current ccloud version.
    Auth: PQBS_CCLOUD_API_KEY injected into environment.

    Raises:
        FileNotFoundError: if ccloud binary is not on PATH.
        subprocess.TimeoutExpired: if command exceeds timeout.
        ValueError: if output is not valid JSON.
        RuntimeError: if ccloud exits with non-zero status.
    """
    env = dict(os.environ)
    api_key = os.environ.get("PQBS_CCLOUD_API_KEY", "")
    if api_key:
        env["CCLOUD_API_KEY"] = api_key

    cmd = ["ccloud"] + args
    logger.debug("ccloud_command", cmd=" ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "ccloud CLI not found on PATH. Install with: brew install cockroachdb/tap/ccloud"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ccloud exited {result.returncode}: {result.stderr.strip()[:500]}"
        )

    if not result.stdout.strip():
        return []

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ccloud output is not valid JSON: {result.stdout[:200]}"
        ) from exc


# ---------------------------------------------------------------------------
# Audit log parsing
# ---------------------------------------------------------------------------

def _parse_audit_event(raw: dict[str, Any]) -> ControlPlaneEvent:
    """Convert a raw ccloud audit log entry to a ControlPlaneEvent.

    Field names are [VERIFY] against current ccloud audit-log schema.
    """
    ts_raw = raw.get("timestamp") or raw.get("created_at") or raw.get("occurred_at", "")
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        ts = datetime.now(tz=timezone.utc)

    return ControlPlaneEvent(
        event_id=str(raw.get("id") or raw.get("event_id") or uuid4()),
        event_type=str(raw.get("type") or raw.get("event_type") or "unknown"),
        actor=str(raw.get("actor") or raw.get("user") or raw.get("service_account") or "unknown"),
        resource=str(raw.get("resource") or raw.get("cluster_id") or ""),
        timestamp=ts,
        details={k: v for k, v in raw.items()
                 if k not in ("id", "event_id", "type", "event_type", "actor",
                               "user", "resource", "timestamp", "created_at")},
    )


def _parse_backup_entry(raw: dict[str, Any], cluster_id: str) -> BackupEntry:
    """Convert a raw ccloud backup catalog entry to a BackupEntry.

    [VERIFY] Field names against current ccloud backup list schema.
    """
    def _parse_ts(val: Any) -> datetime | None:
        if not val:
            return None
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    return BackupEntry(
        backup_id=str(raw.get("id") or raw.get("backup_id") or uuid4()),
        cluster_id=cluster_id,
        started_at=_parse_ts(raw.get("started_at") or raw.get("created_at")) or datetime.now(tz=timezone.utc),
        completed_at=_parse_ts(raw.get("completed_at") or raw.get("finished_at")),
        status=str(raw.get("status", "unknown")),
        covers_from=_parse_ts(raw.get("covers_from") or raw.get("start_time")),
        covers_to=_parse_ts(raw.get("covers_to") or raw.get("end_time") or raw.get("completed_at")),
    )


# ---------------------------------------------------------------------------
# A19 SubstrateCustodyAgent
# ---------------------------------------------------------------------------

class SubstrateCustodyAgent:
    """A19 — Substrate Custody Agent.

    Ingests control-plane audit events from the CockroachDB Cloud into the
    WORM audit sink, and tracks backup coverage for Mechanism 3 queries.
    """

    def __init__(
        self,
        audit_sink: AuditSink,
        cluster_id: str | None = None,
        agent_id: str = "a19_custody",
        tenant_id: UUID | None = None,
    ) -> None:
        self._audit_sink = audit_sink
        self._cluster_id = cluster_id or _DEFAULT_CLUSTER_ID
        self._agent_id = agent_id
        self._tenant_id = tenant_id or UUID(
            os.environ.get("TENANT_ID_DEMO", "00000000-0000-0000-0001-000000000001")
        )
        self._last_polled_at: datetime | None = None
        self._backup_cache: list[BackupEntry] = []

    # ------------------------------------------------------------------
    # (a) Control-plane audit ingestion
    # ------------------------------------------------------------------

    def poll_control_plane_audit(
        self,
        since: datetime | None = None,
    ) -> list[ControlPlaneEvent]:
        """Fetch recent control-plane audit events from ccloud.

        [VERIFY] ccloud cluster audit-log list --cluster <id> --output json
        Exact subcommand path confirmed against ccloud v0.9+ docs.

        Args:
            since: Only return events after this timestamp. If None, uses
                   last_polled_at or falls back to last 24 hours.

        Returns:
            List of parsed ControlPlaneEvent objects.
        """
        since_ts = since or self._last_polled_at
        args = [
            "cluster", "audit-log", "list",
            "--cluster", self._cluster_id,
            "--output", "json",
        ]
        if since_ts is not None:
            args += ["--since", since_ts.strftime("%Y-%m-%dT%H:%M:%SZ")]

        try:
            raw = _run_ccloud(args)
        except FileNotFoundError:
            logger.warning("a19_ccloud_not_found_returning_empty")
            return []
        except RuntimeError as exc:
            logger.error("a19_ccloud_audit_failed", error=str(exc))
            return []

        events_raw = raw if isinstance(raw, list) else raw.get("items", [])  # type: ignore[union-attr]
        events = [_parse_audit_event(e) for e in events_raw]
        self._last_polled_at = datetime.now(tz=timezone.utc)

        logger.info("a19_audit_polled", event_count=len(events))
        return events

    def ingest_events_to_worm(
        self,
        events: list[ControlPlaneEvent],
    ) -> int:
        """Convert control-plane events to AuditRecords and emit to WORM sink.

        Returns:
            Count of events successfully ingested.
        """
        if not events:
            return 0

        ingested = 0
        for event in events:
            record = AuditRecord(
                event_type=AuditEventType.CONTROL_PLANE_AUDIT_INGESTED,
                agent_id=self._agent_id,
                tenant_id=self._tenant_id,
                timestamp=event.timestamp,
                before=None,
                after={
                    "ccloud_event_id": event.event_id,
                    "ccloud_event_type": event.event_type,
                    "actor": event.actor,
                    "resource": event.resource,
                    "details": json.dumps(event.details),
                },
                reason=(
                    f"Control-plane event ingested from CockroachDB Cloud: "
                    f"{event.event_type} by {event.actor}"
                ),
            )
            try:
                emit_or_block(self._audit_sink, record)
                ingested += 1
            except Exception as exc:
                logger.error(
                    "a19_worm_emit_failed",
                    event_id=event.event_id,
                    error=str(exc),
                )

        logger.info("a19_events_ingested", ingested=ingested, total=len(events))
        return ingested

    def run_audit_ingestion_cycle(self, since: datetime | None = None) -> int:
        """Poll control-plane audit and forward all events to WORM. One-shot."""
        events = self.poll_control_plane_audit(since=since)
        return self.ingest_events_to_worm(events)

    # ------------------------------------------------------------------
    # (b) Backup catalog and Mechanism 3
    # ------------------------------------------------------------------

    def list_backups(self, *, force_refresh: bool = False) -> list[BackupEntry]:
        """Fetch the backup catalog from ccloud.

        [VERIFY] ccloud cluster backup list --cluster <id> --output json

        Caches results in memory to avoid repeated API calls within a cycle.
        Pass force_refresh=True to bypass cache.
        """
        if self._backup_cache and not force_refresh:
            return self._backup_cache

        args = [
            "cluster", "backup", "list",
            "--cluster", self._cluster_id,
            "--output", "json",
        ]

        try:
            raw = _run_ccloud(args)
        except FileNotFoundError:
            logger.warning("a19_ccloud_not_found_returning_empty")
            return []
        except RuntimeError as exc:
            logger.error("a19_ccloud_backup_list_failed", error=str(exc))
            return []

        backups_raw = raw if isinstance(raw, list) else raw.get("items", [])  # type: ignore[union-attr]
        backups = [_parse_backup_entry(b, self._cluster_id) for b in backups_raw]
        self._backup_cache = backups

        logger.info("a19_backups_listed", count=len(backups))
        return backups

    def mechanism_3_query(
        self,
        as_of: datetime,
        *,
        force_refresh: bool = False,
    ) -> Mechanism3Result:
        """Find which completed backup covers the requested timestamp (Mechanism 3).

        Returns a Mechanism3Result describing coverage. If no backup covers
        the timestamp, has_coverage=False and coverage_note explains the gap —
        this is the explicit reporting of coverage gaps required by Phase 6.5 DoD.

        A19 reports the backup; it does NOT restore. Restoration is human-authorized.
        """
        backups = self.list_backups(force_refresh=force_refresh)

        completed = [
            b for b in backups
            if b.status == "completed"
            and b.covers_from is not None
            and b.covers_to is not None
            and b.covers_from <= as_of <= b.covers_to
        ]

        if completed:
            best = max(completed, key=lambda b: b.completed_at or b.started_at)
            return Mechanism3Result(
                as_of=as_of,
                covering_backup=best,
                has_coverage=True,
                coverage_note=(
                    f"Backup {best.backup_id} covers {best.covers_from} → {best.covers_to}. "
                    "Contact a DBA to initiate restore — A19 cannot restore autonomously."
                ),
            )

        # No covering backup — report the gap explicitly
        if not backups:
            note = "No backups found in catalog. Mechanism 3 is unavailable."
        else:
            earliest = min(
                (b.covers_from for b in backups if b.covers_from),
                default=None,
            )
            latest = max(
                (b.covers_to for b in backups if b.covers_to),
                default=None,
            )
            note = (
                f"No backup covers {as_of.isoformat()}. "
                f"Catalog spans {earliest} → {latest}. "
                "Mechanism 3 reconstruction is not possible for this timestamp."
            )

        logger.warning("a19_mechanism3_coverage_gap", as_of=as_of.isoformat(), note=note)
        return Mechanism3Result(
            as_of=as_of,
            covering_backup=None,
            has_coverage=False,
            coverage_note=note,
        )

    def trigger_backup(self) -> bool:
        """Request a new backup via ccloud.

        [VERIFY] ccloud cluster backup create --cluster <id>
        Authority: A19 may trigger, NOT restore.

        Returns True if the backup was successfully requested.
        """
        args = [
            "cluster", "backup", "create",
            "--cluster", self._cluster_id,
        ]
        try:
            _run_ccloud(args)
            logger.info("a19_backup_triggered", cluster_id=self._cluster_id)
            return True
        except (FileNotFoundError, RuntimeError) as exc:
            logger.error("a19_backup_trigger_failed", error=str(exc))
            return False
