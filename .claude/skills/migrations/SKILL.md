# Skill: Migrations (Alembic + CockroachDB)

Use this skill when authoring, applying, or rolling back database migrations for PQBS. CockroachDB is wire-compatible with PostgreSQL, so Alembic works with standard psycopg configurations.

---

## Setup

```python
# migrations/env.py — configure to read from environment
import os
from alembic import context
from sqlalchemy import engine_from_config

config = context.config
config.set_main_option('sqlalchemy.url', os.environ['COCKROACH_URL'])
```

```bash
alembic init migrations
alembic upgrade head        # apply all
alembic downgrade -1        # roll back one
alembic current             # show current revision
alembic history             # show full revision chain
```

---

## Migration Order (PQBS)

Migrations must be applied in dependency order. Every migration must apply from empty AND roll back cleanly.

| Revision | Contents | Depends on |
|---|---|---|
| `0001_enums` | All enum types | — |
| `0002_policy` | `predicate_policy` | 0001 |
| `0003_identity` | `agent_identity` | 0001 |
| `0004_provenance` | `provenance` | 0001 |
| `0005_belief` | `belief` + PK + FK to provenance | 0004 |
| `0006_vector_index` | Prefixed vector index on `(tenant_id, embedding)` | 0005, **V1 verified** |
| `0007_integrity` | `integrity_verdict`, `quarantine` | 0005 |
| `0008_contradiction` | `contradiction_event` | 0005 |
| `0009_retrieval_log` | `retrieval_log` | 0005 |
| `0010_working_memory` | `working_memory` + row-level TTL | 0001, **V6 verified** |
| `0011_roles` | Four database roles + grants | all tables |
| `0012_views` | Role-scoped views + view grants | 0011 |

**0006 and 0010 depend on Phase 0 verification findings.** If V1 finds the vector index unavailable or a different syntax is needed, adapt 0006 accordingly and document the deviation in `docs/VERIFICATIONS.md`. Same for 0010 if V6 finds TTL syntax differs.

---

## Enum Types (0001)

Define all enums before any table uses them:

```python
# Alembic migration body
from alembic import op
import sqlalchemy as sa

status_enum = sa.Enum(
    'pending', 'trusted', 'quarantined', 'inconclusive', 'superseded', 'rejected',
    name='belief_status'
)

source_type_enum = sa.Enum(
    'user_statement', 'document', 'tool_result', 'web_content', 'agent_inference', 'system_of_record',
    name='source_type'
)

trust_tier_enum = sa.Enum(
    'authoritative', 'corroborated', 'unverified', 'untrusted',
    name='trust_tier'
)

reason_code_enum = sa.Enum(
    'anomalous_embedding', 'untrusted_source', 'imperative_content', 'contradiction_burst',
    'identity_anomaly', 'derived_from_quarantined', 'temporal_implausible', 'manual',
    name='quarantine_reason_code'
)

resolution_enum = sa.Enum(
    'challenger_supersedes', 'incumbent_retained', 'both_retained', 'deferred',
    name='resolution_type'
)

resolution_basis_enum = sa.Enum(
    'recency', 'confidence', 'source_tier', 'explicit_invalidation', 'policy',
    name='resolution_basis'
)

cardinality_enum = sa.Enum(
    'single_valued', 'multi_valued', 'temporal_sequence',
    name='cardinality'
)

disposition_enum = sa.Enum(
    'held', 'released', 'rejected',
    name='disposition'
)

sensitivity_enum = sa.Enum(
    'normal', 'elevated',
    name='sensitivity'
)
```

---

## Lifecycle Constraints on `belief` (0005)

These constraints are the structural enforcement of design §8. Do not implement them as application logic.

```python
# In the belief table creation:
op.create_check_constraint(
    'belief_status_valid',
    'belief',
    "status IN ('pending', 'trusted', 'quarantined', 'inconclusive', 'superseded', 'rejected')"
)

op.create_check_constraint(
    'belief_superseded_consistency',
    'belief',
    "(status = 'superseded' AND superseded_by IS NOT NULL) OR (status != 'superseded' AND superseded_by IS NULL)"
)

op.create_check_constraint(
    'belief_trust_score_consistency',
    'belief',
    "(trust_score IS NULL AND screened_at IS NULL) OR (trust_score IS NOT NULL AND screened_at IS NOT NULL)"
)
```

**For role-based insert restriction:**
If CockroachDB CHECK constraints cannot reference the current session role `[VERIFY]`, enforce via the role-specific insert view in 0012:

```sql
-- In 0012_views
CREATE VIEW pending_belief_insert AS
  SELECT * FROM belief WHERE status = 'pending';
-- WITH CHECK OPTION ensures inserts via this view can only set status=pending

GRANT INSERT ON pending_belief_insert TO role_producer;
REVOKE INSERT ON TABLE belief FROM role_producer;
```

---

## Rolling Back Migrations

Every migration must have a working `downgrade()` that completely reverses the `upgrade()`.

```python
def upgrade():
    op.create_table('belief', ...)
    op.create_index(...)

def downgrade():
    op.drop_index(...)
    op.drop_table('belief')
    # Drop enum types created in this migration (not shared ones from 0001)
```

**Test rollback before every Phase 2 gate pass:**
```bash
alembic upgrade head
alembic downgrade base   # should return to empty schema cleanly
alembic upgrade head     # should reapply cleanly
```

---

## CockroachDB-Specific Considerations

**Primary keys:** use `UUID` primary keys for distribution. CockroachDB distributes data based on PK ranges; sequential integers create hotspots.

```python
op.create_table('belief',
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('belief_id', sa.UUID(), nullable=False),
    ...
    sa.PrimaryKeyConstraint('tenant_id', 'belief_id'),  # composite PK for tenant isolation
)
```

**Indexes:** prefixed indexes for tenant isolation are created in separate migrations (0006) to control timing relative to V1 verification findings.

**[VERIFY]** CockroachDB's ALTER TABLE for adding constraints to existing tables — syntax may differ from PostgreSQL in some versions.

---

## Migration Testing

Run in CI before any code that depends on the schema:

```bash
# Verify apply from empty
alembic downgrade base && alembic upgrade head

# Verify rollback
alembic downgrade -1     # roll back one revision
alembic upgrade head     # reapply

# Verify all negative tests still pass after any schema change
pytest tests/integration/test_schema_constraints.py
```

Schema changes require a new migration revision, not editing an existing one. Editing an existing migration that has been applied to any environment is destructive.
