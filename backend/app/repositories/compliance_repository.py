"""Reassess committed evidence in one snapshot; never consume the capped events API."""

from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.compliance import evaluate_audit_protections, evaluate_containment, evaluate_mfa
from app.models import ComplianceAssessment, SecurityEvent, User, utcnow

EVALUATOR_VERSION = "3"


async def assess_security_controls(session: AsyncSession) -> int:
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    acquired = await session.scalar(text("SELECT pg_try_advisory_xact_lock(272202611)"))
    if not acquired:
        return 0
    window_end = utcnow()
    window_start = window_end - timedelta(hours=24)
    protections = [{str(key): value for key, value in row.items()} for row in (await session.execute(text("""
        SELECT relation.relname AS table_name,
            coalesce(bool_or(trigger.tgtype = 27 AND trigger.tgenabled IN ('O', 'A') AND trigger.tgqual IS NULL
                AND procedure.proname IN ('reject_security_event_mutation',
                    'reject_audit_row_mutation', 'reject_monitoring_mutation')), false) AS mutation_blocked,
            coalesce(bool_or(trigger.tgtype = 34 AND trigger.tgenabled IN ('O', 'A') AND trigger.tgqual IS NULL
                AND procedure.proname IN ('reject_security_event_mutation',
                    'reject_monitoring_mutation')), false) AS truncate_blocked,
            (role.rolsuper OR role.rolreplication
                OR pg_has_role(current_user, relation.relowner, 'MEMBER')) AS can_bypass
        FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles role ON role.rolname = current_user
        LEFT JOIN pg_trigger trigger ON trigger.tgrelid = relation.oid AND NOT trigger.tgisinternal
        LEFT JOIN pg_proc procedure ON procedure.oid = trigger.tgfoid
        WHERE namespace.nspname = current_schema()
            AND relation.relname IN ('security_event', 'booking_transition',
                'execution_event', 'compliance_assessment')
        GROUP BY relation.relname, relation.relowner, role.rolsuper, role.rolreplication
        ORDER BY relation.relname
    """))).mappings()]
    tenants = list(await session.scalars(select(col(User.tenant_id)).distinct()))
    # shortcut: full-window reads fit the local demo; use durable ingestion when volume outgrows memory.
    for tenant_id in tenants:
        events = list(await session.scalars(select(SecurityEvent).where(
            col(SecurityEvent.tenant_id) == tenant_id,
            col(SecurityEvent.created_at) >= window_start - timedelta(minutes=5),
            col(SecurityEvent.created_at) <= window_end,
        )))
        session_ids = {event.session_id for event in events if event.session_id}
        successes = list(await session.scalars(select(SecurityEvent).where(
            col(SecurityEvent.tenant_id) == tenant_id,
            col(SecurityEvent.action) == "auth.mfa.verify",
            col(SecurityEvent.session_id).in_(session_ids),
            col(SecurityEvent.created_at) <= window_end,
        ))) if session_ids else []
        results = [
            evaluate_mfa([event for event in events if event.created_at >= window_start], successes),
            evaluate_audit_protections(protections),
            evaluate_containment(events, window_end, window_start),
        ]
        session.add(ComplianceAssessment(
            tenant_id=tenant_id, evaluator_version=EVALUATOR_VERSION,
            window_start=window_start, window_end=window_end,
            results=[result.model_dump() for result in results],
        ))
    await session.commit()
    return len(tenants)


async def get_assessments(session: AsyncSession, tenant_id: str) -> list[ComplianceAssessment]:
    return list(await session.scalars(select(ComplianceAssessment).where(
        col(ComplianceAssessment.tenant_id) == tenant_id,
    ).order_by(col(ComplianceAssessment.assessed_at).desc(), col(ComplianceAssessment.id).desc()).limit(10)))
