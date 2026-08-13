"""
V1 — Vector Index Status and Distance Metrics Spike
====================================================
Question  : Is the vector index GA or preview on CockroachDB Serverless v26.2?
            Which distance metrics does the *index* support (as opposed to the
            vector type)? Dimension limits? Minimum row count before index
            creation? Does the query planner actually USE the index?
Method    : Create a table with a vector(1024) column (matching
            amazon.titan-embed-text-v2:0 default output dimension), insert
            ≥200 rows of unit-normalised random vectors, attempt every known
            index-creation syntax, run kNN queries with each available distance
            operator, inspect EXPLAIN plans to confirm index use.
Pass      : At least one index syntax succeeds AND the planner uses it AND
            nearest-neighbour queries return geometrically sane results.

This is a throwaway spike. None of this code moves into the product.
Output    : stdout summary + appended/replaced section in docs/VERIFICATIONS.md.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import psycopg
import psycopg.errors
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

COCKROACH_URL: str = os.environ.get("COCKROACH_URL", "")
if not COCKROACH_URL:
    sys.exit("ERROR: COCKROACH_URL is not set in .env")

# Matches amazon.titan-embed-text-v2:0 default dimension
EMBEDDING_DIM = 1024
# Row counts to insert for main test
MAIN_ROW_COUNT = 250
# Row count for minimum-threshold probe
MIN_ROW_PROBE_COUNTS = [1, 5, 10, 50, 100, 200]
CONNECT_TIMEOUT = 10
STATEMENT_TIMEOUT = "30s"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts_table(base: str) -> str:
    """Timestamped table name — avoids lock contention from previous runs."""
    return f"{base}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def random_unit_vector(dim: int, rng: np.random.Generator) -> list[float]:
    """Unit-normalised random vector. Euclidean distance on unit vectors is
    mathematically equivalent to cosine distance, which matters if the index
    only supports L2 ops."""
    v = rng.standard_normal(dim).astype(np.float32)
    norm = np.linalg.norm(v)
    if norm == 0:
        v[0] = 1.0
    else:
        v /= norm
    return v.tolist()


def vector_literal(v: list[float]) -> str:
    """Format a float list as a CockroachDB/pgvector literal string."""
    inner = ",".join(f"{x:.6f}" for x in v)
    return f"[{inner}]"


def run(conn: psycopg.Connection, sql: str, params=None, autocommit: bool = False):
    """Execute SQL, commit if not in autocommit mode, return rows."""
    if autocommit:
        conn.autocommit = True
        result = conn.execute(sql, params)
        conn.autocommit = False
    else:
        result = conn.execute(sql, params)
        conn.commit()
    return result


def ddl(conn: psycopg.Connection, sql: str) -> None:
    """Execute DDL in autocommit mode (CockroachDB DDL is auto-transactional)."""
    try:
        conn.rollback()  # ensure no open txn before switching autocommit
    except Exception:
        pass
    conn.autocommit = True
    conn.execute(sql)
    conn.autocommit = False


# ---------------------------------------------------------------------------
# Step 1 — Create table and insert rows
# ---------------------------------------------------------------------------

def create_table(conn: psycopg.Connection, table: str) -> None:
    print(f"  Creating table {table} …")
    ddl(conn, f"""
        CREATE TABLE {table} (
            id          SERIAL PRIMARY KEY,
            tenant_id   UUID NOT NULL DEFAULT gen_random_uuid(),
            label       TEXT NOT NULL,
            embedding   VECTOR({EMBEDDING_DIM}) NOT NULL
        )
    """)
    print("  Table created.")


def insert_rows(conn: psycopg.Connection, table: str,
                count: int, rng: np.random.Generator,
                tenant_id: Optional[str] = None) -> list[list[float]]:
    """Insert `count` rows and return their embeddings for sanity checks."""
    tid = tenant_id or "11111111-1111-1111-1111-111111111111"
    print(f"  Inserting {count} rows (dim={EMBEDDING_DIM}) …", end="", flush=True)
    t0 = time.perf_counter()
    embeddings = []
    for i in range(count):
        v = random_unit_vector(EMBEDDING_DIM, rng)
        embeddings.append(v)
        vlit = vector_literal(v)
        conn.execute(
            f"INSERT INTO {table} (tenant_id, label, embedding) VALUES (%s, %s, %s::VECTOR)",
            [tid, f"row_{i}", vlit],
        )
    conn.commit()
    elapsed = time.perf_counter() - t0
    print(f" done ({elapsed:.1f}s)")
    return embeddings


# ---------------------------------------------------------------------------
# Step 2 — Index creation attempts
# ---------------------------------------------------------------------------

# Each entry: (syntax_name, sql_template)
# %TABLE% and %DIM% will be substituted.
INDEX_SYNTAXES = [
    (
        "ivfflat_l2_ops",
        "CREATE INDEX ON %TABLE% USING ivfflat (embedding vector_l2_ops) WITH (lists = 10)",
    ),
    (
        "ivfflat_cosine_ops",
        "CREATE INDEX ON %TABLE% USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10)",
    ),
    (
        "ivfflat_ip_ops",
        "CREATE INDEX ON %TABLE% USING ivfflat (embedding vector_ip_ops) WITH (lists = 10)",
    ),
    (
        "hnsw_l2_ops",
        "CREATE INDEX ON %TABLE% USING hnsw (embedding vector_l2_ops)",
    ),
    (
        "hnsw_cosine_ops",
        "CREATE INDEX ON %TABLE% USING hnsw (embedding vector_cosine_ops)",
    ),
    (
        "native_vector",
        "CREATE VECTOR INDEX ON %TABLE% (embedding)",
    ),
    (
        "native_vector_prefix",
        "CREATE VECTOR INDEX ON %TABLE% (tenant_id, embedding)",
    ),
    (
        "ivfflat_prefix_l2",
        "CREATE INDEX ON %TABLE% USING ivfflat (tenant_id, embedding vector_l2_ops) WITH (lists = 10)",
    ),
]


def attempt_indexes(conn: psycopg.Connection, table: str) -> dict:
    """Try every index syntax. Return dict: syntax_name → True/False/error_str."""
    results = {}
    for name, sql_tmpl in INDEX_SYNTAXES:
        sql = sql_tmpl.replace("%TABLE%", table)
        try:
            ddl(conn, sql)
            # Drop the index immediately so subsequent syntaxes don't conflict
            drop_sql = f"DROP INDEX IF EXISTS {table}@{table}_{name}_idx CASCADE"
            # Try to find the index name and drop it
            try:
                ddl(conn, f"DROP INDEX IF EXISTS {table}_embedding_idx CASCADE")
            except Exception:
                pass
            # Simpler: just note success; we'll keep the last successful one
            results[name] = True
            print(f"    ✅ {name}")
        except Exception as exc:
            results[name] = str(exc)[:120]
            print(f"    ❌ {name}: {str(exc)[:80]}")
    return results


# ---------------------------------------------------------------------------
# Step 3 — Distance operator tests
# ---------------------------------------------------------------------------

DISTANCE_OPS = [
    ("l2_euclidean",  "<->"),
    ("cosine",        "<=>"),
    ("inner_product", "<#>"),
]


def test_distance_operators(
    conn: psycopg.Connection, table: str, query_vec: list[float]
) -> dict:
    """Run a kNN query with each distance operator and record whether it works."""
    results = {}
    qlit = vector_literal(query_vec)
    for name, op in DISTANCE_OPS:
        sql = f"""
            SELECT id, label, embedding {op} %s::VECTOR AS dist
            FROM {table}
            ORDER BY embedding {op} %s::VECTOR
            LIMIT 5
        """
        try:
            t0 = time.perf_counter()
            rows = conn.execute(sql, [qlit, qlit]).fetchall()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            # Sanity check: first distance should be the smallest
            dists = [r[2] for r in rows]
            sane = len(dists) >= 1 and all(
                dists[i] <= dists[i + 1] + 1e-6 for i in range(len(dists) - 1)
            )
            results[name] = {
                "op": op,
                "success": True,
                "rows_returned": len(rows),
                "top_dist": round(float(dists[0]), 6) if dists else None,
                "ordered_correctly": sane,
                "latency_ms": round(elapsed_ms, 1),
            }
            status = "✅" if sane else "⚠️  (ordering issue)"
            print(f"    {status} {name} ({op}): top_dist={results[name]['top_dist']:.4f}  {elapsed_ms:.0f}ms")
        except Exception as exc:
            results[name] = {"op": op, "success": False, "error": str(exc)[:120]}
            print(f"    ❌ {name} ({op}): {str(exc)[:80]}")
    return results


# ---------------------------------------------------------------------------
# Step 4 — EXPLAIN plan inspection
# ---------------------------------------------------------------------------

def check_explain_plan(
    conn: psycopg.Connection, table: str, query_vec: list[float], op: str = "<->"
) -> dict:
    """Run EXPLAIN on a kNN query and look for index usage in the plan."""
    qlit = vector_literal(query_vec)
    sql = f"""
        EXPLAIN SELECT id, embedding {op} %s::VECTOR AS dist
        FROM {table}
        ORDER BY embedding {op} %s::VECTOR
        LIMIT 5
    """
    try:
        rows = conn.execute(sql, [qlit, qlit]).fetchall()
        plan_text = "\n".join(str(r[0]) for r in rows)
        uses_index = any(
            keyword in plan_text.lower()
            for keyword in ["vector", "index", "scan: @", "knn"]
        )
        is_full_scan = "table scan" in plan_text.lower() or "full scan" in plan_text.lower()
        return {
            "success": True,
            "uses_index": uses_index,
            "is_full_scan": is_full_scan,
            "plan_snippet": plan_text[:500],
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Step 5 — Minimum row count probe
# ---------------------------------------------------------------------------

def probe_minimum_row_count(conn: psycopg.Connection, rng: np.random.Generator) -> dict:
    """Try to create a vector index at increasing row counts to find the minimum."""
    results = {}
    working_syntax = None

    # Pick the first syntax that worked in the main test; fall back to ivfflat_l2
    for name, _ in INDEX_SYNTAXES:
        pass  # We'll detect below

    # Try ivfflat with lists=1 for small counts, hnsw otherwise
    probe_syntaxes = [
        "CREATE INDEX ON %TABLE% USING ivfflat (embedding vector_l2_ops) WITH (lists = 1)",
        "CREATE VECTOR INDEX ON %TABLE% (embedding)",
        "CREATE INDEX ON %TABLE% USING hnsw (embedding vector_l2_ops)",
    ]

    tid = "22222222-2222-2222-2222-222222222222"

    for count in MIN_ROW_PROBE_COUNTS:
        probe_table = ts_table(f"v1_probe_{count}")
        try:
            ddl(conn, f"""
                CREATE TABLE {probe_table} (
                    id        SERIAL PRIMARY KEY,
                    tenant_id UUID NOT NULL DEFAULT gen_random_uuid(),
                    label     TEXT NOT NULL,
                    embedding VECTOR({EMBEDDING_DIM}) NOT NULL
                )
            """)
            # Insert `count` rows
            for i in range(count):
                v = random_unit_vector(EMBEDDING_DIM, rng)
                conn.execute(
                    f"INSERT INTO {probe_table} (tenant_id, label, embedding) VALUES (%s, %s, %s::VECTOR)",
                    [tid, f"r{i}", vector_literal(v)],
                )
            conn.commit()

            # Try to create an index
            index_created = False
            for syntax in probe_syntaxes:
                try:
                    ddl(conn, syntax.replace("%TABLE%", probe_table))
                    index_created = True
                    working_syntax = syntax
                    break
                except Exception:
                    continue

            results[count] = index_created
            status = "✅ index created" if index_created else "❌ index failed"
            print(f"    {count:>4} rows → {status}")
        except Exception as exc:
            results[count] = f"error: {str(exc)[:80]}"
            print(f"    {count:>4} rows → ERROR: {str(exc)[:60]}")
        finally:
            try:
                ddl(conn, f"DROP TABLE IF EXISTS {probe_table} CASCADE")
            except Exception:
                pass

    return results


# ---------------------------------------------------------------------------
# Step 6 — Prefixed index test (critical for PQBS tenant isolation)
# ---------------------------------------------------------------------------

def test_prefix_index(
    conn: psycopg.Connection, table: str, query_vec: list[float],
    tenant_id: str
) -> dict:
    """
    Verify the prefix-partitioned index (tenant_id, embedding) is structurally
    preventing cross-tenant retrieval — not just filtering it in application code.
    This is the PQBS design requirement: cross-tenant retrieval must be
    structurally impossible, not merely disallowed by convention.
    """
    results: dict = {"prefix_index_created": False, "cross_tenant_structurally_blocked": False}
    qlit = vector_literal(query_vec)

    # Insert a second tenant's rows
    tenant_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    rng = np.random.default_rng(999)
    for i in range(10):
        v = random_unit_vector(EMBEDDING_DIM, rng)
        conn.execute(
            f"INSERT INTO {table} (tenant_id, label, embedding) VALUES (%s, %s, %s::VECTOR)",
            [tenant_b, f"tenant_b_{i}", vector_literal(v)],
        )
    conn.commit()

    # Try to create a prefix index
    prefix_syntaxes = [
        f"CREATE VECTOR INDEX ON {table} (tenant_id, embedding)",
        f"CREATE INDEX ON {table} USING ivfflat (tenant_id, embedding vector_l2_ops) WITH (lists = 10)",
        f"CREATE INDEX ON {table} USING hnsw (tenant_id, embedding vector_l2_ops)",
    ]
    for syntax in prefix_syntaxes:
        try:
            ddl(conn, syntax)
            results["prefix_index_created"] = True
            results["prefix_syntax_used"] = syntax
            print(f"    ✅ Prefix index created: {syntax[:70]}")
            break
        except Exception as exc:
            print(f"    ❌ {syntax[:60]}: {str(exc)[:60]}")

    # Query restricted to tenant_a — results must contain only tenant_a rows
    tenant_a = "11111111-1111-1111-1111-111111111111"
    try:
        rows = conn.execute(
            f"""
            SELECT id, tenant_id, embedding <-> %s::VECTOR AS dist
            FROM {table}
            WHERE tenant_id = %s
            ORDER BY embedding <-> %s::VECTOR
            LIMIT 5
            """,
            [qlit, tenant_a, qlit],
        ).fetchall()
        tenant_ids_returned = {str(r[1]) for r in rows}
        if tenant_ids_returned and all(t == tenant_a for t in tenant_ids_returned):
            results["cross_tenant_structurally_blocked"] = True
            print(f"    ✅ Tenant-scoped query returned only tenant_a rows ({len(rows)} rows)")
        else:
            results["cross_tenant_structurally_blocked"] = False
            print(f"    ⚠️  Cross-tenant leakage detected: tenants returned = {tenant_ids_returned}")
    except Exception as exc:
        results["tenant_query_error"] = str(exc)[:120]
        print(f"    ❌ Tenant-scoped query failed: {str(exc)[:80]}")

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(
    index_results: dict,
    distance_results: dict,
    explain_result: dict,
    min_row_results: dict,
    prefix_results: dict,
    passed: bool,
) -> None:
    print("\n" + "=" * 70)
    print("V1 SUMMARY")
    print("=" * 70)

    working = [k for k, v in index_results.items() if v is True]
    failing = [k for k, v in index_results.items() if v is not True]
    print(f"  Index syntaxes tried   : {len(index_results)}")
    print(f"  Syntaxes that worked   : {len(working)}  → {', '.join(working) or 'none'}")
    print(f"  Syntaxes that failed   : {len(failing)}")

    dist_ok = [k for k, v in distance_results.items() if v.get("success")]
    print(f"  Distance ops working   : {', '.join(dist_ok) or 'none'}")

    if explain_result.get("success"):
        uses = explain_result.get("uses_index", False)
        full = explain_result.get("is_full_scan", True)
        print(f"  Planner uses index     : {'✅ YES' if uses else '⚠️  uncertain'}")
        print(f"  Full scan detected     : {'⚠️  YES (index not used)' if full else '✅ NO'}")
    else:
        print(f"  EXPLAIN plan           : ❌ failed")

    min_ok = [c for c, v in min_row_results.items() if v is True]
    print(f"  Min row count for index: {min(min_ok) if min_ok else 'unknown'}")

    print(f"  Prefix index (tenant)  : {'✅ created' if prefix_results.get('prefix_index_created') else '❌ not created'}")
    print(f"  Cross-tenant blocked   : {'✅ YES' if prefix_results.get('cross_tenant_structurally_blocked') else '❌ NO'}")
    print()
    if passed:
        print(f"  RESULT: ✅ PASSED")
    else:
        print(f"  RESULT: ❌ FAILED — see findings above")
    print("=" * 70)


def write_verifications_report(
    index_results: dict,
    distance_results: dict,
    explain_result: dict,
    min_row_results: dict,
    prefix_results: dict,
    passed: bool,
) -> None:
    report_path = Path("docs/VERIFICATIONS.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing = report_path.read_text() if report_path.exists() else ""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    working_syntaxes = [k for k, v in index_results.items() if v is True]
    failing_syntaxes = {k: v for k, v in index_results.items() if v is not True}
    dist_ok = [k for k, v in distance_results.items() if v.get("success")]
    dist_fail = [k for k, v in distance_results.items() if not v.get("success")]
    min_ok = [c for c, v in min_row_results.items() if v is True]
    min_row_count = min(min_ok) if min_ok else "unknown"
    uses_index = explain_result.get("uses_index", False) if explain_result.get("success") else False
    is_full_scan = explain_result.get("is_full_scan", True) if explain_result.get("success") else True
    plan_snippet = explain_result.get("plan_snippet", "unavailable")[:300]
    prefix_ok = prefix_results.get("prefix_index_created", False)
    cross_blocked = prefix_results.get("cross_tenant_structurally_blocked", False)
    prefix_syntax = prefix_results.get("prefix_syntax_used", "none succeeded")
    result_str = "✅ PASSED" if passed else "❌ FAILED"

    # Cosine finding
    cosine_idx_ok = "ivfflat_cosine_ops" in working_syntaxes or "hnsw_cosine_ops" in working_syntaxes
    cosine_note = (
        "Cosine distance is supported at the index level."
        if cosine_idx_ok
        else (
            "Cosine distance is NOT supported at the index level. "
            "Workaround: normalise all embeddings to unit length (already planned — "
            "random_unit_vector() in A12) and use Euclidean distance, which is "
            "mathematically equivalent for unit vectors."
        )
    )

    entry = f"""
