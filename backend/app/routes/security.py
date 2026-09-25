"""Small security-operations surface for the local enterprise demo."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.db import get_session
from app.dependencies import SecurityContext, get_security_context
from app.models import SecurityEvent, SecurityIncident, SecuritySession, User, utcnow
from app.schemas import SecurityEventOut, SecurityEventQuery, SecurityIncidentOut
from app.security import authorize, enforce_api_segment

router = APIRouter(
    prefix="/api/security",
    tags=["security"],
    dependencies=[Depends(enforce_api_segment)],
)


@router.get("/events", response_model=list[SecurityEventOut])
async def list_security_events(
    request: Request,
    query: SecurityEventQuery = Depends(),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> list[SecurityEventOut]:
    await authorize(context, session, action="security.read", resource="security-events", request=request, require_mfa=True)
    statement = select(SecurityEvent).where(col(SecurityEvent.tenant_id) == context.tenant_id)
    if query.actor_user_id is not None:
        statement = statement.where(col(SecurityEvent.actor_user_id) == query.actor_user_id)
    if query.decision is not None:
        statement = statement.where(col(SecurityEvent.decision) == query.decision)
    if query.action is not None:
        statement = statement.where(col(SecurityEvent.action) == query.action)
    if query.since is not None:
        statement = statement.where(col(SecurityEvent.created_at) >= query.since.replace(tzinfo=None))
    events = await session.scalars(statement.order_by(col(SecurityEvent.created_at).desc()).limit(200))
    return [SecurityEventOut.model_validate(event) for event in events]


@router.get("/incidents", response_model=list[SecurityIncidentOut])
async def list_incidents(
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> list[SecurityIncidentOut]:
    await authorize(context, session, action="security.read", resource="security-incidents", request=request, require_mfa=True)
    incidents = await session.scalars(
        select(SecurityIncident)
        .where(col(SecurityIncident.tenant_id) == context.tenant_id)
        .order_by(col(SecurityIncident.created_at).desc())
    )
    return [SecurityIncidentOut.model_validate(incident) for incident in incidents]


@router.post("/sessions/{session_id}/revoke", response_model=SecurityIncidentOut)
async def revoke_session(
    session_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> SecurityIncidentOut:
    await authorize(context, session, action="incident.respond", resource=f"session:{session_id}", request=request, require_mfa=True)
    target = await session.scalar(
        select(SecuritySession).where(
            col(SecuritySession.session_id) == session_id,
            col(SecuritySession.tenant_id) == context.tenant_id,
        )
    )
    if target is None:
        raise HTTPException(status_code=404, detail="session not found in caller tenant")
    target.revoked_at = utcnow()
    incident = SecurityIncident(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        device_id=target.device_id,
        session_id=session_id,
        reason="security operator revoked session",
        status="contained",
        contained_at=utcnow(),
    )
    session.add(incident)
    await session.commit()
    return SecurityIncidentOut.model_validate(incident)


@router.post("/users/{user_id}/disable", response_model=SecurityIncidentOut)
async def disable_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> SecurityIncidentOut:
    await authorize(context, session, action="incident.respond", resource=f"user:{user_id}", request=request, require_mfa=True)
    user = await session.scalar(
        select(User).where(col(User.id) == user_id, col(User.tenant_id) == context.tenant_id)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="user not found in caller tenant")
    user.disabled = True
    sessions = await session.scalars(
        select(SecuritySession).where(col(SecuritySession.user_id) == user_id)
    )
    for security_session in sessions:
        security_session.revoked_at = utcnow()
    incident = SecurityIncident(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        device_id=user.device_id,
        reason="security operator disabled identity",
        status="contained",
        contained_at=utcnow(),
    )
    session.add(incident)
    await session.commit()
    return SecurityIncidentOut.model_validate(incident)
