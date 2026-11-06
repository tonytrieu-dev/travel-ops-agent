"""Scope identified incidents to sessions, preserving sessionless device grouping."""

import sqlalchemy as sa
from alembic import op

revision = "3b7d9f1a2c45"
down_revision = "2a6c8e0f1b34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_security_incident_open_tenant_device", table_name="security_incident")
    op.create_index(
        "uq_security_incident_open_session", "security_incident",
        ["tenant_id", "device_id", "session_id"], unique=True,
        postgresql_where=sa.text("status = 'open' AND session_id IS NOT NULL"),
    )
    op.create_index(
        "uq_security_incident_open_sessionless", "security_incident",
        ["tenant_id", "device_id"], unique=True,
        postgresql_where=sa.text("status = 'open' AND session_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_security_incident_open_session", table_name="security_incident")
    op.drop_index("uq_security_incident_open_sessionless", table_name="security_incident")
    op.execute(sa.text("""
        WITH duplicates AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, device_id ORDER BY created_at, id
            ) AS duplicate_number
            FROM security_incident WHERE status = 'open'
        )
        UPDATE security_incident SET status = 'duplicate'
        FROM duplicates WHERE security_incident.id = duplicates.id
            AND duplicates.duplicate_number > 1
    """))
    op.create_index(
        "uq_security_incident_open_tenant_device", "security_incident",
        ["tenant_id", "device_id"], unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
