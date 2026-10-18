"""Server-assigned API segment admission."""

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import get_security_context
from app.security_admission import SecurityError
from app.security_evidence import record_security_event
from app.security_identity import SecurityContext


async def enforce_api_segment(
    request: Request,
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    if request.headers.get("x-source-segment") or request.headers.get("x-service-name"):
        await record_security_event(
            session, context, action="segment.assert", resource="api", decision="deny",
            reason="browser cannot assert an internal source segment", request=request,
        )
        raise SecurityError("source segment is assigned by the server")
    await record_security_event(
        session, context, action="segment.assert", resource="api", decision="allow",
        reason="authenticated request entered from web segment", request=request,
    )
    return context
