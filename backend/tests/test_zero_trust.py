import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.dependencies import SecurityContext, _context_from_claims, _decode_token, _totp_code
from app.models import SecurityIncident, SecuritySession, User, utcnow
from app.request_context import bind_correlation_id, correlation_id
from app.security import DENIAL_THRESHOLD, record_security_event

pytestmark = pytest.mark.no_database


def test_zero_trust_accepts_matching_identity_claims() -> None:
    user = User(
        id=7,
        email="traveler@example.test",
        tenant_id="tenant-a",
        role="traveler",
        device_id="device-a",
        mfa_enabled=True,
    )
    claims: dict[str, object] = {
        "user_id": 7,
        "tenant_id": "tenant-a",
        "device_id": "device-a",
        "role": "traveler",
        "mfa_verified": True,
        "session_id": "session-a",
        "expires_at": int(datetime.now(UTC).timestamp()) + 60,
    }
    context = _context_from_claims(claims, user)
    assert context.user_id == user.id
    assert context.tenant_id == user.tenant_id
    assert context.mfa_verified is True


def test_zero_trust_rejects_unverified_claim_for_mfa_enabled_user() -> None:
    user = User(
        id=7,
        email="traveler@example.test",
        tenant_id="tenant-a",
        role="traveler",
        device_id="device-a",
        mfa_enabled=True,
    )
    claims: dict[str, object] = {
        "user_id": 7,
        "tenant_id": "tenant-a",
        "device_id": "device-a",
        "role": "traveler",
        "mfa_verified": False,
        "session_id": "session-a",
        "expires_at": int(datetime.now(UTC).timestamp()) + 60,
    }
    with pytest.raises(HTTPException, match="claims do not match"):
        _context_from_claims(claims, user)


@pytest.mark.parametrize(
    ("user_role", "claim_role", "mfa_enabled"),
    [
        ("traveler", "admin", True),
        ("security-operator", "security-operator", False),
    ],
)
def test_zero_trust_rejects_claims_that_escalate_persisted_identity(
    user_role: str, claim_role: str, mfa_enabled: bool
) -> None:
    user = User(
        id=7,
        email="operator@example.test",
        tenant_id="tenant-a",
        role=user_role,
        device_id="device-a",
        mfa_enabled=mfa_enabled,
    )
    claims: dict[str, object] = {
        "user_id": 7,
        "tenant_id": "tenant-a",
        "device_id": "device-a",
        "role": claim_role,
        "mfa_verified": True,
        "session_id": "session-a",
        "expires_at": int(datetime.now(UTC).timestamp()) + 60,
    }
    with pytest.raises(HTTPException, match="claims do not match"):
        _context_from_claims(claims, user)


def test_zero_trust_rejects_expired_access_context() -> None:
    user = User(
        id=7,
        email="traveler@example.test",
        tenant_id="tenant-a",
        role="traveler",
        device_id="device-a",
        mfa_enabled=True,
    )
    claims: dict[str, object] = {
        "user_id": 7,
        "tenant_id": "tenant-a",
        "device_id": "device-a",
        "role": "traveler",
        "mfa_verified": True,
        "session_id": "session-a",
        "expires_at": 0,
    }
    with pytest.raises(HTTPException, match="expired"):
        _context_from_claims(claims, user)


def test_zero_trust_rejects_default_signing_secret_when_enabled() -> None:
    with pytest.raises(ValueError, match="ZERO_TRUST_SIGNING_SECRET"):
        Settings(
            cerebras_api_key="test",
            searchapi_api_key="test",
            tavily_api_key="test",
            database_url="postgresql+asyncpg://localhost/test",
            zero_trust_enforced=True,
        )


def test_zero_trust_rejects_tampered_access_token() -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "user_id": 7,
                "tenant_id": "tenant-a",
                "device_id": "device-a",
                "role": "traveler",
                "mfa_verified": False,
                "session_id": "session-a",
                "expires_at": int(datetime.now(UTC).timestamp()) + 60,
            },
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    signature = hmac.new(
        b"local-development-only", payload.encode(), hashlib.sha256
    ).hexdigest()
    replacement = "0" if signature[-1] != "0" else "1"
    tampered = f"{payload}.{signature[:-1]}{replacement}"
    with pytest.raises(HTTPException, match="invalid zero-trust access token"):
        _decode_token(tampered, "local-development-only")


def test_zero_trust_totp_is_time_bound_and_six_digits() -> None:
    code = _totp_code("JBSWY3DPEHPK3PXP", timestamp=1_760_000_000)
    assert code.isdigit() and len(code) == 6
    assert code != _totp_code("JBSWY3DPEHPK3PXP", timestamp=1_760_000_031)


def test_correlation_id_is_stable_within_a_boundary_and_resets_afterward() -> None:
    with bind_correlation_id("workflow-a"):
        assert correlation_id() == correlation_id() == "workflow-a"
    with bind_correlation_id("workflow-b"):
        assert correlation_id() == "workflow-b"


async def test_threshold_revokes_current_session_when_device_already_has_open_incident() -> None:
    security_session = SecuritySession(
        session_id="session-b",
        user_id=7,
        tenant_id="tenant-a",
        device_id="device-a",
        expires_at=utcnow() + timedelta(minutes=15),
    )
    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.scalar = AsyncMock(
        side_effect=[
            DENIAL_THRESHOLD,
            SecurityIncident(
                tenant_id="tenant-a",
                device_id="device-a",
                session_id="session-a",
                status="open",
                reason="denial threshold",
            ),
            security_session,
        ]
    )

    await record_security_event(
        session,
        SecurityContext(
            tenant_id="tenant-a",
            user_id=7,
            device_id="device-a",
            role="traveler",
            mfa_verified=True,
            session_id="session-b",
        ),
        action="trip.read",
        resource="trip:999999",
        decision="deny",
        reason="resource does not exist or is not visible",
    )

    assert security_session.revoked_at is not None
