# Skill: WORM Audit Sink and Non-Repudiation

Use this skill when configuring S3 Object Lock, emitting audit records, implementing the audit-sink-unavailable write-blocking behavior, or verifying tamper-evidence.

---

## Why WORM (Write Once, Read Many)

An audit log that lives inside the system it audits can be altered by anyone who compromises that system. Write-once storage with retention locking means even full administrative compromise cannot rewrite history. This makes the system's audit claim meaningful rather than nominal.

**From design §17:** "Write-once storage with retention locking means even full administrative compromise cannot rewrite history."

Design v3.0 §17 adds a **second audit layer** (substrate-layer) complementing the existing belief-layer audit. Both layers write to the WORM bucket under distinct key prefixes.

---

## S3 Object Lock Configuration

```bash
# [VERIFY] Exact syntax against current AWS SDK/CLI docs

# Create bucket with Object Lock enabled (must be set at creation time)
aws s3api create-bucket \
  --bucket pqbs-audit-<suffix> \
  --region us-east-1 \
  --object-lock-enabled-for-bucket

# Set default retention (COMPLIANCE mode — even bucket owner cannot delete)
aws s3api put-object-lock-configuration \
  --bucket pqbs-audit-<suffix> \
  --object-lock-configuration '{
    "ObjectLockEnabled": "Enabled",
    "Rule": {
      "DefaultRetention": {
        "Mode": "COMPLIANCE",
        "Days": 365
      }
    }
  }'
```

**COMPLIANCE mode** means not even the bucket owner or AWS root account can delete objects within the retention period. This is the strongest protection and the one the design requires.

**GOVERNANCE mode** allows owners with `s3:BypassGovernanceRetention` to delete. Do NOT use governance mode for the production audit bucket — it weakens the non-repudiation claim.

---

## Two Buckets (Critical for Development)

```
pqbs-audit-prod    # COMPLIANCE mode, 365-day retention — production/demo audit records
pqbs-audit-dev     # No retention lock — development and test data
```

**Never write test data into the production WORM bucket.** Those records cannot be deleted, which creates permanent storage cost and pollutes the audit history. Use the dev bucket during development and testing.

## Two Audit Layers (Design §17 v3.0)

PQBS maintains two distinct audit layers in the WORM bucket, separated by key prefix:

| Layer | Key prefix | What it captures | Emitter |
|---|---|---|---|
| Belief-layer | `beliefs/<tenant_id>/<belief_id>/` | Every belief state transition | E1, E2, E3 agents |
| Substrate-layer | `substrate/<event_type>/<timestamp>/` | Control-plane events (admin actions, role changes, REVOKE, backup operations) | A19 via ccloud CLI |

The substrate-layer records defend against T12 (substrate tampering). An admin action visible in the CockroachDB Cloud control plane will appear in the substrate-layer audit within one A19 polling cycle.

### Substrate Audit Record Format

```python
class SubstrateAuditRecord(BaseModel):
    event_id: UUID
    event_type: str          # e.g., 'admin_action', 'role_change', 'backup_triggered'
    source: Literal['ccloud']
    raw_event: dict          # verbatim ccloud JSON output for the event
    ingested_at: datetime    # when A19 polled and ingested this event
    cluster_id: str
```

Write substrate records to the WORM bucket under the `substrate/` prefix using the same `put_object` pattern as belief-layer records. The same COMPLIANCE-mode bucket applies — substrate records are equally immutable.

---

## Audit Record Format

```python
class AuditRecord(BaseModel):
    event_id: UUID
    event_type: Literal['created', 'superseded', 'verdict', 'quarantined', 'released', 'rejected']
    agent_id: str              # cryptographic identity of the acting agent
    tenant_id: UUID
    belief_id: UUID
    timestamp: datetime
    before: Optional[dict]     # state before transition (None for 'created')
    after: dict                # state after transition
    reason: str                # human-readable reason for the transition
    signal_scores: Optional[dict]   # for 'verdict' events
```

---

## Six Required Belief-Layer Transition Types

Every belief lifecycle transition must emit a belief-layer audit record:

