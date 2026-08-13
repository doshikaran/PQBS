"""Create provenance table.

Revision ID: 0004_provenance
Revises: 0003_identity
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0004_provenance"
down_revision: Union[str, None] = "0003_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS provenance (
            provenance_id      UUID        NOT NULL DEFAULT gen_random_uuid(),
            tenant_id          UUID        NOT NULL,
            source_type        source_type NOT NULL,
            source_uri         TEXT,
            source_digest      CHAR(64)    NOT NULL,
            episode_id         UUID        NOT NULL,
            derived_from       JSONB       NOT NULL DEFAULT '[]',
            ingested_at        TIMESTAMPTZ NOT NULL,
            source_trust_tier  trust_tier  NOT NULL,
            ingestion_agent_id VARCHAR(256) NOT NULL,
            PRIMARY KEY (tenant_id, provenance_id)
        )
    """))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS provenance"))
