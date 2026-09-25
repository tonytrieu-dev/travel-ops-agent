"""Reusable policy, segment, audit, and containment controls."""

from datetime import timedelta
from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.db import get_session, get_session_factory
from app.config import get_settings
from app.dependencies import SecurityContext, get_security_context
from app.models import SecurityEvent, SecurityIncident, SecuritySession, TripRequest, User, HITLBookingLog, utcnow
from app.request_context import correlation_id

DENIAL_THRESHOLD = 5
DENIAL_WINDOW_MINUTES = 5
_ROLE_ACTIONS = {
    "traveler": {"trip.read", "trip.create", "trip.plan", "flight.search", "booking.request", "booking.approve", "booking.execute"},
    "trip-planner": {"trip.read", "trip.create", "trip.plan", "flight.search"},
    "security-operator": {"security.read", "incident.respond", "connector.configure"},
    "admin": {"trip.read", "trip.create", "trip.plan", "flight.search", "booking.request", "booking.approve", "booking.execute", "security.read", "incident.respond", "connector.configure"},
}


class SecurityError(Exception):
    def __init__(self, detail: str, status_code: int = 403) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


async def record_security_event(
    session: AsyncSession,
    context: SecurityContext | None,
    *,
    action: str,
    resource: str,
    decision: str,
    reason: str,
    request: Request | None = None,
    source_segment: str | None = None,
    tenant_id: str | None = None,
    actor_user_id: int | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
) -> SecurityEvent:
    event = SecurityEvent(
        tenant_id=tenant_id or (context.tenant_id if context else "unknown"),
        actor_user_id=actor_user_id if actor_user_id is not None else (context.user_id if context else None),
        device_id=device_id or (context.device_id if context else "unknown"),
        session_id=session_id or (context.session_id if context else None),
        source_segment=source_segment or "web",
        target_segment="api",
        action=action,
        resource=resource,
        decision=decision,
        reason=reason,
        correlation_id=(getattr(request.state, "correlation_id", None) if request else None) or correlation_id(),
    )
    session.add(event)
    await session.flush()
    if decision == "deny":
        since = utcnow() - timedelta(minutes=DENIAL_WINDOW_MINUTES)
        denied = await session.scalar(
            select(func.count(col(SecurityEvent.id))).where(
                col(SecurityEvent.device_id) == event.device_id,
                col(SecurityEvent.decision) == "deny",
                col(SecurityEvent.created_at) >= since,
            )
        )
        if denied and denied >= DENIAL_THRESHOLD:
            existing = await session.scalar(
                select(SecurityIncident).where(
                    col(SecurityIncident.device_id) == event.device_id,
                    col(SecurityIncident.status) == "open",
                )
            )
            if existing is None:
                session.add(
                    SecurityIncident(
                        tenant_id=event.tenant_id,
                        actor_user_id=event.actor_user_id,
                        device_id=event.device_id,
                        session_id=event.session_id,
                        reason=f"{DENIAL_THRESHOLD} denied requests in {DENIAL_WINDOW_MINUTES} minutes",
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
    await session.commit()
    return event


async def record_authentication_failure(
    *,
    reason: str,
    tenant_id: str | None = None,
    actor_user_id: int | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    request: Request | None = None,
) -> None:
    """Write auth denials outside the request transaction that failed authentication."""
    async with get_session_factory()() as audit_session:
        await record_security_event(
            audit_session,
            None,
            action="auth.authenticate",
            resource="identity",
            decision="deny",
            reason=reason,
            request=request,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            device_id=device_id,
            session_id=session_id,
        )


async def authorize(
    context: SecurityContext,
    session: AsyncSession,
    *,
    action: str,
    resource: str,
    request: Request | None = None,
    require_mfa: bool = False,
) -> None:
    reason = "allowed by role policy"
    if not get_settings().zero_trust_enforced:
        reason = "development security enforcement disabled"
    elif action not in _ROLE_ACTIONS.get(context.role, set()):
        reason = "role is not allowed to perform action"
    elif require_mfa and not context.mfa_verified:
        reason = "MFA required for privileged action"
    if reason not in {"allowed by role policy", "development security enforcement disabled"}:
        await record_security_event(
            session, context, action=action, resource=resource, decision="deny", reason=reason, request=request
        )
        raise SecurityError(reason)
    await record_security_event(
        session, context, action=action, resource=resource, decision="allow", reason=reason, request=request
    )


async def require_owned_trip(
    session: AsyncSession, context: SecurityContext, trip_id: int, request: Request | None = None
) -> TripRequest:
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        await record_security_event(
            session,
            context,
            action="trip.read",
            resource=f"trip:{trip_id}",
            decision="deny",
            reason="resource does not exist or is not visible",
            request=request,
        )
        raise HTTPException(status_code=404, detail=f"No trip {trip_id}.")
    owner = await session.get(User, trip.user_id)
    if not get_settings().zero_trust_enforced:
        return trip
    if owner is None or owner.tenant_id != context.tenant_id or trip.user_id != context.user_id:
        await record_security_event(
            session,
            context,
            action="trip.read",
            resource=f"trip:{trip_id}",
            decision="deny",
            reason="tenant or owner mismatch",
            request=request,
        )
        raise SecurityError("trip is outside the caller's tenant or ownership scope")
    return trip


async def require_owned_booking(
    session: AsyncSession, context: SecurityContext, log_id: int, request: Request | None = None
) -> HITLBookingLog:
    booking = await session.get(HITLBookingLog, log_id)
    if booking is None:
        raise HTTPException(status_code=404, detail=f"No booking log {log_id}.")
    await require_owned_trip(session, context, booking.trip_request_id, request)
    return booking


async def enforce_api_segment(
    request: Request,
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    if request.headers.get("x-source-segment") or request.headers.get("x-service-name"):
        await record_security_event(
            session,
            context,
            action="segment.assert",
            resource="api",
            decision="deny",
            reason="browser cannot assert an internal source segment",
            request=request,
        )
        raise SecurityError("source segment is assigned by the server")
    await record_security_event(
        session,
        context,
        action="segment.assert",
        resource="api",
        decision="allow",
        reason="authenticated request entered from web segment",
        request=request,
    )
    return context


def require_service_token(request: Request, expected_token: str) -> None:
    if request.headers.get("x-service-token") != expected_token:
        raise SecurityError("trusted service authentication required", 401)