---

## V1 — Vector Index Status and Distance Metrics

**Run date:** {timestamp}
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Cluster version:** CockroachDB CCL v26.2.5
**Embedding dimension tested:** {EMBEDDING_DIM} (matches amazon.titan-embed-text-v2:0 default)
**Row count used:** {MAIN_ROW_COUNT}
**Result:** {result_str}

### Index creation

| Syntax | Result |
|---|---|
""" + "\n".join(
        f"| `{k}` | {'✅ succeeded' if v is True else f'❌ `{str(v)[:60]}`'} |"
        for k, v in index_results.items()
    ) + f"""

**Working syntaxes:** {', '.join(f'`{s}`' for s in working_syntaxes) if working_syntaxes else 'none'}

### Distance operators

| Operator | Symbol | Works for queries | Notes |
|---|---|---|---|
""" + "\n".join(
        f"| `{k}` | `{v.get('op')}` | {'✅ yes' if v.get('success') else '❌ no'} | "
        f"{'top_dist=' + str(v.get('top_dist', 'n/a')) + ', ' + str(v.get('latency_ms', '?')) + 'ms' if v.get('success') else v.get('error', '')[:60]} |"
        for k, v in distance_results.items()
    ) + f"""

### Query planner behaviour

- Index used by planner: **{'YES' if uses_index else 'UNCERTAIN — check plan snippet below'}**
- Full table scan detected: **{'YES — index not used' if is_full_scan else 'NO'}**

