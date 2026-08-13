# Skill: CockroachDB

Use this skill when working with CockroachDB features: serializable isolation, MVCC time-travel, CDC/changefeeds, vector indexes, row-level TTL, database roles, the Managed MCP Server (read transport), the ccloud CLI (control-plane management), the Agent Skills Repo (A18 posture verification), and the wire-protocol compatibility with PostgreSQL drivers.

## Four Required Tools (Submission Requirement)

PQBS must integrate all four CockroachDB tools in load-bearing roles, each with a named removal test:

| Tool | Load-bearing role | Named removal test |
|---|---|---|
| **Distributed Vector Indexing** | Nearest-neighbor recall; structural tenant isolation | `test_removal_vector_index` |
| **Managed MCP Server** | A9/A10 read transport; protocol-layer write enforcement | `test_removal_mcp_server` |
| **ccloud CLI** | A19 control-plane audit ingestion; backup catalog; Mechanism 3 | `test_removal_ccloud` |
| **Agent Skills Repo** | A18 posture verification (security, schema-design, observability families) | `test_removal_agent_skills` |

---

## Wire Protocol Compatibility

CockroachDB speaks the PostgreSQL wire protocol. Standard `psycopg` (v3) or `psycopg2` drivers work without modification. **Critical consequence:** the driver will not retry serializable conflicts automatically. Retry logic is application responsibility (see `serializable-transactions` skill).

```python
# Requirements entry
# psycopg[binary]   # Postgres-wire driver; CockroachDB is wire-compatible

import psycopg
conn = psycopg.connect(os.environ['COCKROACH_URL'])
# conn is now a standard psycopg connection; all standard SQL works
```

---

## Serializable Isolation (Default)

CockroachDB defaults to serializable isolation — the strongest isolation level. Every transaction sees a consistent snapshot; concurrent conflicting transactions are serialized deterministically.

```sql
-- This is the default; you do not need to set it explicitly
BEGIN;
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
-- but if you want to be explicit in code:
```

```python
conn.execution_options(isolation_level='SERIALIZABLE')
```

Serialization failures surface as SQLSTATE `40001`. The application must catch and retry.

**[VERIFY]** Check current CockroachDB docs for the exact psycopg exception class for SQLSTATE 40001 in your driver version.

---

## MVCC Time-Travel (AS OF SYSTEM TIME)

Reconstructs the exact committed state at a past instant:

```sql
-- [VERIFY] Exact syntax against current CockroachDB docs
SELECT * FROM belief
AS OF SYSTEM TIME '-10m'              -- 10 minutes ago
WHERE tenant_id = $1;

-- Or with an absolute timestamp:
SELECT * FROM belief
AS OF SYSTEM TIME '2024-03-15 14:32:00+00'
WHERE tenant_id = $1;
```

**Bounded:** MVCC history is compacted after a garbage-collection retention window. On free/serverless tiers this may be short. The Phase 0 V3 spike measures the actual bound.

**Consequence for design:** MVCC is the short-horizon forensic tool. Bitemporal columns (`tx_from`, `tx_to`) are the durable unbounded record. Do not conflate them in the README.

When an as-of read fails (timestamp too old):
```
ERROR: AS OF SYSTEM TIME: timestamp before 1970-01-01T00:00:00Z: requested timestamp ... is below the earliest available timestamp
```

Catch this and surface a clear error to the caller with the measured window bound.

---

## Change Data Capture (Changefeeds)

Native CDC driven by the transaction log — guarantees no committed write escapes the feed.

```sql
-- [VERIFY] Current syntax against CockroachDB docs
-- Webhook sink (triggers a Lambda)
CREATE CHANGEFEED FOR TABLE belief
  INTO 'webhook-https://<lambda-url>'
  WITH
    updated,
    resolved = '10s',
    envelope = wrapped,
    format = json;

-- Object storage sink (S3)
CREATE CHANGEFEED FOR TABLE belief
  INTO 's3://<bucket>/<prefix>?AUTH=specified&AWS_ACCESS_KEY_ID=...&AWS_SECRET_ACCESS_KEY=...'
  WITH updated, resolved = '10s';
```

**Key properties:**
- Delivers every committed insert and update
- May deliver duplicates (at-least-once) — applications must be idempotent
- `resolved` timestamps provide progress markers for ensuring completeness
- `envelope=wrapped` includes `before` and `after` row state

**Cost warning:** changefeeds run continuously. Disable them when not actively testing to avoid exhausting the free-tier allowance.

---

## Vector Index

Prefix-partitioned vector index for nearest-neighbor search:

```sql
-- [VERIFY] Current syntax — vector index may be preview vs. GA; check vendor docs
CREATE INDEX belief_embedding_idx
  ON belief USING vec (embedding cosine_ops)   -- or euclidean_ops if cosine unsupported at index level
  PARTITION BY (tenant_id);
```

**V1 spike finding:** cosine may be unsupported at the index level. If so, normalize embeddings to unit length at write time and use Euclidean — mathematically equivalent for unit vectors. Record this in the README.

**Verify the planner actually uses the index:**
```sql
EXPLAIN SELECT * FROM belief
WHERE tenant_id = $1
ORDER BY embedding <-> $2
LIMIT 10;
-- Look for "index: belief_embedding_idx" in the plan
-- If you see "table scan", the index is not being used
```

**Dimension limits:** [VERIFY] check current CockroachDB docs. The V1 spike measures the actual limit for your embedding model's dimension count.

**Minimum rows before index creation:** the vector index may require a minimum corpus. Seed the demo corpus (2,000+ beliefs) before creating the index, or create after seeding.

---

