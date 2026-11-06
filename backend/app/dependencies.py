"""FastAPI adapters for request identity."""

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security_identity import SESSION_COOKIE_NAME, SecurityContext, resolve_security_context


async def get_security_context(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    return await resolve_security_context(session, authorization or (f"Bearer {cookie}" if cookie else None), request)
