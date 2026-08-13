"""Create working_memory table with row-level TTL.

Revision ID: 0010_working_memory
Revises: 0009_retrieval_log
Create Date: 2026-08-13

CockroachDB-specific: uses ttl_expiration_expression table option.
Confirmed by V6 spike. The expires_at column is a TIMESTAMPTZ.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0010_working_memory"
down_revision: Union[str, None] = "0009_retrieval_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # CockroachDB row-level TTL via ttl_expiration_expression table storage param.
    # The TTL job runs every minute and deletes rows where expires_at <= now().
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS working_memory (
            tenant_id   UUID         NOT NULL,
            session_id  UUID         NOT NULL,
            entry_id    UUID         NOT NULL DEFAULT gen_random_uuid(),
            agent_id    VARCHAR(256) NOT NULL,
            content     TEXT         NOT NULL,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ  NOT NULL,
            PRIMARY KEY (tenant_id, session_id, entry_id)
        ) WITH (
            ttl_expiration_expression = 'expires_at',
            ttl_job_cron = '* * * * *'
        )
    """))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS working_memory"))
