"""Create role-scoped views enforcing status filtering.

Revision ID: 0012_views
Revises: 0011_roles
Create Date: 2026-08-13

Views:
  v_trusted_current  — role_consumer: only trusted, current (tx_to IS NULL) beliefs
  v_pending_beliefs  — role_producer read-back: pending beliefs only
  v_trusted_full     — role_semantics: all trusted beliefs (all columns)
  v_all_beliefs      — role_integrity, role_auditor: all beliefs
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0012_views"
down_revision: Union[str, None] = "0011_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # v_trusted_current — the ONLY view role_consumer can access.
    # Enforces Security Invariant #2: no consumer-role query can return
    # quarantined or pending beliefs.
    # embedding is included so the recall path can ORDER BY embedding <-> qvec
    # after the two-phase CTE narrows candidates via idx_belief_vector.
    connection.execute(text("""
        CREATE VIEW IF NOT EXISTS v_trusted_current AS
        SELECT
            belief_id,
            tenant_id,
            subject,
            predicate,
            object,
            object_normalized,
            confidence,
            valid_from,
            valid_to,
            tx_from,
            author_agent_id,
            provenance_id,
            trust_score,
            screened_at,
            sensitivity,
            embedding
        FROM belief
        WHERE status = 'trusted' AND tx_to IS NULL
    """))

    # v_pending_beliefs — for role_producer to read back its own pending writes
    connection.execute(text("""
        CREATE VIEW IF NOT EXISTS v_pending_beliefs AS
        SELECT * FROM belief WHERE status = 'pending'
    """))

    # v_trusted_full — for role_semantics: all trusted beliefs with full columns
    connection.execute(text("""
        CREATE VIEW IF NOT EXISTS v_trusted_full AS
        SELECT * FROM belief WHERE status = 'trusted'
    """))

    # v_all_beliefs — for role_integrity and role_auditor: unrestricted view
    connection.execute(text("""
        CREATE VIEW IF NOT EXISTS v_all_beliefs AS
        SELECT * FROM belief
    """))

    # Grant view access to roles
    # role_consumer: ONLY v_trusted_current
    connection.execute(text("GRANT SELECT ON v_trusted_current TO role_consumer"))

    # role_producer: v_pending_beliefs (read-back)
    connection.execute(text("GRANT SELECT ON v_pending_beliefs TO role_producer"))

    # role_semantics: v_trusted_full
    connection.execute(text("GRANT SELECT ON v_trusted_full TO role_semantics"))

    # role_integrity: v_all_beliefs
    connection.execute(text("GRANT SELECT ON v_all_beliefs TO role_integrity"))

    # role_auditor: all views
    connection.execute(text("GRANT SELECT ON v_trusted_current TO role_auditor"))
    connection.execute(text("GRANT SELECT ON v_pending_beliefs TO role_auditor"))
    connection.execute(text("GRANT SELECT ON v_trusted_full TO role_auditor"))
    connection.execute(text("GRANT SELECT ON v_all_beliefs TO role_auditor"))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("DROP VIEW IF EXISTS v_all_beliefs"))
    connection.execute(text("DROP VIEW IF EXISTS v_trusted_full"))
    connection.execute(text("DROP VIEW IF EXISTS v_pending_beliefs"))
    connection.execute(text("DROP VIEW IF EXISTS v_trusted_current"))
