"""FastAPI adapters for request identity."""

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security_identity import SecurityContext, resolve_security_context


async def get_security_context(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    return await resolve_security_context(session, authorization, request)
