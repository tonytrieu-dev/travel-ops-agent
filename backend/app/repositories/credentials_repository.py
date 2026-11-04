"""Credential changes preserve account policy and atomically invalidate prior sessions."""

import base64
import secrets
from urllib.parse import quote

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models import SecuritySession, User, utcnow
from app.security_identity import password_hash


async def _replace_authenticator(session: AsyncSession, user: User, secret: str) -> None:
    # Callers hold the account lock until credentials and session revocation commit together.
    user.totp_secret = secret
    user.last_totp_time_step = None
    if user.id is not None:
        await session.execute(
            update(SecuritySession).where(col(SecuritySession.user_id) == user.id).values(revoked_at=utcnow())
        )


async def enroll_identity(session: AsyncSession, email: str, password: str, *, reset_mfa: bool = False) -> str:
    if not 12 <= len(password) <= 200:
        raise ValueError("password must contain 12 to 200 characters")
    user = await session.scalar(select(User).where(col(User.email) == email).with_for_update())
    if user is None or user.disabled:
        raise ValueError("enabled existing account required")
    if user.password_hash and not reset_mfa:
        raise ValueError("account is already enrolled")
    if not reset_mfa:
        user.password_hash = password_hash(password)
    await _replace_authenticator(
        session, user, base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    )
    await session.commit()
    return f"otpauth://totp/TravelOps:{quote(email, safe='')}?secret={user.totp_secret}&issuer=TravelOps"


async def seed_identities(session: AsyncSession) -> None:
    identities = (
        ("traveler@tenant-a.test", "tenant-a", "traveler", "device-a-traveler"),
        ("operator@tenant-a.test", "tenant-a", "security-operator", "device-a-operator"),
        ("traveler@tenant-b.test", "tenant-b", "traveler", "device-b-traveler"),
    )
    for email, tenant_id, role, device_id in identities:
        user = await session.scalar(select(User).where(col(User.email) == email).with_for_update())
        if user is None:
            user = User(email=email, tenant_id=tenant_id, role=role, device_id=device_id)
            session.add(user)
        elif (user.tenant_id, user.role, user.device_id, user.disabled) != (tenant_id, role, device_id, False):
            raise ValueError(f"Refusing to reset mismatched synthetic identity: {email}")
        user.password_hash = password_hash("demo-password")
        await _replace_authenticator(session, user, "JBSWY3DPEHPK3PXP")
    await session.commit()
