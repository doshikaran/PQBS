"""
Demo script: recall query demonstration.

Connects to the DB, performs a recall query for a hardcoded subject,
prints the results, and shows the retrieval_id from the audit log.

Usage:
    PYTHONPATH=src python demo/scripts/demo_recall.py

Environment variables required:
    COCKROACH_URL       — CockroachDB connection URL
    AWS_REGION          — AWS region for Bedrock embedding
    AWS_ACCESS_KEY_ID   — (or use IAM role / env-level creds)
    AWS_SECRET_ACCESS_KEY

Optional:
    DEMO_TENANT_ID      — UUID of the tenant to query (default: Northwind demo tenant)
    DEMO_QUERY          — Query text (default: hard-coded subject query)
    DEMO_LIMIT          — Number of results (default: 5)
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEMO_TENANT_ID = os.environ.get(
    "DEMO_TENANT_ID",
    # Northwind demo tenant seeded by scripts/seed_demo.py
    "ffffffff-ffff-ffff-ffff-000000000001",
)

DEMO_QUERY = os.environ.get(
    "DEMO_QUERY",
    "What is Alice's current employer?",
)

DEMO_LIMIT = int(os.environ.get("DEMO_LIMIT", "5"))

AWS_REGION = os.environ.get("AWS_REGION")


def main() -> None:
    if not os.environ.get("COCKROACH_URL"):
        print("ERROR: COCKROACH_URL is not set.", file=sys.stderr)
        sys.exit(1)

    if not AWS_REGION:
        print("WARNING: AWS_REGION is not set. Embedding call will fail.", file=sys.stderr)

    print("=" * 60)
    print("PQBS Demo — Recall Query (A9)")
    print("=" * 60)
    print(f"Tenant  : {DEMO_TENANT_ID}")
    print(f"Query   : {DEMO_QUERY!r}")
    print(f"Limit   : {DEMO_LIMIT}")
    print(f"Region  : {AWS_REGION or '(not set)'}")
    print()

    from pqbs.agents.consumer.a9_recall import RecallAgent
    from pqbs.contracts import RecallRequest
    from pqbs.substrate.connection import get_connection

    tenant_id: uuid.UUID
    try:
        tenant_id = uuid.UUID(DEMO_TENANT_ID)
    except ValueError:
        print(f"ERROR: DEMO_TENANT_ID is not a valid UUID: {DEMO_TENANT_ID}", file=sys.stderr)
        sys.exit(1)

    agent = RecallAgent()
    request = RecallRequest(
        query=DEMO_QUERY,
        tenant_id=tenant_id,
        limit=DEMO_LIMIT,
    )

    print("Connecting to CockroachDB...")
    conn = get_connection()

    print(f"Embedding query via Amazon Bedrock (region: {AWS_REGION})...")
    start = datetime.now(tz=timezone.utc)

    try:
        result = agent.recall(request, conn, region=AWS_REGION)
    except Exception as exc:
        print(f"\nERROR during recall: {exc}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    elapsed = (datetime.now(tz=timezone.utc) - start).total_seconds() * 1000

    print()
    print(f"Retrieval ID  : {result.retrieval_id}")
    print(f"Request ID    : {result.request_id}")
    print(f"Retrieved at  : {result.retrieved_at.isoformat()}")
    print(f"Latency       : {result.query_latency_ms}ms (wall clock: {elapsed:.0f}ms)")
    print(f"Results       : {len(result.beliefs)} belief(s)")
    print()

    if result.beliefs:
        print("-" * 60)
        print(f"{'#':<3} {'Subject':<20} {'Predicate':<16} {'Object':<20} {'Trust':>6}")
        print("-" * 60)
        for i, b in enumerate(result.beliefs, start=1):
            subject = b.subject[:19]
            predicate = b.predicate[:15]
            obj = b.object[:19]
            trust = f"{b.trust_score:.3f}"
            print(f"{i:<3} {subject:<20} {predicate:<16} {obj:<20} {trust:>6}")
        print("-" * 60)
    else:
        print("No trusted beliefs matched the query.")
        print("Possible reasons:")
        print("  - No beliefs have status='trusted' for this tenant")
        print("  - The query is semantically distant from stored beliefs")
        print("  - Run scripts/seed_demo.py then screen beliefs via A4/E2 first")

    # Verify retrieval_log entry
    print()
    print("Verifying retrieval_log entry...")
    log_rows = conn.execute(
        """
SELECT retrieval_id, requesting_agent_id, query_digest, retrieved_at,
       jsonb_array_length(returned_belief_ids) AS returned_count
FROM retrieval_log
WHERE tenant_id = %s
  AND retrieval_id = %s
""",
        (str(tenant_id), str(result.retrieval_id)),
    ).fetchall()

    if log_rows:
        r = log_rows[0]
        print(f"  retrieval_id       : {r['retrieval_id']}")
        print(f"  requesting_agent_id: {r['requesting_agent_id']}")
        print(f"  query_digest       : {r['query_digest'][:16]}...")
        print(f"  returned_count     : {r['returned_count']}")
        print(f"  retrieved_at       : {str(r['retrieved_at'])[:19]}")
        print()
        print("Audit log entry confirmed.")
    else:
        print("WARNING: No retrieval_log entry found for this retrieval_id.")

    conn.close()
    print()
    print("Done.")


if __name__ == "__main__":
    main()
