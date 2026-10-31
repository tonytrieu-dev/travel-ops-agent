"""Retire fixed-salt password hashes and their active sessions."""
from alembic import op
import sqlalchemy as sa

revision = "5d9f1b3c4e67"
down_revision = "4c8e0a2b3d56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE security_session SET revoked_at = CURRENT_TIMESTAMP
        WHERE revoked_at IS NULL AND user_id IN (
            SELECT id FROM user_account WHERE password_hash <> '' AND password_hash NOT LIKE 'pbkdf2_sha256$%'
        )
    """))
    op.execute(sa.text("""
        UPDATE user_account SET password_hash = ''
        WHERE password_hash <> '' AND password_hash NOT LIKE 'pbkdf2_sha256$%'
    """))


def downgrade() -> None:
    # Retired hashes cannot be recovered; accounts must enroll a new password.
    pass
