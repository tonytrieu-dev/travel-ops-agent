"""add zero-trust identity, monitoring, and incident-response tables

Revision ID: f1a2b3c4d5e6
Revises: c2f4a8e9d103
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e2fd12c1788c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_account", sa.Column("tenant_id", sa.String(), nullable=False, server_default="demo-tenant"))
    op.add_column("user_account", sa.Column("role", sa.String(), nullable=False, server_default="traveler"))
    op.add_column("user_account", sa.Column("device_id", sa.String(), nullable=False, server_default="demo-device"))
    op.add_column("user_account", sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("user_account", sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_user_account_tenant_id", "user_account", ["tenant_id"])

    op.create_table(
        "security_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("source_segment", sa.String(), nullable=False),
        sa.Column("target_segment", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user_account.id"]),
    )
    op.create_index("ix_security_event_tenant_id", "security_event", ["tenant_id"])
    op.create_index("ix_security_event_actor_user_id", "security_event", ["actor_user_id"])
    op.create_index("ix_security_event_decision", "security_event", ["decision"])
    op.create_index("ix_security_event_correlation_id", "security_event", ["correlation_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_security_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'security events are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER security_event_append_only
        BEFORE UPDATE OR DELETE ON security_event
        FOR EACH ROW EXECUTE FUNCTION reject_security_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS security_event_append_only ON security_event")
    op.execute("DROP FUNCTION IF EXISTS reject_security_event_mutation()")
    op.drop_index("ix_security_event_correlation_id", table_name="security_event")
    op.drop_index("ix_security_event_decision", table_name="security_event")
    op.drop_index("ix_security_event_actor_user_id", table_name="security_event")
    op.drop_index("ix_security_event_tenant_id", table_name="security_event")
    op.drop_table("security_event")
    op.drop_index("ix_user_account_tenant_id", table_name="user_account")
    for column in ("disabled", "mfa_enabled", "device_id", "role", "tenant_id"):
        op.drop_column("user_account", column)
