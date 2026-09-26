"""Identity and zero-trust request context boundary."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import timedelta
from dataclasses import dataclass
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.config import DEMO_USER_EMAIL, get_settings
from app.db import get_session
from app.models import SecuritySession, User, utcnow


@dataclass(frozen=True)
class SecurityContext:
    tenant_id: str
    user_id: int
    device_id: str
    role: str
    mfa_verified: bool
    session_id: str | None = None


def _context_from_claims(claims: dict[str, object], user: User) -> SecurityContext:
    if user.id is None or user.disabled:
        raise HTTPException(status_code=403, detail="identity is disabled or unknown")
    expires_at = claims.get("expires_at")
    if not isinstance(expires_at, int) or expires_at < time.time():
        raise HTTPException(status_code=401, detail="zero-trust access token expired")
    tenant_id = claims.get("tenant_id")
    device_id = claims.get("device_id")
    role = claims.get("role")
    mfa_verified = claims.get("mfa_verified")
    session_id = claims.get("session_id")
    if not all(isinstance(value, str) for value in (tenant_id, device_id, role)):
        raise HTTPException(status_code=401, detail="invalid identity claims")
    assert isinstance(tenant_id, str)
    assert isinstance(device_id, str)
    assert isinstance(role, str)
    if not isinstance(mfa_verified, bool):
        raise HTTPException(status_code=401, detail="invalid MFA claim")
    if not isinstance(session_id, str):
        raise HTTPException(status_code=401, detail="invalid session claim")
    if (
        user.tenant_id != tenant_id
        or user.device_id != device_id
        or user.role != role
        or (mfa_verified and not user.mfa_enabled)
        or (user.mfa_enabled and not mfa_verified)
    ):
        raise HTTPException(status_code=403, detail="identity claims do not match persisted state")
    return SecurityContext(
        tenant_id=tenant_id,
        user_id=user.id,
        device_id=device_id,
        role=role,
        mfa_verified=mfa_verified,
        session_id=session_id,
    )


def _decode_token(token: str, secret: str) -> dict[str, object]:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        if not isinstance(claims, dict):
            raise ValueError("token payload must be an object")
        return claims
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid zero-trust access token") from None


async def _authenticate_identity(
    authorization: str,
    session: AsyncSession,
) -> tuple[User, SecurityContext]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    claims = _decode_token(
        authorization.removeprefix("Bearer "),
        get_settings().zero_trust_signing_secret.get_secret_value(),
    )
    user_id = claims.get("user_id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="invalid user identity")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=403, detail="identity is disabled or unknown")
    context = _context_from_claims(claims, user)
    persisted_session = await session.scalar(
        select(SecuritySession).where(col(SecuritySession.session_id) == context.session_id)
    )
    if (
        persisted_session is None
        or persisted_session.user_id != user.id
        or persisted_session.revoked_at is not None
        or persisted_session.expires_at <= utcnow()
        or persisted_session.device_id != context.device_id
        or persisted_session.mfa_verified != context.mfa_verified
    ):
        raise HTTPException(status_code=401, detail="session revoked or expired")
    return user, context


async def _authenticated_identity(
    authorization: str,
    session: AsyncSession,
    request: Request | None = None,
) -> tuple[User, SecurityContext]:
    try:
        return await _authenticate_identity(authorization, session)
    except HTTPException:
        from app.security import record_authentication_failure

        await record_authentication_failure(reason="authentication failure", request=request)
        raise


def _password_hash(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), b"travelops-demo", 120_000).hex()


def _totp_code(secret: str, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    counter = timestamp // 30
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def password_matches(user: User, password: str) -> bool:
    return bool(user.password_hash) and hmac.compare_digest(user.password_hash, _password_hash(password))


async def create_security_session(session: AsyncSession, user: User, device_id: str) -> SecuritySession:
    assert user.id is not None, "cannot create a session for an unpersisted user"
    security_session = SecuritySession(
        user_id=user.id,
        tenant_id=user.tenant_id,
        device_id=device_id,
        mfa_verified=not user.mfa_enabled,
        expires_at=utcnow() + timedelta(minutes=15),
    )
    session.add(security_session)
    await session.commit()
    return security_session


async def verify_session_mfa(
    session: AsyncSession, security_session: SecuritySession, code: str
) -> bool:
    user = await session.get(User, security_session.user_id)
    if user is None or user.disabled or not user.mfa_enabled or security_session.revoked_at is not None:
        return False
    if not hmac.compare_digest(_totp_code(user.totp_secret), code):
        return False
    security_session.mfa_verified = True
    await session.commit()
    return True


def issue_access_token(security_session: SecuritySession, user: User, secret: str) -> str:
    payload = {
        "user_id": security_session.user_id,
        "tenant_id": security_session.tenant_id,
        "device_id": security_session.device_id,
        "role": user.role,
        "mfa_verified": security_session.mfa_verified,
        "session_id": security_session.session_id,
        "expires_at": int(security_session.expires_at.timestamp()),
        "jti": uuid4().hex,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    if authorization is not None:
        user, _ = await _authenticated_identity(authorization, session, request)
        return user
    user = await session.scalar(select(User).where(col(User.email) == DEMO_USER_EMAIL))
    if user is not None:
        return user
    user = User(email=DEMO_USER_EMAIL)
    session.add(user)
    await session.commit()
    return user


async def get_security_context(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    settings = get_settings()
    if authorization is None:
        if settings.zero_trust_enforced:
            from app.security import record_authentication_failure

            await record_authentication_failure(reason="missing bearer token", request=request)
            raise HTTPException(status_code=401, detail="zero-trust access token required")
        user = await get_current_user(request=request, session=session)
        assert user.id is not None
        return SecurityContext(user.tenant_id, user.id, user.device_id, user.role, True)
    _, context = await _authenticated_identity(authorization, session, request)
    return context
