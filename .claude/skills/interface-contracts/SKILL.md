# Skill: Interface Contracts and the Freeze Protocol

Use this skill when defining contracts in `src/pqbs/contracts/`, consuming a contract another owner produces, or proposing a change to a frozen interface.

---

## What Contracts Are

`src/pqbs/contracts/` is the single source of truth for every cross-owner boundary. It contains Pydantic models — not implementations. The goal is that five engineers can implement in parallel against a shared understanding of what data flows between them, not merely what code each writes.

**After the `interface-freeze-v1` git tag is pushed, no contract changes without the full freeze protocol.**

---

## The 14 Contracts

| Contract | Produced by | Consumed by | Critical fields |
|---|---|---|---|
| `CandidateBelief` | A1/A2/A3/A16 | A11 | subject, predicate, object, confidence, valid_from, valid_to, provenance_stub, author_agent_id |
| `NormalizedBelief` | A11 | A12 | CandidateBelief + object_normalized, sensitivity |
| `EmbeddedBelief` | A12 | A7 | NormalizedBelief + embedding (vector) |
| `ProvenanceRecord` | producers | A4 | source_type, source_uri, source_digest, episode_id, derived_from, trust_tier |
| `ResolutionOutcome` | A7 | telemetry | resolution, basis, incumbent_id, challenger_id, retry_count |
| `ChangeEvent` | CDC | A4 | belief_id, tenant_id, operation, before, after, commit_timestamp |
| `SignalScore` | S1–S8 | A4 | signal_id, score, evidence (dict), latency_ms |
| `Verdict` | A4 | A6, A14, audit | verdict, trust_score, signal_scores, triggering_rule, screener_version |
| `QuarantineRecord` | A4 | A6, A14 | belief_id, reason_code, quarantined_at |
| `CascadeRequest` | A6 | A4 | belief_ids, reason, depth |
| `RecallRequest` | caller | A9 | query, tenant_id, temporal_context, limit |
| `RecallResult` | A9 | caller | beliefs[], provenance[], trust_scores[], retrieval_id |
| `TemporalQuery` | caller | A10 | tenant_id, as_of, mechanism |
| `AuditRecord` | all | WORM sink | event_type, agent_id, timestamp, before, after, reason, tenant_id, belief_id |

---

## The ChangeEvent Contract (Most Critical)

This contract crosses the sync/async boundary between E1 (write path) and E2 (screening gate). If it's wrong, Phase 4 stalls entirely.

```python
from pydantic import BaseModel
from typing import Literal, Optional
from uuid import UUID
from datetime import datetime

class ChangeEvent(BaseModel):
    belief_id: UUID
    tenant_id: UUID
    operation: Literal['insert', 'update']
    before: Optional[dict]    # Full belief row before change; None for inserts
    after: dict               # Full belief row after change — NOT just primary keys
    commit_timestamp: datetime
```

**E1 must confirm with E2:** does E2 need the full `before`/`after` row, or just primary keys? The signal implementations (S1, S3, S4) need the full `object`, `provenance_id`, and `author_agent_id` fields. Agree on this before Phase 1 ends.

---

## Defining a Contract (Phase 1 Only)

All contracts defined once, as data shapes, not implementations:

```python
# src/pqbs/contracts/belief_lifecycle.py
from pydantic import BaseModel, Field, validator
from typing import Literal, Optional
from uuid import UUID, uuid4
from datetime import datetime

class CandidateBelief(BaseModel):
    belief_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    subject: str
    predicate: str
    object: str
    confidence: float = Field(ge=0.0, le=1.0)
    valid_from: datetime
    valid_to: Optional[datetime] = None
    provenance_stub: dict    # filled in by producer before commit
    author_agent_id: str

    class Config:
        frozen = True   # contracts are immutable after freeze
```

Use `frozen = True` on all contract models to prevent accidental mutation.

---

## Interface Freeze Protocol

After `interface-freeze-v1` is tagged, follow this protocol for every proposed change:

**Step 1 — Identify impact.** Which agents consume this contract? What will break if the field is added/removed/renamed?

**Step 2 — Notify Lead.** Propose the change with: what's changing, why, which consumers are affected.

**Step 3 — Agree with all consumers.** Both producer and all consumers must approve.

**Step 4 — Update contract model.** Bump version if needed (add `version: Literal['v2']` field for major changes).

**Step 5 — Update all consuming tests.** Every test that uses the old contract shape must be updated.

**Step 6 — Document in `CHANGELOG-interfaces.md`.** Format:
```markdown
## 2024-03-15 — ChangeEvent v1.1

**Change:** Added `sensitivity` field (Optional[str]) to `ChangeEvent.after`.
**Reason:** E2's S3 signal needs sensitivity level from E1's canonicalization.
**Producers:** E1 (CDC output wrapper)
**Consumers:** E2 (A4 screening gate)
**Approved by:** E1, E2, Lead
**Tests updated:** tests/integration/test_cdc_contract.py
```

**Step 7 — Implement.**

---

## Consuming a Contract

When you consume a contract from another owner:

```python
# Import from the shared contracts package
from pqbs.contracts import ChangeEvent

def process_event(raw_event: dict) -> None:
    event = ChangeEvent.model_validate(raw_event)   # validates on receipt
    # use event.belief_id, event.tenant_id, etc.
```

Validate on receipt, not on trust. A malformed event from a broken upstream fails fast rather than silently propagating bad data.

---

## Contract Versioning

For Phase 1, all contracts are v1.0. If a contract must change after freeze:
- Add new optional fields (backward-compatible additions are lower risk)
- Bump the `screener_version` string on `Verdict` and `integrity_verdict` to trigger re-screening
- Add a `contract_version` field for major shape changes
- Never remove fields from a frozen contract — mark as deprecated Optional first

---

## Testing Contract Compliance

Every cross-owner boundary must have an integration test that asserts:
1. The producer emits valid instances of its contract
2. The consumer accepts those instances without validation errors
3. A deliberately malformed instance is rejected at the boundary

```python
# tests/integration/test_contracts.py
def test_change_event_contract():
    # Producer side (E1): commit a belief and capture the CDC event
    event_raw = capture_cdc_event(write_belief(...))

    # Contract validation (shared)
    event = ChangeEvent.model_validate(event_raw)   # must not raise

    # Consumer side (E2): screening gate accepts it
    process_change_event(event)   # must not raise

def test_change_event_malformed_rejected():
    bad_event = {'belief_id': 'not-a-uuid', 'operation': 'unknown'}
    with pytest.raises(ValidationError):
        ChangeEvent.model_validate(bad_event)
```
