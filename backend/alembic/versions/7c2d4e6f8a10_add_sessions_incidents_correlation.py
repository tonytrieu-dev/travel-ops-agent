"""add revocable sessions, incidents, and correlation ids"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c2d4e6f8a10"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_account", sa.Column("totp_secret", sa.String(), nullable=False, server_default="JBSWY3DPEHPK3PXP"))
    op.add_column("user_account", sa.Column("password_hash", sa.String(), nullable=False, server_default=""))
    op.add_column("booking_transition", sa.Column("correlation_id", sa.String(), nullable=False, server_default="migration"))
    op.add_column("execution_event", sa.Column("correlation_id", sa.String(), nullable=False, server_default="migration"))
    op.create_index("ix_booking_transition_correlation_id", "booking_transition", ["correlation_id"])
    op.create_index("ix_execution_event_correlation_id", "execution_event", ["correlation_id"])
    op.create_table(
        "security_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_account.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("mfa_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_security_session_session_id", "security_session", ["session_id"])
    op.create_index("ix_security_session_user_id", "security_session", ["user_id"])
    op.create_index("ix_security_session_tenant_id", "security_session", ["tenant_id"])
    op.create_index("ix_security_session_device_id", "security_session", ["device_id"])
    op.add_column("security_event", sa.Column("session_id", sa.String()))
    op.create_index("ix_security_event_session_id", "security_event", ["session_id"])
    op.create_table(
        "security_incident",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("user_account.id")),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String()),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("contained_at", sa.DateTime()),
    )
    op.create_index("ix_security_incident_tenant_id", "security_incident", ["tenant_id"])
    op.create_index("ix_security_incident_actor_user_id", "security_incident", ["actor_user_id"])
    op.create_index("ix_security_incident_device_id", "security_incident", ["device_id"])
    op.create_index("ix_security_incident_session_id", "security_incident", ["session_id"])


def downgrade() -> None:
    op.drop_table("security_incident")
    op.drop_index("ix_security_event_session_id", table_name="security_event")
    op.drop_column("security_event", "session_id")
    op.drop_table("security_session")
    op.drop_index("ix_execution_event_correlation_id", table_name="execution_event")
    op.drop_index("ix_booking_transition_correlation_id", table_name="booking_transition")
    op.drop_column("execution_event", "correlation_id")
    op.drop_column("booking_transition", "correlation_id")
    op.drop_column("user_account", "totp_secret")
    op.drop_column("user_account", "password_hash")
