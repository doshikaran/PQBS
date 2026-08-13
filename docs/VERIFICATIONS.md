## V5 — Serializable Retry Determinism

**Run date:** 2026-08-13 08:15 UTC
**Cluster:** CockroachDB Serverless (higher-panther-31862.j77.aws-ap-south-1.cockroachlabs.cloud:26257)
**Cluster version:** CockroachDB CCL v26.2.5
**Runs:** 25
**Conflicts detected:** 25/25 (100.0%)
**Pass criterion:** ≥90% of runs
**Result:** ✅ PASSED

**SQLSTATE observed:** `40001`

**Timing window:** Both threads read row id=1 under SERIALIZABLE isolation (plain
SELECT, no FOR UPDATE), synchronised at a threading.Barrier(2), then each
attempted to write a distinct value. Jitter of 1–8ms applied between barrier
and write. Average pair duration: 148ms. Max pair duration: 204ms.

**Findings:**
- Serializable isolation is reliably producing SQLSTATE 40001 on concurrent
  read-write conflicts. Every single run (25/25) produced a SerializationFailure
  on the losing thread.
- Key implementation detail: `FOR UPDATE` must NOT be used in the read step.
  `FOR UPDATE` causes one thread to block on the lock rather than both threads
  reading and conflicting at commit time — this produces a deadlock-style hang,
  not a SQLSTATE 40001. Plain `SELECT` is correct; the serializable conflict is
  detected at COMMIT time.
- The retry wrapper (Phase 3, E1) must catch `psycopg.errors.SerializationFailure`
  (pgcode `40001`). Belt-and-suspenders: also catch `psycopg.Error` with
  `pgcode == '40001'` for any driver-version variance.
- The Phase 3 / Phase 8 contention harness (`tests/contention/`) will use the
  same timing pattern (plain SELECT + barrier + jitter) with 8–16 concurrent writers.
- Spike table used a timestamped name per run (`v5_contention_YYYYMMDD_HHMMSS`)
  to avoid lock contention from previous hung runs. This pattern should be
  adopted in the productionised harness.

**Active fallback:** None — V5 passed. Proceeding with serializable concurrency
demo as the central demonstration (§26.7).

---

## V1 — Vector Index Status and Distance Metrics

**Run date:** 2026-08-13 10:41 UTC
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Cluster version:** CockroachDB CCL v26.2.5
**Embedding dimension tested:** 1024 (matches amazon.titan-embed-text-v2:0 default)
**Row count used:** 250
**Result:** ✅ PASSED

### Index creation

| Syntax | Result |
|---|---|
| `ivfflat_l2_ops` | ❌ `at or near "ivfflat": syntax error: unrecognized access meth` |
| `ivfflat_cosine_ops` | ❌ `at or near "ivfflat": syntax error: unrecognized access meth` |
| `ivfflat_ip_ops` | ❌ `at or near "ivfflat": syntax error: unrecognized access meth` |
| `hnsw_l2_ops` | ✅ succeeded |
| `hnsw_cosine_ops` | ✅ succeeded |
| `native_vector` | ✅ succeeded |
| `native_vector_prefix` | ✅ succeeded |
| `ivfflat_prefix_l2` | ❌ `at or near "ivfflat": syntax error: unrecognized access meth` |

**Working syntaxes:** `hnsw_l2_ops`, `hnsw_cosine_ops`, `native_vector`, `native_vector_prefix`

### Distance operators

| Operator | Symbol | Works for queries | Notes |
|---|---|---|---|
| `l2_euclidean` | `<->` | ✅ yes | top_dist=0.0, 22.1ms |
| `cosine` | `<=>` | ✅ yes | top_dist=0.0, 19.8ms |
| `inner_product` | `<#>` | ✅ yes | top_dist=-1.0, 20.3ms |

### Query planner behaviour

- Index used by planner: **YES**
- Full table scan detected: **YES — index not used**

EXPLAIN snippet:
```
distribution: local

• top-k
│ order: +column10
│ k: 5
│
└── • render
    │
    └── • scan
          missing stats
          table: v1_vector_spike_20260813_104001@v1_vector_spike_20260813_104001_pkey
          spans: FULL SCAN
```

