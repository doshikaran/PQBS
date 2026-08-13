"""Unit tests for AuditSink — local and S3 modes.

No live DB or AWS credentials required.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from pqbs.contracts import AuditRecord, AuditEventType
from pqbs.contracts.exceptions import AuditSinkError
from pqbs.agents.integrity.audit_sink import AuditSink, emit_or_block

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_TENANT = uuid4()


def _make_record(**kwargs: object) -> AuditRecord:
    defaults: dict = dict(
        event_type=AuditEventType.BELIEF_QUARANTINED,
        agent_id="test-agent",
        tenant_id=_TENANT,
        belief_id=uuid4(),
        timestamp=_NOW,
        before={"status": "pending"},
        after={"status": "quarantined"},
        reason="Unit test record",
    )
    defaults.update(kwargs)
    return AuditRecord(**defaults)  # type: ignore[arg-type]


class TestLocalEmit:
    def test_local_emit_creates_file(self, tmp_path: Path) -> None:
        """emit() in local mode creates a file at the expected path."""
        sink = AuditSink(_local_dir=str(tmp_path))
        record = _make_record()

        path_written = sink.emit(record)

        assert Path(path_written).exists()

    def test_local_emit_path_format(self, tmp_path: Path) -> None:
        """Key format is {tenant_id}/{event_type}/{audit_id}.json."""
        sink = AuditSink(_local_dir=str(tmp_path))
        record = _make_record()

        path_written = sink.emit(record)
        written = Path(path_written)

        assert written.suffix == ".json"
        assert written.name == f"{record.audit_id}.json"
        assert written.parent.name == record.event_type.value
        assert written.parent.parent.name == str(record.tenant_id)

    def test_checksum_in_payload(self, tmp_path: Path) -> None:
        """Emitted JSON file contains a 'checksum' key."""
        sink = AuditSink(_local_dir=str(tmp_path))
        record = _make_record()

        path_written = sink.emit(record)
        with open(path_written) as f:
            payload = json.load(f)

        assert "checksum" in payload
        assert len(payload["checksum"]) == 64  # SHA-256 hex

    def test_local_emit_creates_nested_dirs(self, tmp_path: Path) -> None:
        """Parent directories are created automatically."""
        nested = tmp_path / "a" / "b" / "c"
        sink = AuditSink(_local_dir=str(nested))
        record = _make_record()

        path_written = sink.emit(record)

        assert Path(path_written).exists()

    def test_emit_raises_audit_sink_error_on_failure(self, tmp_path: Path) -> None:
        """AuditSinkError is raised when the directory is read-only."""
        read_only_dir = tmp_path / "ro"
        read_only_dir.mkdir()
        # Create the tenant subdir so mkdir doesn't fix it
        tenant_dir = read_only_dir / str(_TENANT)
        tenant_dir.mkdir()
        # Remove write permission from the tenant dir
        tenant_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

        sink = AuditSink(_local_dir=str(read_only_dir))
        record = _make_record()

        try:
            with pytest.raises(AuditSinkError):
                sink.emit(record)
        finally:
            # Restore perms so tmp_path cleanup works
            tenant_dir.chmod(stat.S_IRWXU)

    def test_multiple_records_different_files(self, tmp_path: Path) -> None:
        """Two records produce two separate files."""
        sink = AuditSink(_local_dir=str(tmp_path))
        r1 = _make_record()
        r2 = _make_record(event_type=AuditEventType.BELIEF_RELEASED)

        p1 = sink.emit(r1)
        p2 = sink.emit(r2)

        assert p1 != p2
        assert Path(p1).exists()
        assert Path(p2).exists()


class TestS3Mode:
    def test_s3_error_raises_audit_sink_error(self) -> None:
        """ClientError from boto3 is wrapped as AuditSinkError."""
        try:
            from botocore.exceptions import ClientError
        except ImportError:
            pytest.skip("botocore not installed")

        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "PutObject",
        )

        # Force S3 mode by setting env var
        with patch.dict(os.environ, {"PQBS_AUDIT_BUCKET": "test-bucket"}):
            sink = AuditSink(_s3_client=mock_s3)
            record = _make_record()

            with pytest.raises(AuditSinkError):
                sink.emit(record)

    def test_s3_put_object_called_with_correct_key(self) -> None:
        """S3 key follows {tenant_id}/{event_type}/{audit_id}.json format."""
        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(os.environ, {"PQBS_AUDIT_BUCKET": "my-worm-bucket"}):
            sink = AuditSink(_s3_client=mock_s3)
            record = _make_record()

            key = sink.emit(record)

        expected_key = (
            f"{record.tenant_id}/{record.event_type.value}/{record.audit_id}.json"
        )
        assert key == expected_key
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "my-worm-bucket"
        assert call_kwargs["Key"] == expected_key
        assert call_kwargs["ObjectLockMode"] == "COMPLIANCE"

    def test_s3_generic_exception_raises_audit_sink_error(self) -> None:
        """Non-ClientError exceptions from boto3 are also wrapped as AuditSinkError."""
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = RuntimeError("connection timed out")

        with patch.dict(os.environ, {"PQBS_AUDIT_BUCKET": "test-bucket"}):
            sink = AuditSink(_s3_client=mock_s3)
            record = _make_record()

            with pytest.raises(AuditSinkError):
                sink.emit(record)


class TestModeSelection:
    def test_no_bucket_env_uses_local_mode(self, tmp_path: Path) -> None:
        """When PQBS_AUDIT_BUCKET is absent, local mode is used."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PQBS_AUDIT_BUCKET", None)
            sink = AuditSink(_local_dir=str(tmp_path))
            assert sink._s3_mode is False

    def test_bucket_env_set_uses_s3_mode(self) -> None:
        """When PQBS_AUDIT_BUCKET is set and non-empty, S3 mode is used."""
        with patch.dict(os.environ, {"PQBS_AUDIT_BUCKET": "my-bucket"}):
            sink = AuditSink()
            assert sink._s3_mode is True

    def test_empty_bucket_env_uses_local_mode(self, tmp_path: Path) -> None:
        """Empty PQBS_AUDIT_BUCKET string falls back to local mode."""
        with patch.dict(os.environ, {"PQBS_AUDIT_BUCKET": ""}):
            sink = AuditSink(_local_dir=str(tmp_path))
            assert sink._s3_mode is False


class TestEmitOrBlock:
    def test_emit_or_block_returns_path(self, tmp_path: Path) -> None:
        """emit_or_block() returns the path/key on success."""
        sink = AuditSink(_local_dir=str(tmp_path))
        record = _make_record()

        result = emit_or_block(sink, record)

        assert result is not None
        assert len(result) > 0

    def test_emit_or_block_propagates_audit_sink_error(self, tmp_path: Path) -> None:
        """emit_or_block() does NOT swallow AuditSinkError — callers must handle it."""
        read_only_dir = tmp_path / "ro"
        read_only_dir.mkdir()
        tenant_dir = read_only_dir / str(_TENANT)
        tenant_dir.mkdir()
        tenant_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

        sink = AuditSink(_local_dir=str(read_only_dir))
        record = _make_record()

        try:
            with pytest.raises(AuditSinkError):
                emit_or_block(sink, record)
        finally:
            tenant_dir.chmod(stat.S_IRWXU)
