"""Create agent_identity table.

Revision ID: 0003_identity
Revises: 0002_policy
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0003_identity"
down_revision: Union[str, None] = "0002_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_identity (
            agent_id           VARCHAR(256) NOT NULL,
            tenant_id          UUID         NOT NULL,
            agent_class        agent_class  NOT NULL,
            db_role            VARCHAR(128) NOT NULL,
            credential_ref     TEXT         NOT NULL,
            registered_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            behavior_baseline  JSONB        NOT NULL DEFAULT '{}',
            trust_multiplier   FLOAT        NOT NULL DEFAULT 1.0
                CHECK (trust_multiplier >= 0 AND trust_multiplier <= 10),
            status             agent_status NOT NULL DEFAULT 'active',
            PRIMARY KEY (tenant_id, agent_id)
        )
    """))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS agent_identity"))
