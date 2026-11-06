"""Persist the last consumed TOTP time step per identity."""
import sqlalchemy as sa
from alembic import op

revision = "4c8e0a2b3d56"
down_revision = "3b7d9f1a2c45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_account", sa.Column("last_totp_time_step", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_account", "last_totp_time_step")
