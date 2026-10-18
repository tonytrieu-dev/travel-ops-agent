"""Password, MFA, token, session, and request-principal lifecycle."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, timedelta
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app import config
from app.models import SecuritySession, User, utcnow
from app.schemas import LoginRequest, MfaVerifyRequest, TokenOut
from app.security_evidence import record_authentication_failure, record_security_event


@dataclass(frozen=True)
class SecurityContext:
    tenant_id: str
    user_id: int
    device_id: str
    role: str
    mfa_verified: bool
    session_id: str | None = None


# --- Inbound token verification: decode a bearer token and reconcile its claims with live state


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
        tenant_id=tenant_id, user_id=user.id, device_id=device_id, role=role,
        mfa_verified=mfa_verified, session_id=session_id,
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


async def authenticate_identity(
    authorization: str, session: AsyncSession
) -> tuple[User, SecurityContext]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    claims = _decode_token(
        authorization.removeprefix("Bearer "),
        config.get_settings().zero_trust_signing_secret.get_secret_value(),
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
    authorization: str, session: AsyncSession, request: Request | None = None
) -> tuple[User, SecurityContext]:
    try:
        return await authenticate_identity(authorization, session)
    except HTTPException:
        await record_authentication_failure(reason="authentication failure", request=request)
        raise


# --- Credential primitives: pure, no DB or request state (directly unit-tested)


def password_hash(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), b"travelops-demo", 120_000).hex()


def totp_code(secret: str, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    counter = timestamp // 30
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def password_matches(user: User, password: str) -> bool:
    return bool(user.password_hash) and hmac.compare_digest(user.password_hash, password_hash(password))


# --- Session lifecycle: create a pending session and upgrade it once MFA is verified


async def create_security_session(
    session: AsyncSession, user: User, device_id: str
) -> SecuritySession:
    assert user.id is not None, "cannot create a session for an unpersisted user"
    security_session = SecuritySession(
        user_id=user.id, tenant_id=user.tenant_id, device_id=device_id,
        mfa_verified=False, expires_at=utcnow() + timedelta(minutes=15),
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
    if not hmac.compare_digest(totp_code(user.totp_secret), code):
        return False
    security_session.mfa_verified = True
    await session.commit()
    return True


# --- Token issuance: mint a signed bearer token from a session


def issue_access_token(security_session: SecuritySession, user: User, secret: str) -> str:
    payload = {
        "user_id": security_session.user_id,
        "tenant_id": security_session.tenant_id,
        "device_id": security_session.device_id,
        "role": user.role,
        "mfa_verified": security_session.mfa_verified,
        "session_id": security_session.session_id,
        "expires_at": int(security_session.expires_at.replace(tzinfo=UTC).timestamp()),
        "jti": uuid4().hex,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _token_out(security_session: SecuritySession, user: User, mfa_required: bool) -> TokenOut:
    return TokenOut(
        access_token=issue_access_token(
            security_session, user,
            config.get_settings().zero_trust_signing_secret.get_secret_value(),
        ),
        expires_at=security_session.expires_at.replace(tzinfo=UTC),
        session_id=security_session.session_id,
        mfa_required=mfa_required,
    )


# --- Request flows: the module's external interface called by the auth routes and dependencies


async def login_identity(
    session: AsyncSession, body: LoginRequest, request: Request
) -> TokenOut:
    user = await session.scalar(select(User).where(col(User.email) == body.email))
    if user is None or user.disabled or user.device_id != body.device_id or not password_matches(user, body.password):
        await record_authentication_failure(
            reason="invalid credentials", tenant_id=user.tenant_id if user else None,
            actor_user_id=user.id if user else None,
            device_id=user.device_id if user else None, request=request,
        )
        raise HTTPException(status_code=401, detail="invalid credentials")
    security_session = await create_security_session(session, user, body.device_id)
    await record_security_event(
        session, None, action="auth.login", resource=f"user:{user.id}", decision="allow",
        reason="password verified; MFA session pending" if user.mfa_enabled else "password verified",
        request=request, tenant_id=user.tenant_id, actor_user_id=user.id,
        device_id=security_session.device_id, session_id=security_session.session_id,
    )
    return _token_out(security_session, user, user.mfa_enabled)


async def verify_identity_mfa(
    session: AsyncSession, body: MfaVerifyRequest, request: Request
) -> TokenOut:
    security_session = await session.scalar(
        select(SecuritySession).where(col(SecuritySession.session_id) == body.session_id)
    )
    if security_session is None:
        await record_authentication_failure(reason="MFA session expired", request=request)
        raise HTTPException(status_code=401, detail="session expired")
    evidence = {
        "tenant_id": security_session.tenant_id,
        "actor_user_id": security_session.user_id,
        "device_id": security_session.device_id,
        "session_id": security_session.session_id,
        "request": request,
    }
    if security_session.expires_at <= utcnow():
        await record_authentication_failure(reason="MFA session expired", **evidence)
        raise HTTPException(status_code=401, detail="session expired")
    if not await verify_session_mfa(session, security_session, body.code):
        await record_authentication_failure(reason="invalid MFA code", **evidence)
        raise HTTPException(status_code=401, detail="invalid MFA code")
    user = await session.get(User, security_session.user_id)
    if user is None:
        await record_authentication_failure(reason="identity unavailable", request=request)
        raise HTTPException(status_code=401, detail="identity unavailable")
    return _token_out(security_session, user, False)


async def resolve_current_user(
    session: AsyncSession, authorization: str | None, request: Request
) -> User:
    if authorization is not None:
        user, _ = await _authenticated_identity(authorization, session, request)
        return user
    user = await session.scalar(select(User).where(col(User.email) == config.DEMO_USER_EMAIL))
    if user is not None:
        return user
    user = User(email=config.DEMO_USER_EMAIL)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        user = await session.scalar(select(User).where(col(User.email) == config.DEMO_USER_EMAIL))
        assert user is not None
    return user


async def resolve_security_context(
    session: AsyncSession, authorization: str | None, request: Request
) -> SecurityContext:
    if authorization is None:
        if config.get_settings().zero_trust_enforced:
            await record_authentication_failure(reason="missing bearer token", request=request)
            raise HTTPException(status_code=401, detail="zero-trust access token required")
        user = await resolve_current_user(session, None, request)
        assert user.id is not None
        return SecurityContext(user.tenant_id, user.id, user.device_id, user.role, True)
    _, context = await _authenticated_identity(authorization, session, request)
    return context
