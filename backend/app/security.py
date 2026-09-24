"""Zero-trust policy, segmentation, monitoring, and incident-response primitives."""

from uuid import uuid4

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import SecurityContext, get_security_context
from app.models import SecurityEvent

SEGMENT_PATHS = {
    ("web", "api"),
    ("api", "agent"),
    ("api", "data"),
    ("api", "connector"),
    ("agent", "data"),
    ("security-operations", "data"),
}


async def record_security_event(
    session: AsyncSession,
    context: SecurityContext,
    *,
    source_segment: str,
    target_segment: str,
    action: str,
    resource: str,
    decision: str,
    reason: str,
) -> SecurityEvent:
    event = SecurityEvent(
        tenant_id=context.tenant_id,
        actor_user_id=context.user_id,
        device_id=context.device_id,
        source_segment=source_segment,
        target_segment=target_segment,
        action=action,
        resource=resource,
        decision=decision,
        reason=reason,
        correlation_id=str(uuid4()),
    )
    session.add(event)
    await session.commit()
    return event


def segment_path_allowed(source_segment: str, target_segment: str) -> bool:
    return (source_segment, target_segment) in SEGMENT_PATHS


async def enforce_api_segment(
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    # External requests enter through the web segment; callers cannot select their own segment.
    source_segment = "web"
    if not segment_path_allowed(source_segment, "api"):
        await record_security_event(
            session,
            context,
            source_segment=source_segment,
            target_segment="api",
            action="request",
            resource="api",
            decision="deny",
            reason="segment path web->api is denied",
        )
        raise HTTPException(status_code=403, detail="segment path web->api is denied")
    await record_security_event(
        session,
        context,
        source_segment=source_segment,
        target_segment="api",
        action="request",
        resource="api",
        decision="allow",
        reason="declared segment path",
    )
    return context
