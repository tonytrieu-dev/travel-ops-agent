"""Zero-trust API boundary monitoring."""

from uuid import uuid4

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import SecurityContext, get_security_context
from app.models import SecurityEvent


async def enforce_api_segment(
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    # External requests enter through the web segment; callers cannot select their own segment.
    session.add(
        SecurityEvent(
            tenant_id=context.tenant_id,
            actor_user_id=context.user_id,
            device_id=context.device_id,
            source_segment="web",
            target_segment="api",
            action="request",
            resource="api",
            decision="allow",
            reason="authenticated web request",
            correlation_id=str(uuid4()),
        )
    )
    await session.commit()
    return context
