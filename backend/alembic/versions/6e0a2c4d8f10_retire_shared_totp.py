"""Retire the public shared authenticator and its sessions."""
from alembic import op

revision = "6e0a2c4d8f10"
down_revision = "5d9f1b3c4e67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""UPDATE security_session SET revoked_at = CURRENT_TIMESTAMP
        WHERE user_id IN (SELECT id FROM user_account WHERE totp_secret = 'JBSWY3DPEHPK3PXP')""")
    op.execute("UPDATE user_account SET totp_secret = '', last_totp_time_step = NULL WHERE totp_secret = 'JBSWY3DPEHPK3PXP'")
    op.execute("ALTER TABLE user_account ALTER COLUMN totp_secret SET DEFAULT ''")


def downgrade() -> None:
    # Retired public secrets must never be restored.
    pass