## Row-Level TTL

Automatic expiry of rows older than a configurable age:

```sql
-- [VERIFY] Current TTL syntax
CREATE TABLE working_memory (
    tenant_id UUID,
    session_id UUID,
    entry_id UUID,
    agent_id TEXT,
    content TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, session_id, entry_id)
) WITH (ttl_expiration_expression = 'expires_at', ttl_job_cron = '*/5 * * * *');
```

The V6 spike verifies that TTL jobs fire within a demo-showable window. If they don't, use an explicit deletion job and disclose the change.

---

## Database Roles and Grants

```sql
-- Create roles
CREATE ROLE role_producer;
CREATE ROLE role_semantics;
CREATE ROLE role_integrity;
CREATE ROLE role_consumer;
CREATE ROLE role_auditor;

-- Grants (examples — see design §22 for full spec)
GRANT INSERT ON TABLE belief TO role_producer;      -- via pending-only view
GRANT INSERT ON TABLE provenance TO role_producer;
GRANT SELECT ON TABLE trusted_current_beliefs TO role_consumer;

-- Assign a role to a user
GRANT role_consumer TO '<username>';
```

**Role-scoped views (created in migration 0012_views):**
```sql
CREATE VIEW trusted_current_beliefs AS
  SELECT * FROM belief
  WHERE status = 'trusted' AND tx_to IS NULL;

GRANT SELECT ON trusted_current_beliefs TO role_consumer;
REVOKE SELECT ON TABLE belief FROM role_consumer;   -- direct table access revoked
```

**Connecting with a specific role:**
```python
# Different connection strings for different roles
CONSUMER_URL = os.environ['COCKROACH_URL'].replace('?', '?role=role_consumer&')
```

---

## Cluster Management (ccloud CLI)

```bash
# [VERIFY] Check current ccloud syntax
ccloud cluster create serverless pqbs-dev --region <REGION> --plan basic
ccloud cluster sql pqbs-dev --echo-sql
ccloud cluster usage pqbs-dev    # check daily consumption
```

## Managed MCP Server (Read Transport for A9/A10)

The CockroachDB Cloud Managed MCP Server provides a read-only protocol endpoint for consumer agents:

- **Endpoint:** `cockroachlabs.cloud/mcp`
- **Protocol guarantee:** no write verb is available at the MCP protocol layer regardless of database role grants. This is the second enforcement layer on TB4 (consumer trust boundary).
- **Use in PQBS:** A9 (Recall) and A10 (Audit) connect through MCP for all consumer-path reads.
- **V4 spike:** Phase 0 V4 verifies MCP is usable, documents read/write/audit semantics, and confirms the write-verb absence at the protocol layer.

```python
# [VERIFY] Exact MCP connection mechanism from V4 spike findings
# Connect A9/A10 through MCP endpoint (not direct CRDB URL)
MCP_ENDPOINT = os.environ['COCKROACH_MCP_ENDPOINT']  # cockroachlabs.cloud/mcp
```

**Fallback:** if V4 found MCP unusable, use direct connection but escalate to the Lead — Phase 6.5 becomes mandatory to compensate for the lost tool count.

## Agent Skills Repo (A18 Posture Verification)

The CockroachDB Agent Skills Repo provides skill families for agents to use CockroachDB capabilities safely:

- **Skill families used by A18:** security, schema-design, observability
- **Use in PQBS:** A18 uses these skills to query the schema catalog, compare role grants and constraints against `docs/posture-baseline.json`, and emit posture attestations or drift alerts.
- **Installation:** `[VERIFY] check current Agent Skills Repo installation method`

A18 runs the security skill family to verify role grants, the schema-design family to verify constraints and views, and the observability family to emit structured posture records to the WORM sink.

## ccloud CLI in A19 (Substrate Custody)

A19 uses the ccloud CLI with JSON output for all control-plane interactions:

```bash
# [VERIFY] Exact ccloud audit-log syntax
ccloud audit-log list --since <last_poll_ts> --output json

# [VERIFY] Exact backup catalog query syntax
ccloud cluster backups list pqbs-dev --output json
```

**Service account RBAC:** A19 runs with a scoped service account that has:
- Read authority on control-plane audit logs
- Authority to trigger backups
- NO restore authority (human-authorized only)

**JSON output discipline:** every ccloud command used by A19 must use `--output json`. Do not parse human-readable ccloud output — it is not stable.

---

## Performance Targets for PQBS

| Metric | Target | Notes |
|---|---|---|
| Write path p50 | < 400 ms | Excludes embedding (pre-transaction) |
| Write path p99 | < 1200 ms | Includes up to 2 retries |
| Screening lag p50 | < 5 s | Fail-closed window |
| Screening lag p99 | < 15 s | Under demo load |
| Recall latency p50 | < 600 ms | Includes query embedding |
| Retry rate (normal) | < 5% | Higher indicates hot-key problem |
| Retry rate (contention test) | > 30% | This is a floor, not a ceiling |

---

## Common Pitfalls

| Pitfall | Consequence | Prevention |
|---|---|---|
| Retry wrapper reusing stale reads | Serializable anomaly uncaught; incorrect resolution | Re-read all rows inside retry loop |
| Embedding model call inside transaction | Contention and timeouts | Embed before opening the transaction |
| Changefeed left running overnight | Free-tier exhaustion | Teardown script at end of every test session |
| WORM bucket used for dev data | Cannot delete | Separate non-locked dev bucket |
| Vector index not used by planner | Slow recall, no error | Always verify with EXPLAIN |
| Conflating MVCC with bitemporal | Wrong README claims | MVCC is bounded; bitemporal is unbounded |
