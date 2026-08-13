# Audit Sink Design — Phase 5

**Decision date:** 2026-08-13
**Owner:** E3 — Containment

## Mode selection

`AuditSink` operates in one of two modes, determined at construction time:

- **S3 mode (prod):** `PQBS_AUDIT_BUCKET` env var is set and non-empty.
- **Local mode (dev/test):** `PQBS_AUDIT_BUCKET` absent or empty. Writes to `PQBS_AUDIT_LOCAL_DIR`, defaulting to `/tmp/pqbs_audit`.

## S3 WORM: ObjectLock COMPLIANCE mode

In S3 mode, every record is written with:
```
ObjectLockMode=COMPLIANCE, ObjectLockRetainUntilDate=now+365d
```
COMPLIANCE mode means even the bucket owner cannot delete or overwrite records within the retention window. This satisfies Security Invariant 6: audit records cannot be deleted or overwritten.

Key format: `{tenant_id}/{event_type}/{audit_id}.json`

## Checksum

Before writing, the sink serializes the record body and computes `SHA-256(payload)`. The hex digest is injected as a top-level `"checksum"` key in the JSON before it is written. Since `AuditRecord` is frozen (immutable Pydantic model), the checksum cannot be computed inside the model; it is computed by the sink and added to the JSON envelope only.

## Fail-closed

`AuditSink.emit()` raises `AuditSinkError` on any failure. The module-level helper `emit_or_block()` surfaces this contract at the call site. Callers MUST abort their write (e.g., rollback the DB transaction) if `AuditSinkError` is raised. A belief write that cannot be audited is never committed.

## Dev bucket note

Do NOT write test records to a retention-locked production bucket — they cannot be deleted. Use a separate non-locked dev bucket or the local mode (no bucket env var set). The local mode writes to `/tmp/pqbs_audit` or `PQBS_AUDIT_LOCAL_DIR` with no retention lock.
