"""add historical security inputs and bounded control assessments"""

import sqlalchemy as sa
from alembic import op

revision = "2a6c8e0f1b34"
down_revision = "1f4a8c2d9b30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, column_type in (
        ("policy_version", sa.String()), ("enforcement_active", sa.Boolean()),
        ("actor_role", sa.String()), ("mfa_required", sa.Boolean()),
        ("mfa_verified", sa.Boolean()), ("related_event_ids", sa.JSON()),
    ):
        op.add_column("security_event", sa.Column(name, column_type, nullable=True))
    op.add_column("security_incident", sa.Column("trigger_event_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_incident_trigger_event", "security_incident", "security_event", ["trigger_event_id"], ["id"])
    op.create_table(
        "compliance_assessment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("evaluator_version", sa.String(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
    )
    op.create_index("ix_compliance_assessment_tenant_id", "compliance_assessment", ["tenant_id"])
    op.execute("""
        CREATE FUNCTION reject_monitoring_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'monitoring evidence is append-only';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER compliance_assessment_append_only
        BEFORE UPDATE OR DELETE ON compliance_assessment
        FOR EACH ROW EXECUTE FUNCTION reject_monitoring_mutation();
    """)
    for table in ("booking_transition", "execution_event", "compliance_assessment"):
        op.execute(f"""
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_monitoring_mutation();
        """)
        op.execute(f"REVOKE TRUNCATE ON {table} FROM PUBLIC")


def downgrade() -> None:
    for table in ("booking_transition", "execution_event"):
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
    op.drop_table("compliance_assessment")
    op.execute("DROP FUNCTION reject_monitoring_mutation()")
    op.drop_constraint("fk_incident_trigger_event", "security_incident", type_="foreignkey")
    op.drop_column("security_incident", "trigger_event_id")
    for name in ("policy_version", "enforcement_active", "actor_role", "mfa_required", "mfa_verified", "related_event_ids"):
        op.drop_column("security_event", name)
