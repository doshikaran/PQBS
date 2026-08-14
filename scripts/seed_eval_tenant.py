"""Seed script for the PQBS eval tenant — Phase 8 red-team harness.

Usage:
  python scripts/seed_eval_tenant.py [--tenant eval] [--reset]

Creates:
  - 7 predicate policies (same as demo tenant)
  - 6 agent identities (same 5 as demo + a15_redteam)
  - Does NOT seed beliefs — the harness seeds those from corpus files

The eval tenant UUID is read from TENANT_ID_EVAL in .env
(default: eeee0000-0000-0000-0000-000000000003).

Final check: print predicate policy and agent identity counts.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql as pgsql
from psycopg.rows import dict_row

# Ensure src/ is on the path when run directly
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

load_dotenv(REPO_ROOT / ".env")

_DEFAULT_EVAL_TENANT = "eeee0000-0000-0000-0000-000000000003"
TENANT_ID_EVAL = uuid.UUID(
    os.environ.get("TENANT_ID_EVAL", _DEFAULT_EVAL_TENANT)
)

# Guard: eval tenant must not equal demo tenant
_DEMO_TENANT = uuid.UUID(os.environ.get("TENANT_ID_DEMO", "cccc0000-0000-0000-0000-000000000001"))
if TENANT_ID_EVAL == _DEMO_TENANT:
    print(
        "ERROR: TENANT_ID_EVAL must not equal TENANT_ID_DEMO. "
        "Use a separate tenant to prevent cross-contamination.",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Predicate policies (same as demo tenant)
# ---------------------------------------------------------------------------

PREDICATE_POLICIES = [
    {
        "predicate": "customer_tier",
        "cardinality": "single_valued",
        "resolution_strategy": "recency",
        "min_source_tier": "unverified",
        "is_sensitive": False,
        "normalization_rule": "lowercase",
        "expected_value_domain": '["gold","silver","bronze","platinum"]',
    },
    {
        "predicate": "preferred_carrier",
        "cardinality": "single_valued",
        "resolution_strategy": "recency",
        "min_source_tier": "unverified",
        "is_sensitive": False,
        "normalization_rule": "none",
        "expected_value_domain": '["FedEx","UPS","DHL","USPS"]',
    },
    {
        "predicate": "payment_terms",
        "cardinality": "single_valued",
        "resolution_strategy": "recency",
        "min_source_tier": "corroborated",
        "is_sensitive": False,
        "normalization_rule": "lowercase",
        "expected_value_domain": '["net30","net60","net90","immediate"]',
    },
    {
        "predicate": "credit_limit",
        "cardinality": "single_valued",
        "resolution_strategy": "confidence",
        "min_source_tier": "authoritative",
        "is_sensitive": True,
        "normalization_rule": "none",
        "expected_value_domain": None,
    },
    {
        "predicate": "city",
        "cardinality": "single_valued",
        "resolution_strategy": "recency",
        "min_source_tier": "unverified",
        "is_sensitive": False,
        "normalization_rule": "title_case",
        "expected_value_domain": None,
    },
    {
        "predicate": "account_status",
        "cardinality": "single_valued",
        "resolution_strategy": "recency",
        "min_source_tier": "authoritative",
        "is_sensitive": False,
        "normalization_rule": "lowercase",
        "expected_value_domain": '["active","inactive","suspended"]',
    },
    {
        "predicate": "contact_email",
        "cardinality": "single_valued",
        "resolution_strategy": "recency",
        "min_source_tier": "unverified",
        "is_sensitive": True,
        "normalization_rule": "lowercase",
        "expected_value_domain": None,
    },
]

# ---------------------------------------------------------------------------
# Agent identities (same 5 as demo + a15_redteam)
# ---------------------------------------------------------------------------

AGENT_IDENTITIES = [
    {
        "agent_id": "a1_ingestion",
        "agent_class": "producer",
        "db_role": "role_producer",
        "credential_ref": "arn:aws:secretsmanager:ap-south-1::secret/pqbs/a1_ingestion",
        "trust_multiplier": 1.0,
    },
    {
        "agent_id": "a2_inference",
        "agent_class": "producer",
        "db_role": "role_producer",
        "credential_ref": "arn:aws:secretsmanager:ap-south-1::secret/pqbs/a2_inference",
        "trust_multiplier": 0.8,
    },
    {
        "agent_id": "a3_correction",
        "agent_class": "integrity",
        "db_role": "role_integrity",
        "credential_ref": "arn:aws:secretsmanager:ap-south-1::secret/pqbs/a3_correction",
        "trust_multiplier": 1.5,
    },
    {
        "agent_id": "a11_canonicalize",
        "agent_class": "semantics",
        "db_role": "role_semantics",
        "credential_ref": "arn:aws:secretsmanager:ap-south-1::secret/pqbs/a11_canonicalize",
        "trust_multiplier": 1.0,
    },
    {
        "agent_id": "a12_embed",
        "agent_class": "semantics",
        "db_role": "role_semantics",
        "credential_ref": "arn:aws:secretsmanager:ap-south-1::secret/pqbs/a12_embed",
        "trust_multiplier": 1.0,
    },
    {
        "agent_id": "a15_redteam",
        "agent_class": "integrity",
        "db_role": "role_integrity",
        "credential_ref": "arn:aws:secretsmanager:ap-south-1::secret/pqbs/a15_redteam",
        "trust_multiplier": 1.0,
    },
]

# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset_tenant(conn: psycopg.Connection, tenant_id: uuid.UUID) -> None:
    """Delete all data for the eval tenant (dangerous — only for dev reset)."""
    print(f"Resetting eval tenant {tenant_id}...")
    tables = [
        "quarantine", "integrity_verdict", "contradiction_event",
        "retrieval_log", "working_memory", "belief", "provenance",
        "predicate_policy", "agent_identity",
    ]
    for table in tables:
        conn.execute(
            pgsql.SQL("DELETE FROM {} WHERE tenant_id = %s").format(pgsql.Identifier(table)),
            (str(tenant_id),),
        )
        conn.commit()
    print("  Reset complete.")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_predicate_policies(conn: psycopg.Connection, tenant_id: uuid.UUID) -> None:
    print(f"Seeding {len(PREDICATE_POLICIES)} predicate policies...")
    for policy in PREDICATE_POLICIES:
        conn.execute(
            """
            INSERT INTO predicate_policy
              (tenant_id, predicate, cardinality, resolution_strategy,
               min_source_tier, is_sensitive, normalization_rule, expected_value_domain)
            VALUES (%s, %s, %s::cardinality, %s::resolution_strategy,
                    %s::trust_tier, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, predicate) DO NOTHING
            """,
            (
                str(tenant_id),
                policy["predicate"],
                policy["cardinality"],
                policy["resolution_strategy"],
                policy["min_source_tier"],
                policy["is_sensitive"],
                policy["normalization_rule"],
                policy["expected_value_domain"],
            ),
        )
    conn.commit()
    print("  Done.")


def seed_agent_identities(conn: psycopg.Connection, tenant_id: uuid.UUID) -> None:
    print(f"Seeding {len(AGENT_IDENTITIES)} agent identities...")
    now = datetime.now(tz=timezone.utc)
    for agent in AGENT_IDENTITIES:
        conn.execute(
            """
            INSERT INTO agent_identity
              (agent_id, tenant_id, agent_class, db_role, credential_ref,
               registered_at, trust_multiplier, status)
            VALUES (%s, %s, %s::agent_class, %s, %s, %s, %s, 'active'::agent_status)
            ON CONFLICT (tenant_id, agent_id) DO NOTHING
            """,
            (
                agent["agent_id"],
                str(tenant_id),
                agent["agent_class"],
                agent["db_role"],
                agent["credential_ref"],
                now,
                agent["trust_multiplier"],
            ),
        )
    conn.commit()
    print("  Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the PQBS eval tenant for Phase 8 red-team harness."
    )
    parser.add_argument(
        "--tenant",
        default="eval",
        help="Tenant name (informational only; UUID comes from TENANT_ID_EVAL in .env)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all existing data for the eval tenant before seeding (DANGEROUS)",
    )
    args = parser.parse_args()

    url = os.environ.get("COCKROACH_URL")
    if not url:
        print("ERROR: COCKROACH_URL not set in environment / .env", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to CockroachDB...")
    print(f"Tenant: {args.tenant} ({TENANT_ID_EVAL})")

    conn = psycopg.connect(url, autocommit=False, row_factory=dict_row)  # type: ignore[call-overload]

    try:
        if args.reset:
            reset_tenant(conn, TENANT_ID_EVAL)

        seed_predicate_policies(conn, TENANT_ID_EVAL)
        seed_agent_identities(conn, TENANT_ID_EVAL)

        # Final verification
        policy_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM predicate_policy WHERE tenant_id = %s",
            (str(TENANT_ID_EVAL),),
        ).fetchone()
        policy_count = policy_row["cnt"] if policy_row else 0  # type: ignore[index]

        agent_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM agent_identity WHERE tenant_id = %s",
            (str(TENANT_ID_EVAL),),
        ).fetchone()
        agent_count = agent_row["cnt"] if agent_row else 0  # type: ignore[index]

        print()
        print("=" * 60)
        print("Eval tenant seed complete.")
        print(f"  Predicate policies : {policy_count}")
        print(f"  Agent identities   : {agent_count}")
        print(f"  Beliefs seeded     : 0 (harness seeds from corpus)")
        print("=" * 60)

        if policy_count < len(PREDICATE_POLICIES):
            print(
                f"WARNING: Expected {len(PREDICATE_POLICIES)} predicate policies, "
                f"got {policy_count}",
                file=sys.stderr,
            )
            sys.exit(1)

        if agent_count < len(AGENT_IDENTITIES):
            print(
                f"WARNING: Expected {len(AGENT_IDENTITIES)} agent identities, "
                f"got {agent_count}",
                file=sys.stderr,
            )
            sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
