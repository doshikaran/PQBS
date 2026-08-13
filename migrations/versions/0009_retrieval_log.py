"""Create retrieval_log table.

Revision ID: 0009_retrieval_log
Revises: 0008_contradiction
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0009_retrieval_log"
down_revision: Union[str, None] = "0008_contradiction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS retrieval_log (
            retrieval_id         UUID         NOT NULL DEFAULT gen_random_uuid(),
            tenant_id            UUID         NOT NULL,
            requesting_agent_id  VARCHAR(256) NOT NULL,
            query_digest         CHAR(64)     NOT NULL,
            returned_belief_ids  JSONB        NOT NULL DEFAULT '[]',
            retrieved_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, retrieval_id)
        )
    """))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS retrieval_log"))
