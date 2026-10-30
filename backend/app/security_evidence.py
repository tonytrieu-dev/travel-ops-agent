"""Append-only security evidence, denial detection, and containment."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.db import get_session_factory
from app import security_posture as posture
from app.models import SecurityEvent, SecurityIncident, SecuritySession, utcnow
from app.request_context import correlation_id
from app.schemas import SecurityEventQuery

if TYPE_CHECKING:
    from app.security_identity import SecurityContext

DENIAL_THRESHOLD = 5
DENIAL_WINDOW_MINUTES = 5
SECURITY_POLICY_VERSION = "1"


async def record_security_event(
    session: AsyncSession,
    context: SecurityContext | None,
    *,
    action: str,
    resource: str,
    decision: str,
    reason: str,
    request: Request | None = None,
    tenant_id: str | None = None,
    actor_user_id: int | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    mfa_required: bool | None = None,
) -> SecurityEvent:
    event = SecurityEvent(
        tenant_id=tenant_id or (context.tenant_id if context else "unknown"),
        actor_user_id=actor_user_id if actor_user_id is not None else (context.user_id if context else None),
        device_id=device_id or (context.device_id if context else "unknown"),
        session_id=session_id or (context.session_id if context else None),
        source_segment="web",
        target_segment="api",
        action=action,
        resource=resource,
        decision=decision,
        reason=reason,
        correlation_id=correlation_id(),
        policy_version=SECURITY_POLICY_VERSION,
        enforcement_active=posture.enforcement_active(),
        actor_role=context.role if context else None,
        mfa_required=mfa_required,
        mfa_verified=context.mfa_verified if context else None,
    )
    if decision == "deny":
        # Serialize one denial window so concurrent transactions cannot each miss the threshold.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{event.tenant_id}\x1f{event.device_id}\x1f{event.session_id or ''}", 0)
                )
            )
        )
    session.add(event)
    await session.flush()
    if decision == "deny":
        since = utcnow() - timedelta(minutes=DENIAL_WINDOW_MINUTES)
        # A targeted account is not a verified source for an unauthenticated attempt.
        denied = list(await session.scalars(
            select(SecurityEvent).where(
                col(SecurityEvent.device_id) == event.device_id,
                col(SecurityEvent.tenant_id) == event.tenant_id,
                col(SecurityEvent.decision) == "deny",
                col(SecurityEvent.session_id) == event.session_id,
                col(SecurityEvent.created_at) >= since,
            )
        ))
        if len(denied) >= DENIAL_THRESHOLD:
            await session.execute(
                insert(SecurityIncident)
                .values(
                    tenant_id=event.tenant_id,
                    actor_user_id=event.actor_user_id,
                    device_id=event.device_id,
                    session_id=event.session_id,
                    trigger_event_id=event.id,
                    reason=(
                        f"{DENIAL_THRESHOLD} denied requests in "
                        f"{DENIAL_WINDOW_MINUTES} minutes"
                    ),
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "device_id", "session_id"] if event.session_id else ["tenant_id", "device_id"],
                    index_where=text("status = 'open' AND session_id IS NOT NULL" if event.session_id else "status = 'open' AND session_id IS NULL"),
                )
            )
            if event.session_id:
                security_session = await session.scalar(
                    select(SecuritySession).where(
                        col(SecuritySession.session_id) == event.session_id
                    )
                )
                if security_session is not None:
                    security_session.revoked_at = utcnow()
                    session.add(SecurityEvent(
                        tenant_id=event.tenant_id, actor_user_id=event.actor_user_id,
                        session_id=event.session_id, device_id=event.device_id,
                        source_segment="api", target_segment="api", action="session.revoke",
                        resource=f"session:{event.session_id}", decision="allow",
                        reason="denial threshold containment", correlation_id=event.correlation_id,
                        policy_version=SECURITY_POLICY_VERSION,
                        enforcement_active=posture.enforcement_active(),
                        related_event_ids=[denial.id for denial in denied if denial.id is not None],
                        created_at=security_session.revoked_at,
                    ))
    await session.commit()
    return event


async def record_authentication_failure(
    *,
    reason: str,
    tenant_id: str | None = None,
    resource: str = "identity",
    device_id: str | None = None,
    session_id: str | None = None,
    request: Request | None = None,
) -> None:
    """Write auth denials outside the request transaction that failed authentication."""
    async with get_session_factory()() as audit_session:
        await record_security_event(
            audit_session, None, action="auth.authenticate", resource=resource,
            decision="deny", reason=reason, request=request, tenant_id=tenant_id,
            device_id=device_id, session_id=session_id,
        )


async def get_security_events(
    session: AsyncSession, tenant_id: str, query: SecurityEventQuery
) -> list[SecurityEvent]:
    statement = select(SecurityEvent).where(col(SecurityEvent.tenant_id) == tenant_id)
    if query.actor_user_id is not None:
        statement = statement.where(col(SecurityEvent.actor_user_id) == query.actor_user_id)
    if query.decision is not None:
        statement = statement.where(col(SecurityEvent.decision) == query.decision)
    if query.action is not None:
        statement = statement.where(col(SecurityEvent.action) == query.action)
    if query.since is not None:
        since = (
            query.since.replace(tzinfo=UTC)
            if query.since.tzinfo is None
            else query.since.astimezone(UTC)
        )
        statement = statement.where(col(SecurityEvent.created_at) >= since.replace(tzinfo=None))
    return list(await session.scalars(
        statement.order_by(col(SecurityEvent.created_at).desc()).limit(200)
    ))


async def get_security_event(session: AsyncSession, tenant_id: str, event_id: int) -> SecurityEvent | None:
    return await session.scalar(select(SecurityEvent).where(
        col(SecurityEvent.id) == event_id, col(SecurityEvent.tenant_id) == tenant_id,
    ))


async def get_security_incidents(
    session: AsyncSession, tenant_id: str
) -> list[SecurityIncident]:
    return list(await session.scalars(
        select(SecurityIncident)
        .where(col(SecurityIncident.tenant_id) == tenant_id)
        .order_by(col(SecurityIncident.created_at).desc())
    ))