| Event type | Trigger | Emitter |
|---|---|---|
| `created` | New belief committed | E1 write path |
| `superseded` | Incumbent closed during resolution | E1 A7 resolution |
| `verdict` | Screening gate issues trust verdict | E2 A4 screening |
| `quarantined` | Belief isolated by gate | E2 A4 screening |
| `released` | Reviewer releases from quarantine | E3 A14 review |
| `rejected` | Reviewer rejects from quarantine | E3 A14 review |

## Additional Substrate-Layer Event Types (A18/A19)

| Event type | Trigger | Emitter |
|---|---|---|
| `posture_attestation` | A18 verifies live schema matches baseline — no drift | E3 A18 |
| `posture_drift` | A18 detects deviation from `docs/posture-baseline.json` | E3 A18 |
| `control_plane_event` | A19 ingests an event from ccloud audit log | E3 A19 |
| `backup_catalog_update` | A19 records a change in backup state | E3 A19 |

---

## Writing Audit Records

```python
import boto3
import json
from datetime import datetime, timezone

s3 = boto3.client('s3', region_name=os.environ['AWS_REGION'])
WORM_BUCKET = os.environ['WORM_BUCKET']

def emit_audit_record(record: AuditRecord) -> None:
    key = f"{record.tenant_id}/{record.belief_id}/{record.event_type}/{record.event_id}.json"
    body = record.model_dump_json(indent=2).encode('utf-8')

    try:
        s3.put_object(
            Bucket=WORM_BUCKET,
            Key=key,
            Body=body,
            ContentType='application/json',
            # Object Lock is enforced at the bucket level by default retention
        )
    except s3.exceptions.ClientError as e:
        raise AuditSinkUnavailable(f"WORM bucket write failed: {e}") from e
```

---

## Audit-Sink-Unavailable Behavior

When the WORM bucket is unavailable, belief writes must be blocked. This is a deliberate availability sacrifice — design §24's hardest row.

```python
# In the write path, after successful commit to the DB:
try:
    emit_audit_record(AuditRecord(event_type='created', ...))
except AuditSinkUnavailable as e:
    # Option 1: Roll back the DB commit (if still in transaction)
    # Option 2: Mark belief as needing-re-audit and block its status from advancing
    # Do NOT silently continue without an audit record
    raise WriteRejectedAuditUnavailable(
        "Write rejected: audit sink unavailable. "
        "An unauditable state transition violates the system's purpose."
    ) from e
```

**Test this explicitly:**
```python
def test_audit_sink_unavailable_blocks_write():
    # Mock S3 to raise ClientError
    with mock_s3_unavailable():
        with pytest.raises(WriteRejectedAuditUnavailable):
            write_belief(subject="test", predicate="test", object="test")
```

---

## Verification Test (Required for Phase 5 Exit Gate)

```python
def test_worm_delete_attempt_fails():
    # Write a legitimate audit record
    key = write_test_audit_record()

    # Attempt to delete it
    with pytest.raises(s3.exceptions.ClientError) as exc_info:
        s3.delete_object(Bucket=WORM_BUCKET, Key=key)

    error_code = exc_info.value.response['Error']['Code']
    assert error_code in ('AccessDenied', 'ObjectLockConfigurationNotAllowedError')
```

Show this test passing in the demo video. It takes four seconds and is the empirical proof of non-repudiation.

---

## Querying Audit Records (A10)

For attribution queries, A10 reads audit records from S3 and correlates them with the belief and retrieval logs:

```python
def get_audit_trail(belief_id: UUID, tenant_id: UUID) -> list[AuditRecord]:
    paginator = s3.get_paginator('list_objects_v2')
    prefix = f"{tenant_id}/{belief_id}/"

    records = []
    for page in paginator.paginate(Bucket=WORM_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            body = s3.get_object(Bucket=WORM_BUCKET, Key=obj['Key'])['Body'].read()
            records.append(AuditRecord.model_validate_json(body))

    return sorted(records, key=lambda r: r.timestamp)
```

---

## Attribution Requirements

Every audit record must carry:
- `agent_id` — the cryptographic identity of the agent that triggered the transition (not just a string the agent claims — a verified identity from `agent_identity`)
- `timestamp` — wall-clock UTC, set by the emitting service, not by the agent
- `before` / `after` — state before and after the transition
- `reason` — human-readable explanation

"Which agent wrote the poisoned belief, from what source, and what it influenced before quarantine" must be answerable from the audit trail joined against `retrieval_log`.
