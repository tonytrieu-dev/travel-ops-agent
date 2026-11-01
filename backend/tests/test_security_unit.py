"""Fast security-boundary regressions that do not require PostgreSQL."""

import base64
from datetime import UTC, datetime
import json
import time

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.security_identity import issue_access_token
from app.models import SecuritySession, User

pytestmark = pytest.mark.no_database


def _settings(*, enforced: bool) -> Settings:
    return Settings(
        cerebras_api_key="x",
        searchapi_api_key="x",
        tavily_api_key="x",
        database_url="postgresql+asyncpg://localhost/travel_agent_test",
        zero_trust_enforced=enforced,
        zero_trust_signing_secret="x" * 32,
    )


def test_access_token_expiry_is_utc_on_a_non_utc_server(monkeypatch: pytest.MonkeyPatch) -> None:
    expires_at = datetime(2030, 1, 1, 12, 0)
    security_session = SecuritySession(
        user_id=1,
        tenant_id="tenant-a",
        device_id="device-a",
        expires_at=expires_at,
    )
    user = User(id=1, email="traveler@example.test")
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(enforced=True))
    monkeypatch.setenv("TZ", "PST8PDT")
    time.tzset()
    try:
        token = issue_access_token(security_session, user)
    finally:
        monkeypatch.undo()
        time.tzset()

    payload = token.split(".", 1)[0]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims["expires_at"] == int(expires_at.replace(tzinfo=UTC).timestamp())


def test_enforced_signing_secret_requires_32_bytes() -> None:
    settings = {
        "cerebras_api_key": "x",
        "searchapi_api_key": "x",
        "tavily_api_key": "x",
        "database_url": "postgresql+asyncpg://localhost/travel_agent_test",
        "zero_trust_enforced": True,
    }
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(**settings, zero_trust_signing_secret="x" * 31)

    assert Settings(**settings, zero_trust_signing_secret="x" * 32)
