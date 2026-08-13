"""Create contradiction_event table.

Revision ID: 0008_contradiction
Revises: 0007_integrity
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0008_contradiction"
down_revision: Union[str, None] = "0007_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS contradiction_event (
            event_id              UUID             NOT NULL DEFAULT gen_random_uuid(),
            tenant_id             UUID             NOT NULL,
            incumbent_belief_id   UUID             NOT NULL,
            challenger_belief_id  UUID             NOT NULL,
            resolution            resolution       NOT NULL,
            resolution_basis      resolution_basis NOT NULL,
            retry_count           INT              NOT NULL DEFAULT 0
                CHECK (retry_count >= 0),
            detected_at           TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, event_id)
        )
    """))

    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_contradiction_incumbent "
        "ON contradiction_event (tenant_id, incumbent_belief_id)"
    ))

    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_contradiction_challenger "
        "ON contradiction_event (tenant_id, challenger_belief_id)"
    ))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP INDEX IF EXISTS idx_contradiction_challenger"))
    connection.execute(text("DROP INDEX IF EXISTS idx_contradiction_incumbent"))
    connection.execute(text("DROP TABLE IF EXISTS contradiction_event"))
