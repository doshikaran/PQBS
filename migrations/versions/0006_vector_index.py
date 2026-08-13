"""Create HNSW vector index on belief(tenant_id, embedding).

Revision ID: 0006_vector_index
Revises: 0005_belief
Create Date: 2026-08-13

CockroachDB-specific: uses CREATE VECTOR INDEX syntax (NOT pgvector hnsw_l2_ops).
The index is prefixed by tenant_id for multi-tenant performance isolation.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0006_vector_index"
down_revision: Union[str, None] = "0005_belief"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # CockroachDB HNSW vector index syntax — confirmed by V1 spike.
    # The vector index is created separately from the table.
    # Only rows where embedding IS NOT NULL will be indexed.
    connection.execute(text(
        "CREATE VECTOR INDEX IF NOT EXISTS idx_belief_vector "
        "ON belief (tenant_id, embedding)"
    ))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP INDEX IF EXISTS idx_belief_vector"))
