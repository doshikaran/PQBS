"""Seed script for the PQBS demo tenant — Northwind Logistics.

Usage:
  python scripts/seed_demo.py [--tenant northwind] [--reset]

Creates:
  - 7 predicate policies for Northwind predicates
  - 5 agent identities (a1_ingestion, a2_inference, a3_correction,
    a11_canonicalize, a12_embed)
  - 100 subjects (customers C001-C100, suppliers S001-S020,
    products P001-P020, vehicles V001-V010)
  - 2100+ belief records (status='pending') with one provenance per belief

Deterministic output: uses random.seed(42).

The demo tenant UUID is read from TENANT_ID_DEMO in .env.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

# Ensure src/ is on the path when run directly
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

load_dotenv(REPO_ROOT / ".env")

TENANT_ID_DEMO = uuid.UUID(os.environ["TENANT_ID_DEMO"])

# ---------------------------------------------------------------------------
# Northwind domain data
# ---------------------------------------------------------------------------

CITIES = [
    "New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
    "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
    "Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte",
    "Indianapolis", "San Francisco", "Seattle", "Denver", "Nashville",
    "Oklahoma City", "El Paso", "Washington", "Boston", "Las Vegas",
    "Louisville", "Portland", "Memphis", "Baltimore", "Milwaukee",
    "Albuquerque", "Tucson", "Fresno", "Sacramento", "Mesa",
    "Atlanta", "Omaha", "Colorado Springs", "Raleigh", "Long Beach",
    "Virginia Beach", "Minneapolis", "Tampa", "New Orleans", "Honolulu",
]

CUSTOMER_TIERS = ["gold", "silver", "bronze", "platinum"]
CARRIERS = ["FedEx", "UPS", "DHL", "USPS"]
PAYMENT_TERMS = ["net30", "net60", "net90", "immediate"]
ACCOUNT_STATUSES = ["active", "inactive", "suspended"]

PREDICATES = [
    "customer_tier",
    "preferred_carrier",
    "payment_terms",
    "credit_limit",
    "city",
    "account_status",
    "contact_email",
]

# ---------------------------------------------------------------------------
# Predicate policies
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
# Agent identities
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
]


# ---------------------------------------------------------------------------
# Subject generation
# ---------------------------------------------------------------------------

def generate_subjects() -> list[str]:
    """Generate 100 subjects: C001-C100, S001-S020, P001-P020, V001-V010."""
    subjects: list[str] = []
    subjects.extend([f"C{i:03d}" for i in range(1, 101)])   # 100 customers
    subjects.extend([f"S{i:03d}" for i in range(1, 21)])    # 20 suppliers
    subjects.extend([f"P{i:03d}" for i in range(1, 21)])    # 20 products
    subjects.extend([f"V{i:03d}" for i in range(1, 11)])    # 10 vehicles
    return subjects


# ---------------------------------------------------------------------------
# Value generation per predicate
# ---------------------------------------------------------------------------

def generate_value(predicate: str, subject: str, rng: random.Random) -> str:
    if predicate == "customer_tier":
        return rng.choice(CUSTOMER_TIERS)
    elif predicate == "preferred_carrier":
        return rng.choice(CARRIERS)
    elif predicate == "payment_terms":
        return rng.choice(PAYMENT_TERMS)
    elif predicate == "credit_limit":
        return str(round(rng.randint(5, 500) * 1000))
    elif predicate == "city":
        return rng.choice(CITIES)
    elif predicate == "account_status":
        return rng.choice(ACCOUNT_STATUSES)
    elif predicate == "contact_email":
        local = subject.lower().replace(" ", "_")
        domain = rng.choice(["northwind.com", "example.com", "logistics.io"])
        return f"{local}@{domain}"
    return "unknown"


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

def reset_tenant(conn: psycopg.Connection, tenant_id: uuid.UUID) -> None:
    """Delete all data for the tenant (dangerous — only for dev reset)."""
    print(f"Resetting tenant {tenant_id}...")
    tables = [
        "quarantine", "integrity_verdict", "contradiction_event",
        "retrieval_log", "working_memory", "belief", "provenance",
        "predicate_policy", "agent_identity",
    ]
    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (str(tenant_id),))
        conn.commit()
    print("  Reset complete.")


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


def seed_beliefs(conn: psycopg.Connection, tenant_id: uuid.UUID) -> tuple[int, int]:
    """
    Seed 2100+ beliefs with provenance records.

    Strategy:
      - 100 subjects × 7 predicates × 3 time snapshots = 2100 beliefs
      - Each belief gets its own provenance record
      - random.seed(42) for determinism
      - valid_from varies across 3 snapshots: T-365d, T-180d, T-30d

    Returns:
      (belief_count, provenance_count)
    """
    rng = random.Random(42)
    subjects = generate_subjects()
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)

    # 3 time snapshots simulating historical loads
    time_snapshots = [
        now - timedelta(days=365),
        now - timedelta(days=180),
        now - timedelta(days=30),
    ]

    episode_ids = [uuid.UUID(f"00000000-0000-0000-0000-{i:012d}") for i in range(1, 8)]

    belief_count = 0
    prov_count = 0

    BATCH_SIZE = 200  # commit every N inserts for efficiency

    print(f"Seeding beliefs for {len(subjects)} subjects × {len(PREDICATES)} predicates × {len(time_snapshots)} snapshots = {len(subjects) * len(PREDICATES) * len(time_snapshots)} beliefs...")

    batch_beliefs = []
    batch_provs = []

    for subject in subjects:
        for predicate in PREDICATES:
            for snapshot_idx, valid_from in enumerate(time_snapshots):
                prov_id = uuid.uuid4()
                belief_id = uuid.uuid4()
                object_value = generate_value(predicate, subject, rng)
                confidence = round(rng.uniform(0.6, 0.99), 4)
                episode_id = episode_ids[PREDICATES.index(predicate)]
                ingested_at = valid_from + timedelta(hours=rng.randint(0, 23))
                source_digest = rng.randbytes(32).hex()  # 64 hex chars

                batch_provs.append((
                    str(prov_id),
                    str(tenant_id),
                    "system_of_record",
                    f"crm://northwind/bulk/{snapshot_idx + 1}",
                    source_digest,
                    str(episode_id),
                    ingested_at,
                    "authoritative",
                    "a1_ingestion",
                ))

                batch_beliefs.append((
                    str(tenant_id),
                    str(belief_id),
                    subject,
                    predicate,
                    object_value,
                    confidence,
                    valid_from,
                    "pending",
                    "a1_ingestion",
                    str(prov_id),
                    "normal",
                ))

                prov_count += 1
                belief_count += 1

                if len(batch_beliefs) >= BATCH_SIZE:
                    _flush_batch(conn, batch_provs, batch_beliefs)
                    batch_beliefs = []
                    batch_provs = []

    # Flush remaining
    if batch_beliefs:
        _flush_batch(conn, batch_provs, batch_beliefs)

    print(f"  Seeded {belief_count} beliefs and {prov_count} provenance records.")
    return belief_count, prov_count


def _flush_batch(
    conn: psycopg.Connection,
    batch_provs: list[tuple],
    batch_beliefs: list[tuple],
) -> None:
    """Flush a batch of provenance and belief rows, then commit.

    psycopg3: executemany() lives on the cursor, not the connection.
    We use the connection as a context manager to get a cursor.
    """
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO provenance
              (provenance_id, tenant_id, source_type, source_uri, source_digest,
               episode_id, derived_from, ingested_at, source_trust_tier, ingestion_agent_id)
            VALUES (%s, %s, %s::source_type, %s, %s, %s, '[]', %s, %s::trust_tier, %s)
            ON CONFLICT (tenant_id, provenance_id) DO NOTHING
            """,
            batch_provs,
        )

        cur.executemany(
            """
            INSERT INTO belief
              (tenant_id, belief_id, subject, predicate, object, confidence,
               valid_from, status, author_agent_id, provenance_id, sensitivity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::belief_status, %s, %s, %s::sensitivity)
            ON CONFLICT (tenant_id, belief_id) DO NOTHING
            """,
            batch_beliefs,
        )

    conn.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the PQBS demo tenant with Northwind Logistics data."
    )
    parser.add_argument(
        "--tenant",
        default="northwind",
        help="Tenant name (informational only; UUID comes from TENANT_ID_DEMO in .env)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all existing data for the tenant before seeding (DANGEROUS)",
    )
    args = parser.parse_args()

    url = os.environ.get("COCKROACH_URL")
    if not url:
        print("ERROR: COCKROACH_URL not set in environment / .env", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to CockroachDB...")
    print(f"Tenant: {args.tenant} ({TENANT_ID_DEMO})")

    conn = psycopg.connect(url, autocommit=False, row_factory=dict_row)

    try:
        if args.reset:
            reset_tenant(conn, TENANT_ID_DEMO)

        seed_predicate_policies(conn, TENANT_ID_DEMO)
        seed_agent_identities(conn, TENANT_ID_DEMO)
        belief_count, prov_count = seed_beliefs(conn, TENANT_ID_DEMO)

        # Final verification query
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM belief WHERE tenant_id = %s",
            (str(TENANT_ID_DEMO),),
        ).fetchone()
        total_beliefs = row["cnt"] if row else 0

        prov_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM provenance WHERE tenant_id = %s",
            (str(TENANT_ID_DEMO),),
        ).fetchone()
        total_provs = prov_row["cnt"] if prov_row else 0

        print()
        print("=" * 60)
        print("Seed complete.")
        print(f"  Beliefs in DB  : {total_beliefs}")
        print(f"  Provenance rows: {total_provs}")
        print(f"  Session seeded : {belief_count} beliefs, {prov_count} provenance records")
        print("=" * 60)

        if total_beliefs < 2000:
            print(f"WARNING: Expected 2000+ beliefs, got {total_beliefs}", file=sys.stderr)
            sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
