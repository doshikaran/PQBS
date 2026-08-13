"""Create all PG enum types.

Revision ID: 0001_enums
Revises:
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0001_enums"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS belief_status AS ENUM ("
        "  'pending', 'trusted', 'quarantined', 'inconclusive', 'superseded', 'rejected'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS source_type AS ENUM ("
        "  'user_statement', 'document', 'tool_result', 'web_content',"
        "  'agent_inference', 'system_of_record'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS trust_tier AS ENUM ("
        "  'authoritative', 'corroborated', 'unverified', 'untrusted'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS reason_code AS ENUM ("
        "  'anomalous_embedding', 'untrusted_source', 'imperative_content',"
        "  'contradiction_burst', 'identity_anomaly', 'derived_from_quarantined',"
        "  'temporal_implausible', 'manual'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS resolution AS ENUM ("
        "  'challenger_supersedes', 'incumbent_retained', 'both_retained', 'deferred'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS resolution_basis AS ENUM ("
        "  'recency', 'confidence', 'source_tier', 'explicit_invalidation', 'policy'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS cardinality AS ENUM ("
        "  'single_valued', 'multi_valued', 'temporal_sequence'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS disposition AS ENUM ("
        "  'held', 'released', 'rejected'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS agent_class AS ENUM ("
        "  'producer', 'integrity', 'semantics', 'consumer', 'platform'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS agent_status AS ENUM ("
        "  'active', 'suspended', 'revoked'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS verdict_value AS ENUM ("
        "  'trusted', 'quarantined', 'inconclusive'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS sensitivity AS ENUM ("
        "  'normal', 'elevated'"
        ")"
    ))

    connection.execute(text(
        "CREATE TYPE IF NOT EXISTS resolution_strategy AS ENUM ("
        "  'recency', 'confidence', 'source_tier', 'explicit_invalidation', 'policy'"
        ")"
    ))


def downgrade() -> None:
    connection = op.get_bind()

    for type_name in [
        "resolution_strategy",
        "sensitivity",
        "verdict_value",
        "agent_status",
        "agent_class",
        "disposition",
        "cardinality",
        "resolution_basis",
        "resolution",
        "reason_code",
        "trust_tier",
        "source_type",
        "belief_status",
    ]:
        connection.execute(text(f"DROP TYPE IF EXISTS {type_name}"))
