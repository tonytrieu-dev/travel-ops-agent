"""Password, MFA, token, session, and request-principal lifecycle."""

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, timedelta
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from starlette.concurrency import run_in_threadpool

from app import config
from app.models import SecuritySession, User, utcnow
from app.schemas import LoginRequest, MfaVerifyRequest, TokenOut
from app.security_evidence import record_authentication_failure, record_security_event
from app import security_posture as posture
from app.security_token_issuer import issue_token, verify_token


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


async def authenticate_identity(
    authorization: str, session: AsyncSession
) -> tuple[User, SecurityContext]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    claims = verify_token(authorization.removeprefix("Bearer "))
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
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()
    return f"pbkdf2_sha256${salt.hex()}${digest}"


def totp_code(secret: str, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    counter = timestamp // 30
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def password_matches(user: User, password: str) -> bool:
    stored = user.password_hash
    if not stored:
        return False
    if "$" not in stored:
        # Existing credentials are upgraded only after a successful login.
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), b"travelops-demo", 120_000).hex()
        return hmac.compare_digest(stored, digest)
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
    except ValueError:
        return False
    if len(salt) != 16 or len(expected) != 32:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return hmac.compare_digest(expected, digest)


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
    timestamp = int(time.time())
    if not hmac.compare_digest(totp_code(user.totp_secret, timestamp), code):
        return False
    consumed = await session.scalar(
        update(User).where(
            col(User.id) == user.id,
            col(User.disabled).is_(False),
            col(User.mfa_enabled).is_(True),
            or_(col(User.last_totp_time_step).is_(None), col(User.last_totp_time_step) < timestamp // 30),
        ).values(last_totp_time_step=timestamp // 30).returning(col(User.id))
    )
    if consumed is None:
        await session.rollback()
        return False
    security_session.mfa_verified = True
    assert user.id is not None
    await record_security_event(
        session,
        SecurityContext(user.tenant_id, user.id, security_session.device_id, user.role, True, security_session.session_id),
        action="auth.mfa.verify", resource=f"session:{security_session.session_id}",
        decision="allow", reason="TOTP verified",
    )
    return True


# --- Token issuance: mint a signed bearer token from a session


def issue_access_token(security_session: SecuritySession, user: User) -> str:
    payload: dict[str, object] = {
        "user_id": security_session.user_id,
        "tenant_id": security_session.tenant_id,
        "device_id": security_session.device_id,
        "role": user.role,
        "mfa_verified": security_session.mfa_verified,
        "session_id": security_session.session_id,
        "expires_at": int(security_session.expires_at.replace(tzinfo=UTC).timestamp()),
        "jti": uuid4().hex,
    }
    return issue_token(payload)


def _token_out(security_session: SecuritySession, user: User, mfa_required: bool) -> TokenOut:
    return TokenOut(
        access_token=issue_access_token(security_session, user),
        expires_at=security_session.expires_at.replace(tzinfo=UTC),
        session_id=security_session.session_id,
        mfa_required=mfa_required,
    )


# --- Request flows: the module's external interface called by the auth routes and dependencies


async def login_identity(
    session: AsyncSession, body: LoginRequest, request: Request
) -> TokenOut:
    user = await session.scalar(select(User).where(col(User.email) == body.email))
    if user is None or user.disabled or user.device_id != body.device_id or not await run_in_threadpool(password_matches, user, body.password):
        await record_authentication_failure(
            reason="invalid credentials", tenant_id=user.tenant_id if user else None,
            resource=f"user:{user.id}" if user else "identity",
            request=request,
        )
        raise HTTPException(status_code=401, detail="invalid credentials")
    if "$" not in user.password_hash:
        user.password_hash = await run_in_threadpool(password_hash, body.password)
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
        "resource": f"user:{security_session.user_id}",
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


async def _demo_user(session: AsyncSession) -> User:
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
        if posture.enforcement_active():
            await record_authentication_failure(reason="missing bearer token", request=request)
            raise HTTPException(status_code=401, detail="zero-trust access token required")
        user = await _demo_user(session)
        assert user.id is not None
        return SecurityContext(user.tenant_id, user.id, user.device_id, user.role, True)
    _, context = await _authenticated_identity(authorization, session, request)
    return context
