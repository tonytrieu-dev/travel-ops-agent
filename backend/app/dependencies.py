"""FastAPI adapters for request identity."""

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security_identity import SecurityContext, resolve_current_user, resolve_security_context


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    return await resolve_current_user(session, authorization, request)


async def get_security_context(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    return await resolve_security_context(session, authorization, request)