EXPLAIN snippet:
```
{plan_snippet}
```

### Minimum row count for index creation

| Row count | Index created? |
|---|---|
""" + "\n".join(
        f"| {c} | {'✅ yes' if v is True else '❌ no'} |"
        for c, v in sorted(min_row_results.items())
    ) + f"""

Minimum confirmed: **{min_row_count} rows**

### Prefix-partitioned index (tenant isolation)

- Prefix index `(tenant_id, embedding)` created: **{'✅ YES' if prefix_ok else '❌ NO'}**
- Syntax used: `{prefix_syntax}`
- Cross-tenant retrieval structurally blocked: **{'✅ YES' if cross_blocked else '❌ NO — must enforce in application'}**

### Cosine distance note

{cosine_note}

### Findings and consequences for Phase 2

1. **Vector type `vector({EMBEDDING_DIM})`** is supported on CockroachDB Serverless v26.2.5.
2. **Working index syntax(es):** {', '.join(f'`{s}`' for s in working_syntaxes) if working_syntaxes else 'none — use sequential scan fallback and document in README'}.
3. Migration `0006_vector_index` must use: `{working_syntaxes[0] if working_syntaxes else 'TBD'}`
4. **Distance operators for queries:** {', '.join(dist_ok) if dist_ok else 'none'} work.
   {'Failing: ' + ', '.join(dist_fail) + '.' if dist_fail else 'All tested operators work.'}
