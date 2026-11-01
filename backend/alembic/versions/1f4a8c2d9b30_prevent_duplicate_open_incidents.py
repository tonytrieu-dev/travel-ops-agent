"""prevent duplicate open security incidents"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1f4a8c2d9b30"
down_revision: str | None = "7c2d4e6f8a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("""
        WITH duplicates AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY tenant_id, device_id
                       ORDER BY created_at, id
                   ) AS duplicate_number
            FROM security_incident
            WHERE status = 'open'
        )
        UPDATE security_incident
        SET status = 'duplicate'
        FROM duplicates
        WHERE security_incident.id = duplicates.id
          AND duplicates.duplicate_number > 1
    """))
    op.create_index(
        "uq_security_incident_open_tenant_device",
        "security_incident",
        ["tenant_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_security_incident_open_tenant_device",
        table_name="security_incident",
    )
