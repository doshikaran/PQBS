"""Create predicate_policy table.

Revision ID: 0002_policy
Revises: 0001_enums
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0002_policy"
down_revision: Union[str, None] = "0001_enums"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS predicate_policy (
            tenant_id          UUID         NOT NULL,
            predicate          VARCHAR(256) NOT NULL,
            cardinality        cardinality  NOT NULL DEFAULT 'single_valued',
            resolution_strategy resolution_strategy NOT NULL DEFAULT 'recency',
            min_source_tier    trust_tier   NOT NULL DEFAULT 'unverified',
            is_sensitive       BOOLEAN      NOT NULL DEFAULT FALSE,
            normalization_rule VARCHAR(256) NOT NULL DEFAULT 'none',
            expected_value_domain JSONB,
            PRIMARY KEY (tenant_id, predicate)
        )
    """))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS predicate_policy"))