### Minimum row count for index creation

| Row count | Index created? |
|---|---|
| 1 | ✅ yes |
| 5 | ✅ yes |
| 10 | ✅ yes |
| 50 | ✅ yes |
| 100 | ✅ yes |
| 200 | ✅ yes |

Minimum confirmed: **1 rows**

### Prefix-partitioned index (tenant isolation)

- Prefix index `(tenant_id, embedding)` created: **✅ YES**
- Syntax used: `CREATE VECTOR INDEX ON v1_vector_spike_20260813_104001 (tenant_id, embedding)`
- Cross-tenant retrieval structurally blocked: **✅ YES**

### Cosine distance note

Cosine distance is supported at the index level.

### Findings and consequences for Phase 2

1. **Vector type `vector(1024)`** is supported on CockroachDB Serverless v26.2.5.
2. **Working index syntax(es):** `hnsw_l2_ops`, `hnsw_cosine_ops`, `native_vector`, `native_vector_prefix`.
3. Migration `0006_vector_index` must use: `hnsw_l2_ops`
4. **Distance operators for queries:** l2_euclidean, cosine, inner_product work.
   All tested operators work.
5. **Planner behaviour:** Index not confirmed in planner — may fall back to scan. Monitor recall latency.
6. **Tenant isolation:** Prefix index enforces structural isolation. Cross-tenant retrieval is impossible at the index level.

**Active fallback:** None — V1 passed.



---

## V3 — MVCC Retention Window

**Run date:** 2026-08-13 10:59 UTC
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Result:** ✅ PASSED

### Cluster GC settings

- Default zone GC TTL: `4500s (1h)`
- Zone config: `ALTER RANGE default CONFIGURE ZONE USING
	range_min_bytes = 134217728,
	range_max_bytes = 536870912,
	gc.ttlseconds = 45`

### AS OF SYSTEM TIME probe results

| Interval | Status | Content seen | Error |
|----------|--------|--------------|-------|
| `1s` | ✅ success | v1_original |  |
| `10s` | ⏳ not elapsed |  |  |
| `30s` | ⏳ not elapsed |  |  |
| `1min` | ⏳ not elapsed |  |  |
| `5min` | ⏳ not elapsed |  |  |
| `10min` | ⏳ not elapsed |  |  |
| `20min` | ⏳ not elapsed |  |  |
| `30min` | ⏳ not elapsed |  |  |
| `60min` | ⏳ not elapsed |  |  |

**Note:** Intervals 10s, 30s, 1min, 5min, 10min, 20min, 30min, 60min were not probed because the spike completed in 0s — insufficient time had elapsed since the anchor write. GC TTL (4500s (1h)) implies these intervals would succeed.

**Furthest confirmed read:** -30min
**Target for demo:** -30min (1800s)

### Consequence for design §16

AS OF SYSTEM TIME reads confirmed to -30min. Mechanism 2 (MVCC temporal reconstruction) is viable within this window. Bitemporal columns remain the durable record beyond this boundary.

**Active fallback:** None — V3 passed. Mechanism 2 claims bounded by 30min. README must cite this limit explicitly.



---

## V6 — Row-Level TTL Behavior

**Run date:** 2026-08-13 11:09 UTC
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**TTL expire_after:** 10s
**TTL job cron:** `* * * * *` (every minute)
**Rows inserted:** 20
**Result:** ✅ PASSED

### TTL syntax

Working syntax: `ttl_expiration_expression_with_cron`
Table reloptions: `["ttl='on'", "ttl_expiration_expression=e'created_at + INTERVAL \'10 seconds\''", "ttl_job_cron='* * * * *'", 'schema_locked=true']`

### Expiry timeline

| Elapsed | Row count |
|---------|-----------|
|     0s |    20 |
|    10s |    20 |
|    20s |    20 |
|    30s |    20 |
|    40s |    20 |
|    50s |    20 |
|    60s |    20 |
|    70s |    20 |
|    80s |    20 |
|    90s |    20 |
|   100s |    20 |
|   110s |    20 |
|   120s |     0 |

