"""Local OIDC-compatible classroom issuer with server-backed sessions and TOTP MFA."""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.config import get_settings
from app.db import get_session
from app.dependencies import (
    create_security_session,
    issue_access_token,
    password_matches,
    verify_session_mfa,
)
from app.models import SecuritySession, User, utcnow
from app.schemas import LoginRequest, MfaVerifyRequest, TokenOut
from app.security import record_authentication_failure, record_security_event

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    user = await session.scalar(select(User).where(col(User.email) == body.email))
    if user is None or user.disabled or not password_matches(user, body.password):
        await record_authentication_failure(reason="invalid credentials", request=request)
        raise HTTPException(status_code=401, detail="invalid credentials")
    security_session = await create_security_session(session, user, body.device_id)
    await record_security_event(
        session,
        None,
        action="auth.login",
        resource=f"user:{user.id}",
        decision="allow",
        reason="password verified; MFA session pending",
    )
    return TokenOut(
        access_token=issue_access_token(security_session, user, get_settings().zero_trust_signing_secret.get_secret_value()),
        expires_at=security_session.expires_at.replace(tzinfo=UTC),
        session_id=security_session.session_id,
        mfa_required=True,
    )


@router.post("/mfa", response_model=TokenOut)
async def verify_mfa(
    body: MfaVerifyRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    security_session = await session.scalar(
        select(SecuritySession).where(col(SecuritySession.session_id) == body.session_id)
    )
    if security_session is None or security_session.expires_at <= utcnow():
        await record_authentication_failure(reason="MFA session expired", request=request)
        raise HTTPException(status_code=401, detail="session expired")
    if not await verify_session_mfa(session, security_session, body.code):
        await record_authentication_failure(reason="invalid MFA code", request=request)
        raise HTTPException(status_code=401, detail="invalid MFA code")
    user = await session.get(User, security_session.user_id)
    if user is None:
        await record_authentication_failure(reason="identity unavailable", request=request)
        raise HTTPException(status_code=401, detail="identity unavailable")
    return TokenOut(
        access_token=issue_access_token(security_session, user, get_settings().zero_trust_signing_secret.get_secret_value()),
        expires_at=security_session.expires_at.replace(tzinfo=UTC),
        session_id=security_session.session_id,
        mfa_required=False,
    )


@router.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, str]:
    return {
        "issuer": "/api/auth",
        "token_endpoint": "/api/auth/login",
        "jwks_uri": "/api/auth/jwks",
        "grant_types_supported": "password,mfa",
    }


@router.get("/jwks")
async def jwks() -> dict[str, list[object]]:
    return {"keys": []}
