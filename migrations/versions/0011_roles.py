"""Create 5 DB roles and grant appropriate permissions.

Revision ID: 0011_roles
Revises: 0010_working_memory
Create Date: 2026-08-13

Roles:
  role_producer  — write beliefs and provenance; read policies and agent identities
  role_semantics — read/update beliefs; write contradiction events; read policies
  role_integrity — read everything relevant; write verdicts and quarantine; update belief status
  role_consumer  — SELECT on v_trusted_current view ONLY
  role_auditor   — SELECT on all tables
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0011_roles"
down_revision: Union[str, None] = "0010_working_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # Create roles (idempotent)
    for role in ["role_producer", "role_semantics", "role_integrity", "role_consumer", "role_auditor"]:
        connection.execute(text(f"CREATE ROLE IF NOT EXISTS {role}"))

    # ---------------------------------------------------------------------------
    # role_producer: write beliefs and provenance; read policies and agent identities
    # ---------------------------------------------------------------------------
    connection.execute(text("GRANT INSERT ON TABLE belief TO role_producer"))
    connection.execute(text("GRANT INSERT ON TABLE provenance TO role_producer"))
    connection.execute(text("GRANT SELECT ON TABLE predicate_policy TO role_producer"))
    connection.execute(text("GRANT SELECT ON TABLE agent_identity TO role_producer"))

    # ---------------------------------------------------------------------------
    # role_semantics: read/update beliefs; write contradiction events; read policies
    # CockroachDB does not support column-level UPDATE grants, so grant full UPDATE.
    # ---------------------------------------------------------------------------
    connection.execute(text("GRANT SELECT ON TABLE belief TO role_semantics"))
    connection.execute(text("GRANT UPDATE ON TABLE belief TO role_semantics"))
    connection.execute(text("GRANT INSERT ON TABLE contradiction_event TO role_semantics"))
    connection.execute(text("GRANT SELECT ON TABLE predicate_policy TO role_semantics"))

    # ---------------------------------------------------------------------------
    # role_integrity: read all relevant tables; write verdicts and quarantine;
    #                 update belief (for status transitions)
    # ---------------------------------------------------------------------------
    connection.execute(text("GRANT SELECT ON TABLE belief TO role_integrity"))
    connection.execute(text("GRANT SELECT ON TABLE provenance TO role_integrity"))
    connection.execute(text("GRANT SELECT ON TABLE agent_identity TO role_integrity"))
    connection.execute(text("GRANT SELECT ON TABLE integrity_verdict TO role_integrity"))
    connection.execute(text("GRANT SELECT ON TABLE quarantine TO role_integrity"))
    connection.execute(text("GRANT INSERT ON TABLE integrity_verdict TO role_integrity"))
    connection.execute(text("GRANT INSERT ON TABLE quarantine TO role_integrity"))
    connection.execute(text("GRANT UPDATE ON TABLE belief TO role_integrity"))

    # ---------------------------------------------------------------------------
    # role_auditor: read everything
    # ---------------------------------------------------------------------------
    for table in [
        "belief", "provenance", "predicate_policy", "agent_identity",
        "integrity_verdict", "quarantine", "contradiction_event",
        "retrieval_log", "working_memory",
    ]:
        connection.execute(text(f"GRANT SELECT ON TABLE {table} TO role_auditor"))

    # role_consumer grants are done in 0012_views after view creation


def downgrade() -> None:
    connection = op.get_bind()

    # Revoke all grants
    for table in [
        "belief", "provenance", "predicate_policy", "agent_identity",
        "integrity_verdict", "quarantine", "contradiction_event",
        "retrieval_log", "working_memory",
    ]:
        for role in ["role_producer", "role_semantics", "role_integrity", "role_consumer", "role_auditor"]:
            connection.execute(text(f"REVOKE ALL ON TABLE {table} FROM {role}"))

    # Drop roles
    for role in ["role_producer", "role_semantics", "role_integrity", "role_consumer", "role_auditor"]:
        connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
