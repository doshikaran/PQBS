"""Create integrity_verdict and quarantine tables.

Revision ID: 0007_integrity
Revises: 0006_vector_index
Create Date: 2026-08-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0007_integrity"
down_revision: Union[str, None] = "0006_vector_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # integrity_verdict — append-only; INSERT-only by role_integrity
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS integrity_verdict (
            verdict_id        UUID          NOT NULL DEFAULT gen_random_uuid(),
            belief_id         UUID          NOT NULL,
            tenant_id         UUID          NOT NULL,
            verdict           verdict_value NOT NULL,
            trust_score       FLOAT         NOT NULL
                CHECK (trust_score >= 0 AND trust_score <= 1),
            signal_scores     JSONB         NOT NULL,
            triggering_rule   VARCHAR(256),
            screened_at       TIMESTAMPTZ   NOT NULL,
            screener_version  VARCHAR(64)   NOT NULL,
            re_screen_reason  TEXT,
            latency_ms        INT           NOT NULL
                CHECK (latency_ms >= 0),
            PRIMARY KEY (tenant_id, verdict_id)
        )
    """))

    # Index: efficient lookup of verdicts for a given belief
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_integrity_verdict_belief "
        "ON integrity_verdict (tenant_id, belief_id, screened_at DESC)"
    ))

    # quarantine — tracks quarantined beliefs and their review disposition
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS quarantine (
            quarantine_id  UUID         NOT NULL DEFAULT gen_random_uuid(),
            belief_id      UUID         NOT NULL,
            tenant_id      UUID         NOT NULL,
            reason_code    reason_code  NOT NULL,
            quarantined_at TIMESTAMPTZ  NOT NULL,
            disposition    disposition  NOT NULL DEFAULT 'held',
            reviewed_by    VARCHAR(256),
            review_notes   TEXT,
            reviewed_at    TIMESTAMPTZ,
            PRIMARY KEY (tenant_id, quarantine_id)
        )
    """))

    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_belief "
        "ON quarantine (tenant_id, belief_id)"
    ))

    # Partial index: fast lookup of all 'held' quarantine records
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_held "
        "ON quarantine (tenant_id, disposition) WHERE disposition = 'held'"
    ))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP INDEX IF EXISTS idx_quarantine_held"))
    connection.execute(text("DROP INDEX IF EXISTS idx_quarantine_belief"))
    connection.execute(text("DROP TABLE IF EXISTS quarantine"))
    connection.execute(text("DROP INDEX IF EXISTS idx_integrity_verdict_belief"))
    connection.execute(text("DROP TABLE IF EXISTS integrity_verdict"))
