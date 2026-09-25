import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.dependencies import _context_from_claims, _decode_token, _totp_code
from app.models import User

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