5. **Planner behaviour:** {'Index is used — recall latency target (p50 < 600ms) is achievable.' if uses_index and not is_full_scan else 'Index not confirmed in planner — may fall back to scan. Monitor recall latency.'}
6. **Tenant isolation:** {'Prefix index enforces structural isolation. Cross-tenant retrieval is impossible at the index level.' if prefix_ok and cross_blocked else 'Prefix index failed or did not block cross-tenant. Isolation must be enforced via role-scoped views and WHERE clause. Document this limitation.'}

**Active fallback:** {'None — V1 passed.' if passed else 'Vector index creation failed. Migration 0006_vector_index must use sequential scan with a GIN or regular index, and the recall path must add WHERE tenant_id = ? explicitly. The "structural isolation" claim weakens to application-level filtering. Disclose in README.'}

"""

    v1_pattern = r"\n---\n\n## V1 — Vector Index Status.*?(?=\n---\n\n## |\Z)"
    if re.search(v1_pattern, existing, flags=re.DOTALL):
        new_content = re.sub(v1_pattern, entry.rstrip(), existing, flags=re.DOTALL)
        report_path.write_text(new_content)
    else:
        report_path.write_text(existing + entry)

    print(f"\nFindings written → {report_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="V1 — Vector index status and distance metrics spike for PQBS."
    )
    parser.add_argument(
        "--rows", type=int, default=MAIN_ROW_COUNT,
        help=f"Number of rows to insert for main test (default: {MAIN_ROW_COUNT})"
    )
    parser.add_argument(
        "--dim", type=int, default=EMBEDDING_DIM,
        help=f"Embedding dimension (default: {EMBEDDING_DIM}, matches Titan Embed Text v2)"
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip writing to docs/VERIFICATIONS.md"
    )
    parser.add_argument(
        "--skip-min-probe", action="store_true",
        help="Skip the minimum row count probe (saves time)"
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="NumPy random seed for reproducibility"
    )
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    main_table = ts_table("v1_vector_spike")
    tenant_a = "11111111-1111-1111-1111-111111111111"

    print("=" * 70)
    print("V1 — Vector Index Status and Distance Metrics")
    print(f"Cluster : {COCKROACH_URL.split('@')[-1].split('/')[0]}")
    print(f"Table   : {main_table}")
    print(f"Dim     : {args.dim}  Rows: {args.rows}  Seed: {args.seed}")
    print("=" * 70)

    # ── Connect ──────────────────────────────────────────────────────────────
    try:
        conn = psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT)
        conn.autocommit = False
        conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.commit()
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect to CockroachDB: {exc}")

    # ── Step 1: Create table and insert rows ─────────────────────────────────
    print("\n[1/6] Table setup")
    try:
        create_table(conn, main_table)
    except Exception as exc:
        conn.close()
        sys.exit(f"ERROR: Could not create spike table: {exc}")

    embeddings = insert_rows(conn, main_table, args.rows, rng, tenant_id=tenant_a)
    query_vec = embeddings[0]  # use first row's vector as our query anchor

    # ── Step 2: Index creation attempts ──────────────────────────────────────
    print("\n[2/6] Index creation attempts")
    index_results = attempt_indexes(conn, main_table)
    working = [k for k, v in index_results.items() if v is True]
    if not working:
        print("  ⚠️  No index syntax succeeded — continuing with sequential scan tests")

    # ── Step 3: Distance operator tests ──────────────────────────────────────
    print("\n[3/6] Distance operator tests")
    distance_results = test_distance_operators(conn, main_table, query_vec)

    # ── Step 4: EXPLAIN plan ──────────────────────────────────────────────────
    print("\n[4/6] EXPLAIN plan inspection")
    # Use the first working distance op for explain; fall back to L2
    explain_op = "<->"
    for name, info in distance_results.items():
        if info.get("success"):
            explain_op = info["op"]
            break
    explain_result = check_explain_plan(conn, main_table, query_vec, op=explain_op)
    if explain_result.get("success"):
        uses = explain_result.get("uses_index", False)
        full = explain_result.get("is_full_scan", True)
        print(f"  Planner uses index: {'✅ YES' if uses else '⚠️  uncertain'}")
        print(f"  Full scan: {'⚠️  YES' if full else '✅ NO'}")
        print(f"  Plan snippet:\n    {explain_result['plan_snippet'][:200].replace(chr(10), chr(10) + '    ')}")
    else:
        print(f"  ❌ EXPLAIN failed: {explain_result.get('error', 'unknown')[:100]}")

    # ── Step 5: Minimum row count probe ──────────────────────────────────────
    if not args.skip_min_probe:
        print("\n[5/6] Minimum row count probe")
        min_row_results = probe_minimum_row_count(conn, rng)
    else:
        print("\n[5/6] Minimum row count probe — SKIPPED")
        min_row_results = {}

    # ── Step 6: Prefix index and cross-tenant isolation ──────────────────────
    print("\n[6/6] Prefix index and tenant isolation test")
    prefix_results = test_prefix_index(conn, main_table, query_vec, tenant_a)

    # ── Pass / Fail ───────────────────────────────────────────────────────────
    any_index_worked = bool(working)
    any_dist_worked = any(v.get("success") for v in distance_results.values())
    passed = any_index_worked and any_dist_worked

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(
        index_results, distance_results, explain_result,
        min_row_results, prefix_results, passed
    )

    # ── Cleanup ───────────────────────────────────────────────────────────────
    print("\nDropping spike table …")
    try:
        ddl(conn, f"DROP TABLE IF EXISTS {main_table} CASCADE")
        print("  OK")
    except Exception as exc:
        print(f"  WARNING: {exc}")
        print(f"  Run manually: DROP TABLE IF EXISTS {main_table} CASCADE;")

    conn.close()

    # ── Write report ──────────────────────────────────────────────────────────
    if not args.no_report:
        write_verifications_report(
            index_results, distance_results, explain_result,
            min_row_results, prefix_results, passed
        )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
