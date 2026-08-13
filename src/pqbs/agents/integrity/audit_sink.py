"""WORM audit sink — Phase 5, E3.

Two modes:
- S3 mode (prod): writes to PQBS_AUDIT_BUCKET via boto3 with ObjectLock COMPLIANCE.
- Local mode (dev/test): writes to PQBS_AUDIT_LOCAL_DIR (defaults to /tmp/pqbs_audit).

Mode selection: PQBS_AUDIT_BUCKET set and non-empty → S3; else → local.

Fail-closed: emit() raises AuditSinkError on any failure.
Callers must abort their write if AuditSinkError is raised.

Security Invariant 6: Audit records cannot be deleted or overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from pqbs.contracts import AuditRecord
from pqbs.contracts.exceptions import AuditSinkError

try:
    import boto3
    from botocore.exceptions import ClientError as BotocoreClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    BotocoreClientError = Exception  # type: ignore[assignment, misc]
    _BOTO3_AVAILABLE = False

logger = structlog.get_logger(__name__)

_DEFAULT_LOCAL_DIR = "/tmp/pqbs_audit"
_WORM_RETENTION_DAYS = 365


class AuditSink:
    """WORM audit sink supporting S3 (prod) and local (dev/test) modes.

    Constructor accepts optional overrides for test injection:
      _s3_client: pre-built boto3 S3 client (bypasses env-based construction)
      _local_dir: override for PQBS_AUDIT_LOCAL_DIR (used in local mode)
    """

    def __init__(
        self,
        *,
        _s3_client: Any = None,
        _local_dir: str | None = None,
    ) -> None:
        self._s3_client_override = _s3_client
        self._local_dir_override = _local_dir

        bucket = os.environ.get("PQBS_AUDIT_BUCKET", "").strip()
        self._s3_mode: bool = bool(bucket)
        self._bucket: str = bucket

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def emit(self, record: AuditRecord) -> str:
        """Write record to sink. Returns the key/path written.

        Raises AuditSinkError on any failure.
        Callers MUST abort their write if this raises.
        """
        try:
            payload_dict = record.model_dump(mode="json")
            # Compute checksum of the payload body (before adding checksum key)
            raw_bytes = json.dumps(payload_dict, indent=2, sort_keys=True).encode()
            checksum = hashlib.sha256(raw_bytes).hexdigest()
            payload_dict["checksum"] = checksum
            payload = json.dumps(payload_dict, indent=2).encode()
        except Exception as exc:
            raise AuditSinkError(f"Failed to serialize audit record: {exc}") from exc

        if self._s3_mode:
            return self._emit_s3(record, payload)
        return self._emit_local(record, payload)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_s3(self, record: AuditRecord, payload: bytes) -> str:
        """Write to S3 with ObjectLock COMPLIANCE mode (WORM).

        Key: {tenant_id}/{event_type}/{audit_id}.json
        """
        key = f"{record.tenant_id}/{record.event_type.value}/{record.audit_id}.json"
        retain_until = datetime.now(tz=timezone.utc) + timedelta(days=_WORM_RETENTION_DAYS)

        try:
            if self._s3_client_override is not None:
                client: Any = self._s3_client_override
            elif boto3 is None or not _BOTO3_AVAILABLE:
                raise AuditSinkError("boto3 is not installed; cannot write to S3")
            else:
                client = boto3.client("s3")
            client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType="application/json",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
            )
            logger.info(
                "audit_record_emitted_s3",
                audit_id=str(record.audit_id),
                event_type=record.event_type.value,
                key=key,
            )
            return key
        except BotocoreClientError as exc:
            raise AuditSinkError(
                f"S3 PutObject failed for audit record {record.audit_id}: {exc}"
            ) from exc
        except Exception as exc:
            raise AuditSinkError(
                f"Unexpected error writing audit record {record.audit_id} to S3: {exc}"
            ) from exc

    def _emit_local(self, record: AuditRecord, payload: bytes) -> str:
        """Write to local filesystem.

        Path: {local_dir}/{tenant_id}/{event_type}/{audit_id}.json
        Directories are created on demand.
        """
        base_dir = self._local_dir_override or os.environ.get(
            "PQBS_AUDIT_LOCAL_DIR", _DEFAULT_LOCAL_DIR
        )
        path = (
            Path(base_dir)
            / str(record.tenant_id)
            / record.event_type.value
            / f"{record.audit_id}.json"
        )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            logger.info(
                "audit_record_emitted_local",
                audit_id=str(record.audit_id),
                event_type=record.event_type.value,
                path=str(path),
            )
            return str(path)
        except OSError as exc:
            raise AuditSinkError(
                f"Failed to write audit record {record.audit_id} to {path}: {exc}"
            ) from exc
        except Exception as exc:
            raise AuditSinkError(
                f"Unexpected error writing audit record {record.audit_id} to local sink: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Module-level helper — callers use this so the fail-closed contract is
# expressed at the call site rather than buried in the class.
# ---------------------------------------------------------------------------

def emit_or_block(sink: AuditSink, record: AuditRecord) -> str:
    """Emit audit record. If this raises AuditSinkError, the caller MUST abort their write.

    The name is intentional: if the sink is unavailable, the belief write is blocked.
    This is Security Invariant 6 enforcement at the call-site level.
    """
    return sink.emit(record)
