"""Identity and zero-trust request context boundary."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.config import DEMO_USER_EMAIL, get_settings
from app.db import get_session
from app.models import User


@dataclass(frozen=True)
class SecurityContext:
    tenant_id: str
    user_id: int
    device_id: str
    role: str
    mfa_verified: bool


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
    if not all(isinstance(value, str) for value in (tenant_id, device_id, role)):
        raise HTTPException(status_code=401, detail="invalid identity claims")
    assert isinstance(tenant_id, str)
    assert isinstance(device_id, str)
    assert isinstance(role, str)
    if not isinstance(mfa_verified, bool):
        raise HTTPException(status_code=401, detail="invalid MFA claim")
    if (
        user.tenant_id != tenant_id
        or user.device_id != device_id
        or user.role != role
        or (mfa_verified and not user.mfa_enabled)
    ):
        raise HTTPException(status_code=403, detail="identity claims do not match persisted state")
    return SecurityContext(
        tenant_id=tenant_id,
        user_id=user.id,
        device_id=device_id,
        role=role,
        mfa_verified=mfa_verified,
    )


def _decode_token(token: str, secret: str) -> dict[str, object]:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        return json.loads(base64.urlsafe_b64decode(payload + "=="))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid zero-trust access token") from None


def create_access_token(
    *, user_id: int, tenant_id: str, device_id: str, role: str, mfa_verified: bool
) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "device_id": device_id,
                "role": role,
                "mfa_verified": mfa_verified,
                "expires_at": int(time.time()) + 900,
            },
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    signature = hmac.new(
        get_settings().zero_trust_signing_secret.get_secret_value().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


async def get_current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    if authorization is not None:
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
        _context_from_claims(claims, user)
        return user
    user = await session.scalar(select(User).where(col(User.email) == DEMO_USER_EMAIL))
    if user is not None:
        return user
    user = User(email=DEMO_USER_EMAIL)
    session.add(user)
    await session.commit()
    return user


async def get_security_context(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> SecurityContext:
    settings = get_settings()
    if authorization is None:
        if settings.zero_trust_enforced:
            raise HTTPException(status_code=401, detail="zero-trust access token required")
        user = await get_current_user(session=session)
        assert user.id is not None
        return SecurityContext(user.tenant_id, user.id, user.device_id, user.role, True)
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    claims = _decode_token(
        authorization.removeprefix("Bearer "),
        settings.zero_trust_signing_secret.get_secret_value(),
    )
    user_id = claims.get("user_id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="invalid user identity")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=403, detail="identity is disabled or unknown")
    return _context_from_claims(claims, user)
