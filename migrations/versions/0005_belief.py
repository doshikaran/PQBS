"""Create belief table with PK, FK, and check constraints.

Revision ID: 0005_belief
Revises: 0004_provenance
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0005_belief"
down_revision: Union[str, None] = "0004_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS belief (
            tenant_id         UUID          NOT NULL,
            belief_id         UUID          NOT NULL DEFAULT gen_random_uuid(),
            subject           VARCHAR(1024) NOT NULL,
            predicate         VARCHAR(256)  NOT NULL,
            object            TEXT          NOT NULL,
            object_normalized TEXT,
            embedding         VECTOR(1024),
            confidence        FLOAT         NOT NULL
                CHECK (confidence >= 0 AND confidence <= 1),
            valid_from        TIMESTAMPTZ   NOT NULL,
            valid_to          TIMESTAMPTZ,
            tx_from           TIMESTAMPTZ   NOT NULL DEFAULT now(),
            tx_to             TIMESTAMPTZ,
            status            belief_status NOT NULL DEFAULT 'pending',
            supersedes        UUID,
            superseded_by     UUID,
            author_agent_id   VARCHAR(256)  NOT NULL,
            provenance_id     UUID          NOT NULL,
            trust_score       FLOAT
                CHECK (trust_score IS NULL OR (trust_score >= 0 AND trust_score <= 1)),
            screened_at       TIMESTAMPTZ,
            sensitivity       sensitivity   NOT NULL DEFAULT 'normal',
            PRIMARY KEY (tenant_id, belief_id),
            CONSTRAINT fk_belief_provenance
                FOREIGN KEY (tenant_id, provenance_id)
                REFERENCES provenance (tenant_id, provenance_id),
            CONSTRAINT chk_status_valid
                CHECK (status IN ('pending','trusted','quarantined','inconclusive','superseded','rejected')),
            CONSTRAINT chk_superseded_by_requires_superseded_status
                CHECK ((superseded_by IS NULL) OR (status = 'superseded')),
            CONSTRAINT chk_trust_score_screened_at_consistency
                CHECK ((trust_score IS NULL) = (screened_at IS NULL))
        )
    """))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP TABLE IF EXISTS belief"))