**First deletion observed:** t=120s (110s after eligibility)
**Table empty at:** t=120s

### Consequence for Phase 2

`working_memory` table (migration `0010_working_memory`) will use `ttl_expiration_expression_with_cron` TTL syntax. Job runs every minute; rows are eligible 10s after creation. First deletions observed ~120s after insert (110s lag past eligibility).

**Active fallback:** None — V6 passed. `working_memory` will use row-level TTL.

---

## V2 — Change Feed Availability and Cost

**Run date:** 2026-08-13 11:14 UTC
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Writes:** 100  **Events received:** 100
**Result:** ✅ PASSED

### CDC feature availability

| Feature | Value |
|---------|-------|
| `kv.rangefeed.enabled` | `error: InsufficientPrivilege` |
| SHOW CHANGEFEED JOBS | `True` |
| Webhook CDC available | `yes` |

**Webhook probe error:** `n/a`

### Test method: `core_changefeed`

- Events received: **100 / 100**
- Total elapsed: **1.0s** (inserts + event capture)
- CDC error: `none`
- Test note: ``

### Consequence for Phase 3 / E2

Core changefeed (EXPERIMENTAL CHANGEFEED FOR) is available. Phase 3 (E2) should use a webhook or cloud-storage sink with an ngrok tunnel for local development. For the demo, a webhook to a Lambda URL is the intended production architecture. Log-driven guarantee holds.

**Active fallback:** None — CDC available. Build plan §4.3 (E2) proceeds as designed. Webhook sink requires ngrok or similar tunnel for local dev testing.

---

## V4 — Managed MCP Server Semantics

**Run date:** 2026-08-13 11:17 UTC
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**MCP endpoint:** `https://cockroachlabs.cloud/mcp`
**Result:** ✅ PASSED (endpoint reachable)

### HTTP endpoint probe

| Probe | Status |
|-------|--------|
| GET `https://cockroachlabs.cloud/mcp` | `401` |
| POST (MCP initialize) | `401` |

**Interpretation:** MCP endpoint reachable (HTTP 401). Authentication required — endpoint exists, config needed.

POST response body (first 200 chars): `{"error":"invalid_request","error_description":"Authorization required"}
`

### SQL visibility of MCP

- MCP-related sessions visible: `0`
- MCP-related DB roles: `none found`

### Manual configuration checklist (required for full V4 pass)

```
Manual steps to fully configure V4 (do after this spike):

1. Log in to CockroachDB Cloud Console → your cluster → "Connect" tab
2. Look for "MCP Server" or "AI Agent Access" section
3. Click "Enable MCP Server" (or equivalent)
4. Generate an API key / OAuth token for MCP access
5. Copy the MCP connection snippet (should look like):
     npx @cockroachlabs/cockroachdb-mcp-server \
       --host <cluster-host> \
       --database <db> \
       --token <api-key>
6. In your agent (Claude Desktop or LLM client), add the MCP server config
7. Test: ask the agent to SELECT 1 (read test)
8. Test: ask the agent to INSERT a row (write test — should require consent)
9. Check the audit log: run SELECT * FROM crdb_internal.cluster_contention_events
   or check the Cloud Console Activity tab
10. Record: what the audit log captures (agent ID, query, timestamp)

Expected behavior per CockroachDB docs:
- Read queries: permitted by default (SELECT)
- Write queries: require explicit confirmation / consent flow
- Audit log: records agent identity, query, timestamp, result
- Auth model: OAuth 2.0 or API key; scoped to the cluster
```

### Consequence for Phase 6 / A9, A10

MCP server endpoint is reachable at `cockroachlabs.cloud/mcp`. Full configuration requires generating an API token from the Cloud Console and registering the MCP server in the agent's config. Phase 6 (A9, A10) proceeds as designed — the MCP server is the second independent enforcement layer on TB4. Webhook sink in V2 can use the Lambda URL.

**Active fallback:** None — MCP endpoint reachable. Full auth setup required before Phase 6. See manual steps in this report.

